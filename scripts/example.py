"""Shared boundaries for this example; no third-party Python dependencies."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
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


def outputs(values: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as stream:
            for key, value in values.items():
                require("\n" not in str(value), f"multiline output: {key}")
                stream.write(f"{key}={value}\n")


@dataclasses.dataclass(frozen=True)
class AmiIdentity:
    id: str
    owner: str
    architecture: str
    boot_mode: str
    inventory_file: str
    inventory_sha256: str

    @classmethod
    def parse(cls, value: dict, name: str) -> AmiIdentity:
        require(set(value) == {f.name for f in dataclasses.fields(cls)}, f"unexpected {name} fields")
        match(value["id"], AMI, name + ".id")
        match(value["owner"], r"\d{12}", name + ".owner")
        require(value["architecture"] == "x86_64", "only x86_64 is qualified")
        require(value["boot_mode"] in ("uefi", "uefi-preferred", "legacy-bios"), "record the exact AMI boot mode")
        match(value["inventory_sha256"], SHA256, name + ".inventory_sha256")
        match(value["inventory_file"], r"infra/[a-z0-9-]+inventory\.json", name + ".inventory_file")
        return cls(**value)

    @property
    def effective_boot_mode(self) -> str:
        # The selected Nitro type must support UEFI, so preference never silently falls back.
        return "legacy-bios" if self.boot_mode == "legacy-bios" else "uefi"


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
class Deployment:
    repository: str
    account_id: str
    region: str
    environment: str
    runs_on: RunsOnInstallation | None
    source_ami: AmiIdentity
    controller_ami: AmiIdentity
    instance_type: str
    vcpus: int
    builder_instance_type: str
    vpc_id: str
    subnet_id: str
    security_group_id: str
    controller_role_arn: str
    builder_profile_name: str
    probe_profile_name: str
    artifact_bucket: str
    root_volume_gib: int
    parent_root_volume_gib: int
    deadlines: dict[str, int]
    retain_hours: int
    private: bool

    @classmethod
    def parse(cls, value: dict) -> Deployment:
        expected = {f.name for f in dataclasses.fields(cls)}
        require(set(value) == expected, f"deployment keys differ: {set(value) ^ expected}")
        value = dict(value)
        for key, pattern in {
            "repository": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "account_id": r"\d{12}",
            "region": r"[a-z]{2}-[a-z]+-\d", "environment": r"[A-Za-z0-9_-]+",
            "instance_type": INSTANCE_TYPE, "builder_instance_type": INSTANCE_TYPE,
            "vpc_id": r"vpc-[0-9a-f]{17}", "subnet_id": r"subnet-[0-9a-f]{17}",
            "security_group_id": r"sg-[0-9a-f]{17}",
            "controller_role_arn": rf"arn:aws:iam::{value['account_id']}:role/[A-Za-z0-9+=,.@_/-]+",
            "builder_profile_name": r"[A-Za-z0-9+=,.@_-]+", "probe_profile_name": r"[A-Za-z0-9+=,.@_-]+",
            "artifact_bucket": r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
        }.items():
            match(value[key], pattern, key)
        for name in ("source_ami", "controller_ami"):
            value[name] = AmiIdentity.parse(value[name], name)
        value["runs_on"] = RunsOnInstallation.parse(value["runs_on"]) if value["runs_on"] is not None else None
        require(type(value["vcpus"]) is int and 1 <= value["vcpus"] <= 64, "vcpus must be 1..64")
        for name in ("root_volume_gib", "parent_root_volume_gib"):
            require(type(value[name]) is int and 8 <= value[name] <= 256, f"{name} must be 8..256")
        require(value["root_volume_gib"] >= value["parent_root_volume_gib"], "candidate root must cover the parent root")
        require(type(value["retain_hours"]) is int and 1 <= value["retain_hours"] <= 168, "retention must be 1..168 hours")
        require(type(value["private"]) is bool, "private must be a boolean")
        require(set(value["deadlines"]) == {"boot_seconds", "registration_seconds", "workflow_seconds"}, "deadline keys differ")
        for key, seconds in value["deadlines"].items():
            require(type(seconds) is int and 60 <= seconds <= 18000, f"invalid deadline: {key}")
        require(value["deadlines"]["workflow_seconds"] > value["deadlines"]["registration_seconds"], "workflow deadline too short")
        return cls(**value)

    def require_runs_on(self) -> RunsOnInstallation:
        require(self.runs_on is not None, "configure the actual RunsOn installation before building or routing jobs")
        return self.runs_on

    def image_inputs(self) -> dict:
        return {"source_ami": dataclasses.asdict(self.source_ami), "builder_instance_type": self.builder_instance_type,
                "root_volume_gib": self.root_volume_gib,
                "runs_on_version": self.require_runs_on().version,
                "runs_on_bootstrap_version": self.require_runs_on().bootstrap_version}

    def label(self, key: str, ami: str, *, parent: bool = False) -> str:
        installation = self.require_runs_on()
        match(key, r"[A-Za-z0-9_-]+", "runner routing key")
        match(ami, AMI, "runner AMI")
        volume = self.parent_root_volume_gib if parent else self.root_volume_gib
        return (f"runs-on={key}/family={self.instance_type}/cpu={self.vcpus}/ami={ami}"
                f"/spot=false/retry=false/env={installation.environment}/region={self.region}"
                f"/private={str(self.private).lower()}/volume={volume}gb:gp3")


def load_deployment(path: str = "infra/deployment.json", *, inventories: bool = True) -> Deployment:
    deployment = Deployment.parse(read_json(ROOT / path))
    if inventories:
        installation = deployment.require_runs_on()
        for identity in (deployment.source_ami, deployment.controller_ami):
            require(file_sha(ROOT / identity.inventory_file) == identity.inventory_sha256,
                    f"inventory hash differs: {identity.inventory_file}")
            inventory = read_json(ROOT / identity.inventory_file)
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
    return deployment


def recipe(lock: dict, deployment: Deployment, root: Path = ROOT) -> tuple[str, dict[str, str]]:
    hashes = {}
    for name in lock["recipe_files"]:
        path = (root / name).resolve()
        require(path.is_relative_to(root.resolve()) and path.is_file(), f"invalid recipe file: {name}")
        hashes[name] = file_sha(path)
    return digest({"lock": lock, "files": hashes, "deployment": deployment.image_inputs()}), hashes


def build_id(run_id: str, attempt: str, variant: str) -> str:
    return match(f"{run_id}-{attempt}-{variant}", r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", "build ID")


def resource_tags(deployment: Deployment, build: str, purpose: str, expiry: str) -> list[dict[str, str]]:
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
    def __init__(self, deployment: Deployment):
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
        object_key = f"{self.deployment.repository}/{key}"
        target = f"s3://{bucket.name}/{object_key}"
        run(["aws", "s3", "cp", str(local), target, "--region", bucket.region,
             "--only-show-errors", "--sse", "AES256"], timeout=1200)
        metadata = self.call("s3api", "head-object", {"Bucket": bucket.name, "Key": object_key,
                                                      "ExpectedBucketOwner": bucket.account_id})
        version = metadata.get("VersionId")
        require(bool(version) and version != "null", "artifact upload did not produce an immutable S3 version")
        return target + "?versionId=" + quote(version, safe="")

    def artifacts(self) -> VersionedBucket:
        if self._artifact_bucket is None:
            d = self.deployment
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
