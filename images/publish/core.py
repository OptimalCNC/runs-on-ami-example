"""Shared publishing configuration, AWS access, and publication recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


PUBLICATION_TAG = "image-publication-id"
DIGEST_TAG = "image-sha256"
FAMILY_TAG = "image-family"
NAME_TAG = "Name"
RETIRED_TAG = "image-retired"


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def write_json(path: Path, value: dict):
    """Replace a result atomically so interrupted work cannot look complete."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class ImageName:
    value: str

    def __post_init__(self) -> None:
        # Leave room in the 128-character AMI name for the two generated suffixes.
        require(isinstance(self.value, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,101}", self.value) is not None,
                "Image name must be 1-102 letters, digits, dots, underscores or hyphens, "
                "starting with a letter or digit")


@dataclass(frozen=True)
class PublishingTarget:
    account_id: str
    region: str
    required_tags: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, data: Any) -> "PublishingTarget":
        require(isinstance(data, dict), "Publishing target must be a JSON object")
        account, region = (data.get(key) for key in ("account_id", "region"))
        require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account) is not None,
                "Publishing target account_id must be a 12-digit string")
        require(isinstance(region, str) and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region) is not None,
                "Invalid AWS region in publishing target")
        tags = data.get("required_tags")
        require(isinstance(tags, dict) and len(tags) <= 45,
                "Publishing tags must be an object containing at most 45 tags")
        require(all(isinstance(k, str) and 1 <= len(k) <= 128 and not k.lower().startswith("aws:")
                    and ",Value=" not in k and isinstance(v, str) and len(v) <= 256 for k, v in tags.items()),
                "Publishing target contains invalid AWS tags")
        require(not {PUBLICATION_TAG, DIGEST_TAG, FAMILY_TAG, NAME_TAG, RETIRED_TAG}.intersection(tags),
                "Publishing target cannot override publication identity or retention tags")
        return cls(account, region, tuple(sorted(tags.items())))

    def identity(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id, "region": self.region,
            "required_tags": dict(self.required_tags),
        }

    def owns(self, resource: dict[str, Any]) -> bool:
        tags = {tag["Key"]: tag["Value"] for tag in resource.get("Tags", [])}
        return resource.get("OwnerId") == self.account_id and all(
            tags.get(key) == value for key, value in self.required_tags)


@dataclass(frozen=True)
class PublishingConfig:
    name: ImageName
    target: PublishingTarget

    @classmethod
    def load(cls, path: Path) -> "PublishingConfig":
        data = read_json(path)
        require(isinstance(data, dict), "Publishing configuration must be a JSON object")
        require(not set(data) - {"name", "account_id", "region", "tags"},
                "Publishing configuration accepts only name, account_id, region, and tags")
        name = ImageName(data.get("name"))
        target = PublishingTarget.parse({
            "account_id": data.get("account_id"), "region": data.get("region"),
            "required_tags": data.get("tags", {}),
        })
        return cls(name, target)


class AwsError(RuntimeError):
    def __init__(self, operation: str, detail: str):
        self.operation = operation
        self.detail = detail.strip()
        super().__init__(f"AWS {operation} failed: {self.detail}")

    def missing(self) -> bool:
        return "InvalidAMIID.NotFound" in self.detail or "InvalidSnapshot.NotFound" in self.detail


class Cloud:
    """One verified publisher session and the AWS operations publication requires."""

    def __init__(self, target: PublishingTarget, profile: str | None = None):
        self.target = target
        self.profile = profile
        self.environment = dict(os.environ, AWS_PAGER="", AWS_MAX_ATTEMPTS="4", AWS_RETRY_MODE="standard")
        self.source_profile = profile
        self.source_environment = dict(self.environment)

    def aws(self, service: str, operation: str, *arguments: str) -> dict[str, Any]:
        command = ["aws", "--region", self.target.region, "--output", "json", "--no-cli-pager"]
        if self.profile:
            command += ["--profile", self.profile]
        result = subprocess.run(command + [service, operation, *arguments], env=self.environment,
                                capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AwsError(operation, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def authenticate(self) -> None:
        self.profile = self.source_profile
        self.environment = dict(self.source_environment)
        # Resolve credentials once so the CLI and coldsnap use the same login.
        credentials = self.aws("configure", "export-credentials", "--format", "process")
        for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
            self.environment.pop(key, None)
        self.environment.update({
            "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
        })
        if credentials.get("SessionToken"):
            self.environment["AWS_SESSION_TOKEN"] = credentials["SessionToken"]
        self.profile = None
        identity = self.aws("sts", "get-caller-identity")
        require(identity.get("Account") == self.target.account_id,
                "AWS credentials do not belong to the target account")

    def require_unencrypted_snapshots(self) -> None:
        settings = self.aws("ec2", "get-ebs-encryption-by-default")
        require(settings.get("EbsEncryptionByDefault") is False,
                f"Unencrypted publication requires EBS encryption by default to be disabled in "
                f"{self.target.account_id}/{self.target.region}; this command does not change account settings")

    def upload(self, disk: Path, tags: dict[str, str], volume_gib: int, output: Path) -> str:
        command = ["coldsnap", "--region", self.target.region, "upload", str(disk),
                    "--volume-size", str(volume_gib), "--description", f"Image publication {tags[PUBLICATION_TAG]}",
                    "--no-progress"]
        for key, value in sorted(tags.items()):
            command += ["--tag", f"Key={key},Value={value}"]
        with (output / "upload.log").open("w") as log:
            result = subprocess.run(command, env=self.environment, stdout=subprocess.PIPE,
                                    stderr=log, text=True, timeout=3000)
            if result.returncode:
                raise RuntimeError(f"coldsnap upload failed; see {output / 'upload.log'}")
        return result.stdout.strip()

    def discover(self, publication_id: str) -> tuple[list[str], list[str]]:
        filters = [
            {"Name": f"tag:{PUBLICATION_TAG}", "Values": [publication_id]},
            *[{"Name": f"tag:{key}", "Values": [value]} for key, value in self.target.required_tags],
        ]
        snapshots = self.aws("ec2", "describe-snapshots", "--owner-ids", self.target.account_id,
                             "--filters", json.dumps(filters))["Snapshots"]
        images = self.aws("ec2", "describe-images", "--owners", self.target.account_id,
                          "--filters", json.dumps(filters))["Images"]
        # EC2 filters interpret wildcards; ownership values must match literally.
        return ([s["SnapshotId"] for s in snapshots if self.target.owns(s)],
                [i["ImageId"] for i in images if self.target.owns(i)])

    def versions(self, name: ImageName) -> list[dict[str, Any]]:
        filters = [
            {"Name": "name", "Values": [f"{name.value}-*"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": f"tag:{FAMILY_TAG}", "Values": [name.value]},
            {"Name": f"tag:{NAME_TAG}", "Values": [name.value]},
            *[{"Name": f"tag:{key}", "Values": [value]} for key, value in self.target.required_tags],
        ]
        images = self.aws("ec2", "describe-images", "--owners", self.target.account_id,
                          "--filters", json.dumps(filters))["Images"]
        return [image for image in images if self.target.owns(image)]

    def retired_snapshots(self, name: ImageName) -> list[dict[str, Any]]:
        filters = [
            {"Name": f"tag:{RETIRED_TAG}", "Values": ["true"]},
            {"Name": f"tag:{FAMILY_TAG}", "Values": [name.value]},
            {"Name": f"tag:{NAME_TAG}", "Values": [name.value]},
            *[{"Name": f"tag:{key}", "Values": [value]} for key, value in self.target.required_tags],
        ]
        snapshots = self.aws("ec2", "describe-snapshots", "--owner-ids", self.target.account_id,
                             "--filters", json.dumps(filters))["Snapshots"]
        return [snapshot for snapshot in snapshots if self.target.owns(snapshot)]

    def mark_retired(self, snapshot_id: str) -> None:
        tags = {**dict(self.target.required_tags), RETIRED_TAG: "true"}
        self.aws("ec2", "create-tags", "--resources", snapshot_id, "--tags",
                 json.dumps([{"Key": key, "Value": value} for key, value in tags.items()]))

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        try:
            values = self.aws("ec2", "describe-snapshots", "--snapshot-ids", snapshot_id)["Snapshots"]
            return values[0] if values else None
        except AwsError as error:
            if error.missing():
                return None
            raise

    def image(self, image_id: str) -> dict[str, Any] | None:
        try:
            values = self.aws("ec2", "describe-images", "--image-ids", image_id)["Images"]
            return values[0] if values else None
        except AwsError as error:
            if error.missing():
                return None
            raise

    def register(self, name: str, snapshot_id: str, volume_gib: int, tags: dict[str, str]) -> str:
        parameters = {
            "Name": name, "Architecture": "x86_64", "VirtualizationType": "hvm",
            "BootMode": "uefi", "EnaSupport": True,
            "RootDeviceName": "/dev/sda1",
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {
                "SnapshotId": snapshot_id, "DeleteOnTermination": True, "VolumeType": "gp3",
                "VolumeSize": volume_gib,
            }}],
            "TagSpecifications": [{"ResourceType": "image", "Tags": [
                {"Key": key, "Value": value} for key, value in sorted(tags.items())
            ]}],
        }
        return self.aws("ec2", "register-image", "--cli-input-json", json.dumps(parameters))["ImageId"]

    def deregister(self, image_id: str) -> None:
        try:
            self.aws("ec2", "deregister-image", "--image-id", image_id)
        except AwsError as error:
            if not error.missing():
                raise

    def delete_snapshot(self, snapshot_id: str) -> None:
        for attempt in range(12):
            try:
                self.aws("ec2", "delete-snapshot", "--snapshot-id", snapshot_id)
                return
            except AwsError as error:
                if error.missing():
                    return
                if "InvalidSnapshot.InUse" not in error.detail or attempt == 11:
                    raise
                time.sleep(5)


def owned(resource: dict[str, Any], target: PublishingTarget, publication_id: str, digest: str) -> None:
    tags = {tag["Key"]: tag["Value"] for tag in resource.get("Tags", [])}
    require(target.owns(resource)
            and tags.get(PUBLICATION_TAG) == publication_id and tags.get(DIGEST_TAG) == digest,
            "Resource ownership or artifact digest does not match the publication record")


@dataclass(frozen=True)
class PublicationRecord:
    target: PublishingTarget
    publication_id: str
    digest: str
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "PublicationRecord":
        record = read_json(path)
        require(isinstance(record, dict), "Publication record must be a JSON object")
        target = PublishingTarget.parse(record.get("target"))
        publication_id = record.get("publication_id")
        require(isinstance(publication_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", publication_id) is not None,
                "Publication record has an invalid publication ID")
        artifact = record.get("artifact")
        require(isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is not None,
                "Publication record has an invalid artifact digest")
        require(record.get("status") in ("pending", "available", "cleanup-needed", "deleted"),
                "Publication record has an invalid status")
        return cls(target, publication_id, artifact["sha256"], record)


def cleanup_record(path: Path, profile: str | None = None,
                   cloud: Cloud | None = None) -> dict[str, Any]:
    publication = PublicationRecord.load(path)
    target, record = publication.target, publication.data
    cloud = cloud or Cloud(target, profile)
    require(cloud.target.identity() == target.identity(),
            "Cloud session belongs to a different publishing target")
    cloud.authenticate()
    snapshot_ids, image_ids = cloud.discover(publication.publication_id)
    snapshot_ids = sorted(set(snapshot_ids + record.get("discovered_snapshot_ids", [])
                              + ([record["snapshot_id"]] if record.get("snapshot_id") else [])))
    image_ids = sorted(set(image_ids + record.get("discovered_ami_ids", [])
                           + ([record["ami_id"]] if record.get("ami_id") else [])))
    record["discovered_snapshot_ids"] = snapshot_ids
    record["discovered_ami_ids"] = image_ids
    write_json(path, record)
    if not snapshot_ids and record["status"] in {"pending", "cleanup-needed"}:
        raise RuntimeError("No snapshot ID is visible yet; retain this record and retry cleanup after EBS discovery catches up")
    # Read and check the complete deletion set before performing any mutation.
    snapshots = {sid: cloud.snapshot(sid) for sid in snapshot_ids}
    images = {iid: cloud.image(iid) for iid in image_ids}
    for resource in [*snapshots.values(), *images.values()]:
        if resource:
            owned(resource, target, publication.publication_id, publication.digest)
    for image in images.values():
        if image:
            sources = {mapping["Ebs"]["SnapshotId"] for mapping in image.get("BlockDeviceMappings", [])
                       if "Ebs" in mapping and "SnapshotId" in mapping["Ebs"]}
            require(sources and sources.issubset(snapshots), "Image refers to snapshots outside this publication")
    for image_id, image in images.items():
        if image and image.get("State") != "deregistered":
            cloud.deregister(image_id)
    for snapshot_id, snapshot in snapshots.items():
        if snapshot:
            cloud.delete_snapshot(snapshot_id)
    record["status"] = "deleted"
    write_json(path, record)
    return record


def run_cli(operation: Callable[[], None]) -> int:
    os.umask(0o077)

    def terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt("Operation interrupted")

    signal.signal(signal.SIGTERM, terminate)
    try:
        operation()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0

