#!/usr/bin/env python3
"""Publish a Cobalt image, retain its newest named snapshot, or delete a publication."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any
import uuid

from contracts import BuiltImage, read_json, write_json


PUBLICATION_TAG = "image-publication-id"
DIGEST_TAG = "image-sha256"
FAMILY_TAG = "image-family"
NAME_TAG = "Name"
RETIRED_TAG = "image-retired"
IMAGE_FAMILY = "ubuntu2404-xenomai-cobalt"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True)
class PublishingTarget:
    name: str
    account_id: str
    region: str
    publisher_role_arn: str
    required_tags: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, path: Path) -> "PublishingTarget":
        data = read_json(path)
        name, account, region = (data.get(key) for key in ("name", "account_id", "region"))
        require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]{2,23}", name) is not None,
                "Invalid installation name in publishing target")
        require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account) is not None,
                "Publishing target account_id must be a 12-digit string")
        require(isinstance(region, str) and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region) is not None,
                "Invalid AWS region in publishing target")
        role = data.get("publisher_role_arn")
        require(isinstance(role, str) and re.fullmatch(rf"arn:aws:iam::{account}:role/(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]+", role) is not None,
                "Publishing role must be an IAM role in the target AWS account")
        tags = data.get("required_tags")
        require(isinstance(tags, dict) and 1 <= len(tags) <= 45,
                "Publishing target must specify between 1 and 45 ownership tags")
        require(all(isinstance(k, str) and 1 <= len(k) <= 128 and not k.lower().startswith("aws:")
                    and ",Value=" not in k and isinstance(v, str) and len(v) <= 256 for k, v in tags.items()),
                "Publishing target contains invalid AWS tags")
        require(tags.get("runs-on-installation") == name, "Publishing ownership tag must match installation name")
        require(not {PUBLICATION_TAG, DIGEST_TAG, FAMILY_TAG, NAME_TAG, RETIRED_TAG}.intersection(tags),
                "Publishing target cannot override publication identity or retention tags")
        return cls(name, account, region, role, tuple(sorted(tags.items())))

    def identity(self) -> dict[str, Any]:
        return {
            "name": self.name, "account_id": self.account_id, "region": self.region,
            "publisher_role_arn": self.publisher_role_arn,
            "encrypted": False,
            "required_tags": dict(self.required_tags),
        }


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
        identity = self.aws("sts", "get-caller-identity")
        role_name = self.target.publisher_role_arn.rsplit("/", 1)[1]
        expected = f"arn:aws:sts::{self.target.account_id}:assumed-role/{role_name}/"
        if not str(identity.get("Arn", "")).startswith(expected):
            credentials = self.aws("sts", "assume-role", "--role-arn", self.target.publisher_role_arn,
                                   "--role-session-name", "image-publication", "--duration-seconds", "3600")["Credentials"]
        else:
            # The CLI and Rust SDK resolve ambient profiles differently. Export
            # every existing publisher session so both use the verified login.
            credentials = self.aws("configure", "export-credentials", "--format", "process")
        self.environment.update({
            "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
            "AWS_SESSION_TOKEN": credentials["SessionToken"],
        })
        self.environment.pop("AWS_PROFILE", None)
        self.environment.pop("AWS_DEFAULT_PROFILE", None)
        self.profile = None
        identity = self.aws("sts", "get-caller-identity")
        require(identity.get("Account") == self.target.account_id and str(identity.get("Arn", "")).startswith(expected),
                "AWS session is not the publisher role declared by the installation")

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
            {"Name": "tag:runs-on-installation", "Values": [self.target.name]},
        ]
        snapshots = self.aws("ec2", "describe-snapshots", "--owner-ids", self.target.account_id,
                             "--filters", json.dumps(filters))["Snapshots"]
        images = self.aws("ec2", "describe-images", "--owners", self.target.account_id,
                          "--filters", json.dumps(filters))["Images"]
        return [s["SnapshotId"] for s in snapshots], [i["ImageId"] for i in images]

    def versions(self) -> list[dict[str, Any]]:
        filters = [
            {"Name": "name", "Values": [f"{IMAGE_FAMILY}-*"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": f"tag:{FAMILY_TAG}", "Values": [IMAGE_FAMILY]},
            {"Name": f"tag:{NAME_TAG}", "Values": [IMAGE_FAMILY]},
            *[{"Name": f"tag:{key}", "Values": [value]} for key, value in self.target.required_tags],
        ]
        return self.aws("ec2", "describe-images", "--owners", self.target.account_id,
                        "--filters", json.dumps(filters))["Images"]

    def retired_snapshots(self) -> list[dict[str, Any]]:
        filters = [
            {"Name": f"tag:{RETIRED_TAG}", "Values": ["true"]},
            {"Name": f"tag:{FAMILY_TAG}", "Values": [IMAGE_FAMILY]},
            {"Name": f"tag:{NAME_TAG}", "Values": [IMAGE_FAMILY]},
            *[{"Name": f"tag:{key}", "Values": [value]} for key, value in self.target.required_tags],
        ]
        return self.aws("ec2", "describe-snapshots", "--owner-ids", self.target.account_id,
                        "--filters", json.dumps(filters))["Snapshots"]

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
    require(resource.get("OwnerId") == target.account_id, "Refusing a resource owned by another AWS account")
    require(all(tags.get(key) == value for key, value in target.required_tags)
            and tags.get(PUBLICATION_TAG) == publication_id and tags.get(DIGEST_TAG) == digest,
            "Resource ownership or artifact digest does not match the publication record")


@dataclass(frozen=True)
class PublishedVersion:
    image_id: str
    snapshot_id: str
    publication_id: str
    digest: str
    created_at: str

    @classmethod
    def parse(cls, image: dict[str, Any]) -> "PublishedVersion":
        tags = {tag["Key"]: tag["Value"] for tag in image["Tags"]}
        publication_id, digest = tags[PUBLICATION_TAG], tags[DIGEST_TAG]
        require(image["Name"] == f"{IMAGE_FAMILY}-{digest[:12]}-{publication_id[:12]}",
                "Image does not belong to the available Cobalt publication family")
        snapshots = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", [])
                     if "Ebs" in item and "SnapshotId" in item["Ebs"]]
        require(len(snapshots) == 1,
                "Published image must identify one backing snapshot")
        return cls(image["ImageId"], snapshots[0], publication_id, digest, image["CreationDate"])


def prune_images(target: PublishingTarget, profile: str | None = None, cloud: Cloud | None = None) -> None:
    cloud = cloud or Cloud(target, profile)
    cloud.authenticate()
    versions = sorted((PublishedVersion.parse(image) for image in cloud.versions()),
                      key=lambda version: (version.created_at, version.image_id), reverse=True)
    retired = versions[1:]
    retained_snapshot = versions[0].snapshot_id if versions else None
    # Check every selected snapshot before marking or deleting any publication.
    for version in versions:
        snapshot = cloud.snapshot(version.snapshot_id)
        require(snapshot is not None, "A published image is missing its backing snapshot")
        owned(snapshot, target, version.publication_id, version.digest)
        tags = {tag["Key"]: tag["Value"] for tag in snapshot.get("Tags", [])}
        require(tags.get(FAMILY_TAG) == IMAGE_FAMILY and tags.get(NAME_TAG) == IMAGE_FAMILY,
                "Snapshot does not have the exact published image name")
        require(snapshot.get("State") == "completed", "Published snapshot is not completed")
    for version in retired:
        require(version.snapshot_id != retained_snapshot,
                "An old image shares a snapshot with a retained image")
    pending_snapshots = cloud.retired_snapshots()
    for snapshot in pending_snapshots:
        require(snapshot["SnapshotId"] != retained_snapshot,
                "Refusing to delete a retained snapshot")
    for version in retired:
        # Persist deletion intent in AWS before deregistration, so a later run can
        # find and retry a snapshot deletion even after its image is gone.
        cloud.mark_retired(version.snapshot_id)
        cloud.deregister(version.image_id)
        cloud.delete_snapshot(version.snapshot_id)
        print(f"Retired {version.image_id} and {version.snapshot_id}")
    retired_snapshot_ids = {version.snapshot_id for version in retired}
    for snapshot in pending_snapshots:
        snapshot_id = snapshot["SnapshotId"]
        if snapshot_id in retired_snapshot_ids:
            continue
        cloud.delete_snapshot(snapshot_id)
        print(f"Deleted retired snapshot {snapshot_id}")


def parse_record(path: Path, target: PublishingTarget) -> dict[str, Any]:
    record = read_json(path)
    require(record.get("target") == target.identity(), "Publication record belongs to a different publishing target")
    require(isinstance(record.get("publication_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", record["publication_id"]) is not None,
            "Publication record has an invalid publication ID")
    artifact = record.get("artifact")
    require(isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is not None,
            "Publication record has an invalid artifact digest")
    return record


def cleanup_record(path: Path, target: PublishingTarget, profile: str | None = None,
                   cloud: Cloud | None = None) -> dict[str, Any]:
    record = parse_record(path, target)
    cloud = cloud or Cloud(target, profile)
    cloud.authenticate()
    snapshot_ids, image_ids = cloud.discover(record["publication_id"])
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
            owned(resource, target, record["publication_id"], record["artifact"]["sha256"])
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


def wait_snapshot(cloud: Cloud, snapshot_id: str, target: PublishingTarget,
                  publication_id: str, digest: str) -> dict[str, Any]:
    for attempt in range(120):
        snapshot = cloud.snapshot(snapshot_id)
        if snapshot:
            owned(snapshot, target, publication_id, digest)
            require(snapshot.get("Encrypted") is False and not snapshot.get("KmsKeyId"),
                    "Published snapshot must be unencrypted; EBS encryption by default may have changed during upload")
            if snapshot.get("State") == "completed":
                return snapshot
            require(snapshot.get("State") != "error", f"Snapshot {snapshot_id} entered an error state")
        if attempt != 119:
            time.sleep(5)
    raise RuntimeError(f"Snapshot {snapshot_id} did not complete within ten minutes")


def publish_image(build: BuiltImage, target: PublishingTarget, output: Path,
                  profile: str | None = None, cloud: Cloud | None = None) -> dict[str, Any]:
    volume_gib = build.disk_size_bytes // 1024**3
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    record_path = output / "published-image.json"
    require(not record_path.exists(),
            "Output already contains a publication; use a new directory or clean up the recorded publication")
    publication_id = uuid.uuid4().hex
    name = f"{IMAGE_FAMILY}-{build.disk_sha256[:12]}-{publication_id[:12]}"
    tags = {**dict(target.required_tags), PUBLICATION_TAG: publication_id,
            DIGEST_TAG: build.disk_sha256, FAMILY_TAG: IMAGE_FAMILY, NAME_TAG: IMAGE_FAMILY}
    record = {
        "status": "pending", "image": IMAGE_FAMILY, "name": name,
        "publication_id": publication_id, "target": target.identity(),
        "artifact": {"sha256": build.disk_sha256}, "snapshot_id": None, "ami_id": None,
    }
    cloud = cloud or Cloud(target, profile)
    cloud.authenticate()
    cloud.require_unencrypted_snapshots()
    write_json(record_path, record)
    try:
        snapshot_id = cloud.upload(build.disk_path, tags, volume_gib, output)
        record["snapshot_id"] = snapshot_id
        write_json(record_path, record)
        cloud.authenticate()
        snapshot = wait_snapshot(cloud, snapshot_id, target, publication_id, build.disk_sha256)
        require(snapshot["VolumeSize"] == volume_gib, "Published snapshot has an unexpected volume size")
        image_id = cloud.register(name, snapshot_id, volume_gib, tags)
        record["ami_id"] = image_id
        write_json(record_path, record)
        for attempt in range(60):
            image = cloud.image(image_id)
            if image:
                owned(image, target, publication_id, build.disk_sha256)
                require(image.get("Architecture") == "x86_64"
                        and image.get("BootMode") == "uefi"
                        and image.get("EnaSupport") is True,
                        "Registered image does not match the build's architecture and boot requirements")
                sources = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", [])
                           if "Ebs" in item and "SnapshotId" in item["Ebs"]]
                require(sources == [snapshot_id], "Registered image references a different disk")
                require(all(item["Ebs"].get("Encrypted") is False for item in image["BlockDeviceMappings"] if "Ebs" in item),
                        "Registered image must use an unencrypted snapshot")
                if image.get("State") == "available":
                    break
                require(image.get("State") not in {"failed", "error", "deregistered"}, "Registered image is unavailable")
            if attempt != 59:
                time.sleep(5)
        else:
            raise RuntimeError(f"Image {image_id} did not become available within five minutes")
        record["status"] = "available"
        write_json(record_path, record)
    except (Exception, KeyboardInterrupt) as error:
        record["status"] = "cleanup-needed"
        record["error"] = str(error)
        write_json(record_path, record)
        try:
            cleanup_record(record_path, target, profile, cloud)
        except (Exception, KeyboardInterrupt) as cleanup_error:
            record = read_json(record_path)
            record["status"] = "cleanup-needed"
            record["cleanup_error"] = str(cleanup_error)
            write_json(record_path, record)
            raise RuntimeError(f"Publication failed: {error}. Cleanup needs attention: {cleanup_error}. "
                               f"Recovery record: {record_path}") from error
        raise RuntimeError(f"Publication failed and its resources were cleaned up: {error}. Record: {record_path}") from error

    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--build", type=Path, help="Built image manifest to publish without rebuilding")
    operation.add_argument("--cleanup", type=Path, help="Published image or partial publication record to delete")
    operation.add_argument("--prune", action="store_true",
                           help="Keep the newest publication with the exact Cobalt Name tag and retry retirement")
    parser.add_argument("--target", type=Path, required=True, help="Installation publishing.json contract")
    parser.add_argument("--output", type=Path, help="New directory for publication records and upload log")
    parser.add_argument("--profile", help="Existing AWS profile; omitted uses ambient credentials such as GitHub OIDC")
    args = parser.parse_args(argv)
    if args.build and not args.output:
        parser.error("--build requires --output")
    if not args.build and args.output:
        parser.error("--output is only used with --build")
    os.umask(0o077)
    def terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt("Publication interrupted")
    signal.signal(signal.SIGTERM, terminate)
    try:
        target = PublishingTarget.load(args.target)
        if args.cleanup:
            cleanup_record(args.cleanup, target, args.profile)
            print(f"Deleted publication recorded in {args.cleanup}")
        elif args.prune:
            prune_images(target, args.profile)
        else:
            publish_image(BuiltImage.load(args.build), target, args.output, args.profile)
            print(args.output / "published-image.json")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
