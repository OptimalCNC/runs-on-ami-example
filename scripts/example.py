"""Shared boundaries for this example; no third-party Python dependencies."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
OWNER_TAG = "ami-example:owner"
BUILD_TAG = "ami-example:build-id"
PURPOSE_TAG = "ami-example:purpose"
EXPIRY_TAG = "ami-example:expires-at"
RUNS_ON_TAG = "ami-example:runs-on-repository"
SHA256 = r"[0-9a-f]{64}"
AMI = r"ami-[0-9a-f]{17}"
INSTANCE = r"i-[0-9a-f]{17}"
INSTANCE_TYPE = r"[a-z][a-z0-9]+\.(?:nano|micro|small|medium|large|xlarge|[1-9][0-9]*xlarge)"


class InvalidInput(ValueError):
    pass


class AwsCommandError(subprocess.CalledProcessError):
    def __init__(self, error: subprocess.CalledProcessError):
        super().__init__(error.returncode, error.cmd, error.output, error.stderr)
        parsed = re.search(r"An error occurred \(([^)]+)\)", error.stderr or "")
        self.code = parsed[1] if parsed else "Unknown"

    def __str__(self):
        return (self.stderr or super().__str__()).strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidInput(message)


def match(value: Any, pattern: str, name: str) -> str:
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None,
            f"{name} must match {pattern}; got {value!r}")
    return value


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value: dt.datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "timestamp needs a timezone")
    return result


def run(args: list[str], **kwargs: Any) -> str:
    return subprocess.check_output(args, text=True, **kwargs).strip()


@dataclasses.dataclass(frozen=True)
class ParentSelection:
    id: str
    owner: str

    @classmethod
    def parse(cls, value: dict, name: str = "parent") -> ParentSelection:
        require(isinstance(value, dict) and set(value) == {"id", "owner"}, f"unexpected {name} fields")
        return cls(match(value["id"], AMI, name + ".id"), match(value["owner"], r"\d{12}", name + ".owner"))


@dataclasses.dataclass(frozen=True)
class ImageIdentity(ParentSelection):
    architecture: str
    boot_mode: str

    @classmethod
    def parse(cls, value: dict, name: str = "AMI") -> ImageIdentity:
        require(isinstance(value, dict) and set(value) == {f.name for f in dataclasses.fields(cls)}, f"unexpected {name} fields")
        ParentSelection.parse({key: value[key] for key in ("id", "owner")}, name)
        require(value["architecture"] == "x86_64", "only x86_64 is qualified")
        require(value["boot_mode"] in ("uefi", "uefi-preferred", "legacy-bios"), "record the exact AMI boot mode")
        return cls(**value)

    @property
    def effective_boot_mode(self) -> str:
        return "legacy-bios" if self.boot_mode == "legacy-bios" else "uefi"

    def semantic_identity(self, region: str) -> dict:
        return {"id": self.id, "owner": self.owner, "architecture": self.architecture,
                "boot_mode": self.boot_mode, "region": region}


@dataclasses.dataclass(frozen=True)
class AmiIdentity(ImageIdentity):
    inventory_file: str
    inventory_sha256: str

    @classmethod
    def parse(cls, value: dict, name: str = "AMI") -> AmiIdentity:
        require(isinstance(value, dict) and set(value) == {f.name for f in dataclasses.fields(cls)}, f"unexpected {name} fields")
        ImageIdentity.parse({key: value[key] for key in ("id", "owner", "architecture", "boot_mode")}, name)
        match(value["inventory_sha256"], SHA256, name + ".inventory_sha256")
        require(isinstance(value["inventory_file"], str) and bool(value["inventory_file"])
                and "\x00" not in value["inventory_file"], "inventory_file must be a file reference")
        return cls(**value)

    def record(self) -> dict:
        return dataclasses.asdict(self)

    def semantic_identity(self, region: str) -> dict:
        return {**super().semantic_identity(region), "inventory_sha256": self.inventory_sha256}


@dataclasses.dataclass(frozen=True)
class VerifiedParent(AmiIdentity):
    """An image identity whose captured inventory has been read and verified."""

    @classmethod
    def load(cls, value: dict, base_dir: Path, installation: RunsOnInstallation, name: str = "parent") -> VerifiedParent:
        identity = cls.parse(value, name)
        path = (base_dir / identity.inventory_file).resolve()
        require(path.is_file(), f"parent inventory is missing: {path}")
        content = path.read_bytes()
        require(hashlib.sha256(content).hexdigest() == identity.inventory_sha256, f"inventory hash differs: {path}")
        inventory = json.loads(content)
        verify_inventory(inventory, installation)
        object.__setattr__(identity, "_inventory_path", path)
        object.__setattr__(identity, "_inventory", inventory)
        return identity

    @property
    def inventory_path(self) -> Path:
        return self._inventory_path

    @property
    def inventory(self) -> dict:
        return self._inventory


@dataclasses.dataclass(frozen=True)
class CobaltIdentity:
    version: str
    core: str
    prefix: str

    @classmethod
    def parse(cls, value: dict) -> CobaltIdentity:
        require(isinstance(value, dict) and set(value) == {f.name for f in dataclasses.fields(cls)},
                "Cobalt identity is missing or has unexpected fields")
        version = match(value["version"], r"3\.\d+\.\d+", "Xenomai version")
        require(value["core"] == "cobalt", "image requires the Cobalt core")
        require(value["prefix"] == "/usr/xenomai", "Xenomai installation prefix differs")
        return cls(version, "cobalt", "/usr/xenomai")


@dataclasses.dataclass(frozen=True)
class RunsOnInstallation:
    environment: str
    version: str
    bootstrap_version: str

    @classmethod
    def parse(cls, value: dict) -> RunsOnInstallation:
        require(isinstance(value, dict) and set(value) == {"environment", "version", "bootstrap_version"},
                "RunsOn installation needs its exact environment, service version, and bootstrap version")
        return cls(match(value["environment"], r"[a-z0-9-]+", "RunsOn environment"),
                   match(value["version"], r"\d+\.\d+\.\d+", "RunsOn version"),
                   match(value["bootstrap_version"], r"\d+\.\d+\.\d+", "RunsOn bootstrap version"))


@dataclasses.dataclass(frozen=True)
class CloudTarget:
    account_id: str
    region: str

    @classmethod
    def parse(cls, value: dict) -> CloudTarget:
        require(isinstance(value, dict) and set(value) == {"account_id", "region"}, "cloud target needs account_id and region")
        return cls(match(value["account_id"], r"\d{12}", "account_id"),
                   match(value["region"], r"[a-z]{2}-[a-z]+-\d", "region"))


@dataclasses.dataclass(frozen=True)
class CleanupContext(CloudTarget):
    repository: str
    artifact_bucket: str

    @classmethod
    def parse(cls, value: dict) -> CleanupContext:
        require(isinstance(value, dict) and set(value) == {f.name for f in dataclasses.fields(CleanupContext)},
                "cleanup context needs account_id, region, repository and artifact_bucket")
        CloudTarget.parse({key: value[key] for key in ("account_id", "region")})
        match(value["repository"], r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "repository")
        match(value["artifact_bucket"], r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", "artifact_bucket")
        return cls(**value)

    def cleanup(self) -> CleanupContext:
        return CleanupContext(**{f.name: getattr(self, f.name) for f in dataclasses.fields(CleanupContext)})


@dataclasses.dataclass(frozen=True)
class InfrastructureBindings(CleanupContext):
    environment: str
    runs_on: RunsOnInstallation | None
    instance_type: str
    vcpus: int
    builder_instance_type: str
    vpc_id: str
    subnet_id: str
    security_group_id: str
    controller_role_arn: str
    builder_profile_name: str
    probe_profile_name: str
    root_volume_gib: int
    parent_root_volume_gib: int
    deadlines: dict[str, int]
    retain_hours: int
    private: bool

    @classmethod
    def parse(cls, value: dict) -> InfrastructureBindings:
        expected = {f.name for f in dataclasses.fields(InfrastructureBindings)}
        require(isinstance(value, dict) and set(value) == expected, f"binding keys differ: {set(value) ^ expected}")
        value = dict(value)
        CleanupContext.parse({f.name: value[f.name] for f in dataclasses.fields(CleanupContext)})
        for key, pattern in {
            "environment": r"[A-Za-z0-9_-]+", "instance_type": INSTANCE_TYPE, "builder_instance_type": INSTANCE_TYPE,
            "vpc_id": r"vpc-[0-9a-f]{17}", "subnet_id": r"subnet-[0-9a-f]{17}",
            "security_group_id": r"sg-[0-9a-f]{17}",
            "controller_role_arn": rf"arn:aws:iam::{value['account_id']}:role/[A-Za-z0-9+=,.@_/-]+",
            "builder_profile_name": r"[A-Za-z0-9+=,.@_-]+", "probe_profile_name": r"[A-Za-z0-9+=,.@_-]+",
        }.items():
            match(value[key], pattern, key)
        value["runs_on"] = RunsOnInstallation.parse(value["runs_on"]) if value["runs_on"] is not None else None
        require(type(value["vcpus"]) is int and 1 <= value["vcpus"] <= 64, "vcpus must be 1..64")
        for name in ("root_volume_gib", "parent_root_volume_gib"):
            require(type(value[name]) is int and 8 <= value[name] <= 256, f"{name} must be 8..256")
        require(value["root_volume_gib"] >= value["parent_root_volume_gib"], "candidate root must cover the parent root")
        require(type(value["retain_hours"]) is int and 1 <= value["retain_hours"] <= 168, "retention must be 1..168 hours")
        require(type(value["private"]) is bool, "private must be a boolean")
        require(isinstance(value["deadlines"], dict) and set(value["deadlines"]) == {"boot_seconds", "registration_seconds", "workflow_seconds"}, "deadline keys differ")
        for key, seconds in value["deadlines"].items():
            require(type(seconds) is int and 60 <= seconds <= 18000, f"invalid deadline: {key}")
        require(value["deadlines"]["workflow_seconds"] > value["deadlines"]["registration_seconds"], "workflow deadline too short")
        return cls(**value)

    def require_runs_on(self) -> RunsOnInstallation:
        require(self.runs_on is not None, "configure the actual RunsOn installation before building or routing jobs")
        return self.runs_on


@dataclasses.dataclass(frozen=True)
class BuildInputs(InfrastructureBindings):
    source_ami: VerifiedParent
    controller_ami: VerifiedParent

    @classmethod
    def parse(cls, value: dict, *, base_dir: Path | None = None) -> BuildInputs:
        expected = {f.name for f in dataclasses.fields(cls)}
        require(isinstance(value, dict) and set(value) == expected, f"build input keys differ: {set(value) ^ expected}")
        bindings = InfrastructureBindings.parse({f.name: value[f.name] for f in dataclasses.fields(InfrastructureBindings)})
        installation = bindings.require_runs_on()
        directory = Path.cwd() if base_dir is None else Path(base_dir)
        parents = {name: VerifiedParent.load(value[name], directory, installation, name)
                   for name in ("source_ami", "controller_ami")}
        return cls(**{f.name: getattr(bindings, f.name) for f in dataclasses.fields(InfrastructureBindings)}, **parents)

    def image_inputs(self) -> dict:
        return {"source_ami": self.source_ami.semantic_identity(self.region), "builder_instance_type": self.builder_instance_type,
                "root_volume_gib": self.root_volume_gib, "runs_on_version": self.require_runs_on().version,
                "runs_on_bootstrap_version": self.require_runs_on().bootstrap_version}

    def snapshot(self, output_directory: str | Path) -> Path:
        """Copy the complete verified input closure with relocatable references."""
        import shutil
        destination = Path(output_directory).resolve()
        destination.mkdir(parents=True, exist_ok=False)
        value = dataclasses.asdict(self)
        for name in ("source_ami", "controller_ami"):
            parent = getattr(self, name)
            relative = "parents/" + name.removesuffix("_ami") + ".json"
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(parent.inventory_path, target)
            require(file_sha(target) == parent.inventory_sha256, "parent inventory changed after loading")
            value[name]["inventory_file"] = relative
        path = destination / "manifest.json"
        write_json(path, value)
        return path


def verify_inventory(inventory: dict, installation: RunsOnInstallation) -> None:
    fields = {"os_version", "registered", "workspaces", "secure_boot", "packages_sha256", "package_inventory",
              "runner_version", "bootstrap_files", "runner_listener_sha256", "snap_hashes"}
    require(isinstance(inventory, dict) and fields <= set(inventory), "parent inventory fields are missing")
    require(inventory["os_version"] == "24.04", "parent must use Ubuntu 24.04")
    require(inventory["registered"] is False and inventory["workspaces"] == [], "parent is not clean")
    require(inventory["secure_boot"] is False, "unsigned-kernel qualification requires Secure Boot disabled")
    match(inventory["packages_sha256"], SHA256, "parent package inventory")
    require(hashlib.sha256(inventory["package_inventory"].encode()).hexdigest() == inventory["packages_sha256"], "parent package inventory digest is inconsistent")
    match(inventory["runner_version"], r"\d+\.\d+\.\d+", "inherited runner version")
    require(bool(inventory["bootstrap_files"]), "missing inherited RunsOn bootstrap")
    match(inventory["runner_listener_sha256"], SHA256, "inherited runner binary digest")
    for path, checksum in inventory["snap_hashes"].items():
        match(path, r"[A-Za-z0-9_.-]+\.snap", "inherited snap file")
        match(checksum, SHA256, "inherited snap digest")
    for path, checksum in inventory["bootstrap_files"].items():
        match(path, r"/usr/local/bin/runs-on-bootstrap-v?\d+\.\d+\.\d+", "inherited bootstrap path")
        match(checksum, SHA256, "inherited bootstrap digest")
    require(any(Path(p).name in {f"runs-on-bootstrap-{installation.bootstrap_version}", f"runs-on-bootstrap-v{installation.bootstrap_version}"} for p in inventory["bootstrap_files"]),
            "parent must contain the bootstrap version selected by the RunsOn installation")


def load_deployment(path: str | Path) -> BuildInputs:
    source = Path(path).resolve()
    return BuildInputs.parse(read_json(source), base_dir=source.parent)


def load_bindings(path: str | Path) -> InfrastructureBindings:
    value = read_json(path)
    return InfrastructureBindings.parse({f.name: value[f.name] for f in dataclasses.fields(InfrastructureBindings)})


def load_cleanup_context(path: str | Path) -> CleanupContext:
    value = read_json(path)
    return CleanupContext.parse({f.name: value[f.name] for f in dataclasses.fields(CleanupContext)})


def load_parent_record(path: str | Path, installation: RunsOnInstallation) -> VerifiedParent:
    source = Path(path).resolve()
    return VerifiedParent.load(read_json(source), source.parent, installation)


def recipe(lock: dict, deployment: BuildInputs, root: Path = ROOT) -> tuple[str, dict[str, str]]:
    hashes = {}
    for name in lock["recipe_files"]:
        path = (root / name).resolve()
        require(path.is_relative_to(root.resolve()) and path.is_file(), f"invalid recipe file: {name}")
        hashes[name] = file_sha(path)
    return digest({"lock": lock, "files": hashes, "deployment": deployment.image_inputs()}), hashes


def build_id(run_id: str, attempt: str, variant: str) -> str:
    return match(f"{run_id}-{attempt}-{variant}", r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", "build ID")


def resource_tags(deployment: CleanupContext, build: str, purpose: str, expiry: str) -> list[dict[str, str]]:
    match(build, r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", "build ID")
    require(purpose in ("builder", "probe", "candidate", "test"), "invalid resource purpose")
    parse_time(expiry)
    return [{"Key": k, "Value": v} for k, v in {
        OWNER_TAG: deployment.repository, BUILD_TAG: build, PURPOSE_TAG: purpose, EXPIRY_TAG: expiry,
    }.items()]


@dataclasses.dataclass(frozen=True)
class VersionedBucket:
    name: str
    account_id: str
    region: str


class Cloud:
    """An AWS CLI boundary bound to a verified account and explicit region."""
    def __init__(self, deployment: CloudTarget):
        self.deployment = deployment
        self._artifact_bucket: VersionedBucket | None = None
        account = self.call("sts", "get-caller-identity")["Account"]
        require(account == deployment.account_id, f"AWS account mismatch: {account}")

    def call(self, service: str, operation: str, payload: dict | None = None) -> dict:
        args = ["aws", service, operation, "--region", self.deployment.region, "--output", "json",
                "--no-cli-pager", "--cli-connect-timeout", "10", "--cli-read-timeout", "30"]
        if payload:
            args += ["--cli-input-json", json.dumps(payload)]
        try:
            result = run(args, timeout=90, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as error:
            raise AwsCommandError(error) from error
        return json.loads(result) if result else {}

    def instances(self, filters: list[dict]) -> list[dict]:
        # AWS CLI performs service pagination unless explicitly disabled.
        response = self.call("ec2", "describe-instances", {"Filters": filters})
        return [i for r in response["Reservations"] for i in r["Instances"]]

    def instance(self, instance_id: str) -> dict:
        match(instance_id, INSTANCE, "instance ID")
        response = self.call("ec2", "describe-instances", {"InstanceIds": [instance_id]})
        return response["Reservations"][0]["Instances"][0]

    def wait_terminated(self, ids: list[str], seconds: int = 600) -> None:
        deadline = time.monotonic() + seconds
        while ids:
            ids = [i for i in ids if self.instance(i)["State"]["Name"] != "terminated"]
            if not ids:
                return
            require(time.monotonic() < deadline, f"termination deadline exceeded: {ids}")
            time.sleep(10)

    def retain(self, local: Path, key: str) -> str:
        require(local.is_file(), f"artifact missing: {local}")
        bucket = self.artifacts()
        object_key = f"{self.artifact_context().repository}/reports/{key}"
        target = f"s3://{bucket.name}/{object_key}"
        run(["aws", "s3", "cp", str(local), target, "--region", bucket.region,
             "--only-show-errors", "--sse", "AES256"], timeout=1200)
        metadata = self.call("s3api", "head-object", {"Bucket": bucket.name, "Key": object_key,
                                                      "ExpectedBucketOwner": bucket.account_id})
        version = metadata.get("VersionId")
        require(bool(version) and version != "null", "artifact upload did not produce an immutable S3 version")
        return target + "?versionId=" + quote(version, safe="")

    def artifact_context(self) -> CleanupContext:
        if isinstance(self.deployment, CleanupContext):
            return self.deployment.cleanup()
        raise InvalidInput("artifact operations require repository and bucket bindings")

    def artifacts(self) -> VersionedBucket:
        if self._artifact_bucket is None:
            d = self.artifact_context()
            request = {"Bucket": d.artifact_bucket, "ExpectedBucketOwner": d.account_id}
            versioning = self.call("s3api", "get-bucket-versioning", request)
            require(versioning.get("Status") == "Enabled", "artifact bucket versioning must be enabled")
            location = self.call("s3api", "get-bucket-location", request).get("LocationConstraint")
            region = "us-east-1" if location is None else ("eu-west-1" if location == "EU" else location)
            require(region == d.region, "artifact bucket must be in the qualification region")
            self._artifact_bucket = VersionedBucket(d.artifact_bucket, d.account_id, region)
        return self._artifact_bucket


def tags_of(resource: dict) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in resource.get("Tags", [])}


def filters_for(owner: str, build: str | None = None) -> list[dict]:
    result = [{"Name": f"tag:{OWNER_TAG}", "Values": [owner]}]
    if build:
        result.append({"Name": f"tag:{BUILD_TAG}", "Values": [build]})
    return result
