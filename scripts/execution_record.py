"""Explicit GitHub job plans and durable per-attempt execution records."""
import dataclasses
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote

from example import (AMI, BUILD_TAG, EXPIRY_TAG, INSTANCE_TYPE, OWNER_TAG, PURPOSE_TAG, SHA256,
                     AwsCommandError, CleanupContext, match, parse_time, read_json, require, run, tags_of)
from deployment_state import ConfigurationBinding, VersionedObject


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
        return f"state/executions/{self.run_id}/{self.run_attempt}/"


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
class ImageSnapshot:
    ami_id: str
    source_build_id: str
    recipe_id: str
    created_at: str
    expires_at: str
    boot_mode: str
    snapshot_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value):
        require(isinstance(value, dict) and set(value) == {field.name for field in dataclasses.fields(cls)}, "image snapshot fields differ")
        match(value["ami_id"], AMI, "selected image AMI")
        match(value["source_build_id"], r"[1-9]\d*-[1-9]\d*-(?:one|two)", "selected image build")
        match(value["recipe_id"], SHA256, "selected image recipe")
        for field in ("created_at", "expires_at"):
            parse_time(value[field])
        require(value["boot_mode"] in ("uefi", "uefi-preferred"), "selected image boot mode differs")
        snapshots = value["snapshot_ids"]
        require(isinstance(snapshots, list) and bool(snapshots), "selected image snapshots are missing")
        snapshots = tuple(sorted(match(identifier, r"snap-[0-9a-f]{17}", "selected snapshot") for identifier in snapshots))
        require(len(snapshots) == len(set(snapshots)), "selected image snapshots must be distinct")
        return cls(**{**value, "snapshot_ids": snapshots})

    @classmethod
    def capture(cls, image):
        tags = tags_of(image)
        return cls.parse({"ami_id": image["ImageId"], "source_build_id": tags[BUILD_TAG],
                          "recipe_id": tags["ami-example:recipe-id"], "created_at": image["CreationDate"],
                          "expires_at": tags[EXPIRY_TAG], "boot_mode": image["BootMode"],
                          "snapshot_ids": [mapping["Ebs"]["SnapshotId"] for mapping in image["BlockDeviceMappings"] if "Ebs" in mapping]})

    def as_dict(self):
        return {**dataclasses.asdict(self), "snapshot_ids": list(self.snapshot_ids)}

    def inspect(self, cloud):
        """Prove saved image ownership without current launch policy or lifetime gates."""
        context = cloud.deployment
        images = cloud.call("ec2", "describe-images", {"ImageIds": [self.ami_id], "Owners": [context.account_id]})["Images"]
        require(len(images) == 1, "selected image is absent or owned by another account")
        image = images[0]
        require(image["ImageId"] == self.ami_id and image["OwnerId"] == context.account_id,
                "selected image cloud identity differs")
        expected = {OWNER_TAG: context.repository, BUILD_TAG: self.source_build_id, PURPOSE_TAG: "candidate",
                    EXPIRY_TAG: self.expires_at, "ami-example:recipe-id": self.recipe_id}
        require(all(tags_of(image).get(key) == value for key, value in expected.items()), "selected image ownership or recipe differs")
        require(ImageSnapshot.capture(image) == self, "selected image creation, boot mode or snapshots differ")
        return image


@dataclasses.dataclass(frozen=True)
class ExecutionRecord:
    attempt: RunAttempt
    build_id: str
    cleanup: CleanupContext
    instance_type: str
    plan: JobPlan
    terminal: str
    deadline_seconds: int
    registration_seconds: int
    configuration: ConfigurationBinding | None
    image: ImageSnapshot | None = None

    @classmethod
    def parse(cls, value):
        require(isinstance(value, dict) and set(value) == {"schema_version", "repository", "run_id", "run_attempt",
                "build_id", "cleanup", "instance_type", "plan", "terminal", "deadline_seconds", "registration_seconds", "configuration", "image"}
                and value["schema_version"] == 1, "execution record fields differ")
        attempt = RunAttempt.parse(value["repository"], value["run_id"], value["run_attempt"])
        build = match(value["build_id"], r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", "execution build ID")
        require(build.startswith(f"{attempt.run_id}-{attempt.run_attempt}-"), "execution build belongs to another run attempt")
        cleanup = CleanupContext.parse(value["cleanup"])
        require(cleanup.repository == attempt.repository, "execution cleanup repository differs")
        instance_type = match(value["instance_type"], INSTANCE_TYPE, "execution instance type")
        configuration = ConfigurationBinding.parse(value["configuration"]) if value["configuration"] is not None else None
        if configuration is not None:
            reference = VersionedObject.parse(configuration.uri)
            require(reference.bucket == cleanup.artifact_bucket and reference.key.startswith(f"{cleanup.repository}/state/"),
                    "execution configuration belongs to another storage scope")
        plan = JobPlan.parse(value["plan"])
        require(value["terminal"] in plan.stages, "terminal job must identify a plan stage")
        for name in ("deadline_seconds", "registration_seconds"):
            require(type(value[name]) is int and value[name] > 0, f"{name} must be a positive integer")
        return cls(attempt, build, cleanup, instance_type, plan, value["terminal"], value["deadline_seconds"], value["registration_seconds"],
                   configuration, ImageSnapshot.parse(value["image"]) if value["image"] is not None else None)

    @property
    def key(self):
        return f"{self.attempt.prefix}{self.build_id}.json"

    def as_dict(self):
        return {"schema_version": 1, **dataclasses.asdict(self.attempt), "build_id": self.build_id,
                "cleanup": dataclasses.asdict(self.cleanup), "instance_type": self.instance_type,
                "plan": self.plan.as_dict(), "terminal": self.terminal, "deadline_seconds": self.deadline_seconds,
                "registration_seconds": self.registration_seconds,
                "configuration": self.configuration.as_dict() if self.configuration else None,
                "image": self.image.as_dict() if self.image else None}

    def require_context(self, context):
        require(self.cleanup == context.cleanup(), "execution cleanup context differs")


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
