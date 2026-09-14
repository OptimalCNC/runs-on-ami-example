"""Explicit GitHub job plans and durable per-attempt execution records."""
import dataclasses
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote

from example import INSTANCE_TYPE, AwsCommandError, match, read_json, require, run


@dataclasses.dataclass(frozen=True)
class RunAttempt:
    repository: str
    run_id: str
    run_attempt: str

    @classmethod
    def parse(cls, repository, run_id, run_attempt):
        return cls(match(repository, r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "repository"),
                   match(run_id, r"[1-9][0-9]*", "run ID"),
                   match(run_attempt, r"[1-9][0-9]*", "run attempt"))

    @property
    def prefix(self):
        return f"executions/{self.run_id}/{self.run_attempt}/"


@dataclasses.dataclass(frozen=True)
class Job:
    name: str
    needs: tuple[str, ...]
    adopt: bool


@dataclasses.dataclass(frozen=True)
class JobPlan:
    stages: dict[str, Job]

    @classmethod
    def parse(cls, value):
        require(isinstance(value, dict) and bool(value), "job plan must be a nonempty mapping")
        stages = {}
        for stage, job in value.items():
            match(stage, r"[A-Za-z0-9_-]+", "job stage")
            require(isinstance(job, dict) and "name" in job and set(job) <= {"name", "needs", "adopt"}, "job plan fields differ")
            require(isinstance(job["name"], str) and bool(job["name"].strip()), "job name is missing")
            needs = job.get("needs", [])
            require(isinstance(needs, list) and all(isinstance(need, str) and need in value for need in needs),
                    "job dependencies must identify stages in the plan")
            require(len(needs) == len(set(needs)) and stage not in needs, "job dependencies must be distinct other stages")
            adopt = job.get("adopt", False)
            require(type(adopt) is bool, "job adoption must be boolean")
            stages[stage] = Job(job["name"], tuple(needs), adopt)
        require(len({job.name for job in stages.values()}) == len(stages), "job names must be distinct")
        resolved = set()
        while len(resolved) < len(stages):
            ready = {stage for stage, job in stages.items() if set(job.needs) <= resolved} - resolved
            require(bool(ready), "job dependencies contain a cycle")
            resolved |= ready
        return cls(stages)

    def as_dict(self):
        return {stage: {"name": job.name, "needs": list(job.needs), "adopt": job.adopt}
                for stage, job in self.stages.items()}


@dataclasses.dataclass(frozen=True)
class ImageSelection:
    kind: str
    record: dict

    @classmethod
    def parse(cls, value):
        require(isinstance(value, dict) and set(value) == {"kind", "record"}, "image selection fields differ")
        require(value["kind"] in ("accepted", "candidate") and isinstance(value["record"], dict), "invalid image selection")
        return cls(**value)

    def inspect(self, cloud, *, recovery=False):
        if self.kind == "accepted":
            from accepted_image import AcceptedImage
            selected = AcceptedImage.parse(self.record, cloud.deployment, allow_expired=recovery)
            return selected.inspect(cloud, minimum_seconds=None if recovery else 0), selected.expires_at, cloud.deployment.instance_type
        from retained_candidate import inspect_candidate, validate_source
        validate_source(self.record, cloud.deployment, for_launch=not recovery)
        return (inspect_candidate(cloud, self.record, minimum_seconds=None if recovery else 0, for_launch=not recovery),
                self.record["lifecycle"]["expires_at"], self.record["cloud"]["instance_type"])


@dataclasses.dataclass(frozen=True)
class ExecutionRecord:
    attempt: RunAttempt
    build_id: str
    account_id: str
    region: str
    instance_type: str
    plan: JobPlan
    terminal: str
    deadline_seconds: int
    registration_seconds: int
    image: ImageSelection | None = None

    @classmethod
    def parse(cls, value):
        require(isinstance(value, dict) and set(value) == {"schema_version", "repository", "run_id", "run_attempt",
                "build_id", "account_id", "region", "instance_type", "plan", "terminal", "deadline_seconds", "registration_seconds", "image"}
                and value["schema_version"] == 1, "execution record fields differ")
        attempt = RunAttempt.parse(value["repository"], value["run_id"], value["run_attempt"])
        build = match(value["build_id"], r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", "execution build ID")
        require(build.startswith(f"{attempt.run_id}-{attempt.run_attempt}-"), "execution build belongs to another run attempt")
        account = match(value["account_id"], r"[0-9]{12}", "execution account ID")
        region = match(value["region"], r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", "execution region")
        instance_type = match(value["instance_type"], INSTANCE_TYPE, "execution instance type")
        plan = JobPlan.parse(value["plan"])
        require(value["terminal"] in plan.stages, "terminal job must identify a plan stage")
        for name in ("deadline_seconds", "registration_seconds"):
            require(type(value[name]) is int and value[name] > 0, f"{name} must be a positive integer")
        return cls(attempt, build, account, region, instance_type, plan, value["terminal"], value["deadline_seconds"], value["registration_seconds"],
                   ImageSelection.parse(value["image"]) if value["image"] is not None else None)

    @property
    def key(self):
        return f"{self.attempt.prefix}{self.build_id}.json"

    def as_dict(self):
        return {"schema_version": 1, **dataclasses.asdict(self.attempt), "build_id": self.build_id,
                "account_id": self.account_id, "region": self.region, "instance_type": self.instance_type,
                "plan": self.plan.as_dict(), "terminal": self.terminal, "deadline_seconds": self.deadline_seconds,
                "registration_seconds": self.registration_seconds,
                "image": dataclasses.asdict(self.image) if self.image else None}

    def require_deployment(self, deployment):
        require(self.attempt.repository == deployment.repository and self.account_id == deployment.account_id
                and self.region == deployment.region, "execution cloud account, region or repository differs from deployment")


def download_record(cloud, bucket, key, target):
    metadata = cloud.call("s3api", "head-object", {"Bucket": bucket.name, "Key": key,
                                                   "ExpectedBucketOwner": bucket.account_id})
    version = metadata.get("VersionId")
    require(isinstance(version, str) and version not in ("", "null"), "execution record has no immutable S3 version")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    response = json.loads(run(["aws", "s3api", "get-object", "--bucket", bucket.name, "--key", key,
                               "--version-id", version, "--expected-bucket-owner", bucket.account_id,
                               "--region", bucket.region, "--no-cli-pager", str(target)], timeout=90))
    require(response.get("VersionId") == version, "S3 returned a different execution record version")
    return ExecutionRecord.parse(read_json(target)), version


def retain_record(cloud, record, source):
    """Create one binding per execution; an identical retry reuses that binding."""
    bucket = cloud.artifacts()
    key = f"{record.attempt.repository}/{record.key}"
    try:
        response = json.loads(run(["aws", "s3api", "put-object", "--bucket", bucket.name, "--key", key,
                                   "--body", str(source), "--if-none-match", "*", "--server-side-encryption", "AES256",
                                   "--expected-bucket-owner", bucket.account_id, "--region", bucket.region,
                                   "--no-cli-pager"], timeout=90, stderr=subprocess.PIPE))
        version = response.get("VersionId")
    except subprocess.CalledProcessError as error:
        if AwsCommandError(error).code != "PreconditionFailed":
            raise
        with tempfile.TemporaryDirectory() as temporary:
            existing, version = download_record(cloud, bucket, key, Path(temporary) / "execution.json")
        require(existing == record, "execution is already registered with a different plan or image selection")
    require(isinstance(version, str) and version not in ("", "null"), "execution record has no immutable S3 version")
    return f"s3://{bucket.name}/{key}?versionId={quote(version, safe='')}"


def registered_records(cloud, attempt, output):
    """Read versioned execution records only from the selected attempt's prefix."""
    require(cloud.deployment.repository == attempt.repository, "execution repository differs from deployment")
    bucket = cloud.artifacts()
    prefix = f"{attempt.repository}/{attempt.prefix}"
    contents = cloud.call("s3api", "list-objects-v2", {"Bucket": bucket.name, "Prefix": prefix,
                                                     "ExpectedBucketOwner": bucket.account_id}).get("Contents", [])
    result = []
    for item in contents:
        key = item["Key"]
        name = key.removeprefix(prefix)
        match(name, r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)\.json", "registered execution filename")
        require(key == prefix + name, "registered execution is outside its attempt prefix")
        record, _ = download_record(cloud, bucket, key, Path(output) / name)
        require(record.attempt == attempt and key == f"{attempt.repository}/{record.key}", "registered execution identity differs")
        result.append(record)
    return result
