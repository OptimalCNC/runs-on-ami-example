#!/usr/bin/env python3
"""Publish an existing raw image, or delete one recorded publication."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Callable
import uuid

import yaml

from contracts import BuiltImage, sha256, write_yaml


PUBLICATION_TAG = "image-publication-id"
DIGEST_TAG = "image-sha256"
RECIPE_TAG = "image-recipe-id"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text())
    require(isinstance(value, dict), f"{path} must contain a YAML mapping")
    return value


@dataclass(frozen=True)
class PublishingTarget:
    name: str
    account_id: str
    region: str
    publisher_role_arn: str
    kms_key_arn: str
    required_tags: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, path: Path) -> "PublishingTarget":
        data = load_yaml(path)
        require(data.get("kind") == "ami-publishing-target" and data.get("schema_version") == 1,
                "Expected an ami-publishing-target contract with schema_version 1")
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
        destination = data.get("destination")
        require(isinstance(destination, dict), "Publishing target is missing destination")
        require(destination.get("type") == "ec2-ami" and destination.get("upload_method") == "ebs-direct-api"
                and destination.get("disk_format") == "raw" and destination.get("encrypted") is True,
                "Publishing requires an encrypted raw-disk EBS direct API destination")
        key = destination.get("kms_key_arn")
        require(isinstance(key, str) and re.fullmatch(
            rf"arn:aws:kms:{region}:{account}:key/[a-zA-Z0-9-]+", key) is not None,
            "Image encryption key must belong to the publishing account and region")
        tags = destination.get("required_tags")
        require(isinstance(tags, dict) and 1 <= len(tags) <= 47,
                "Publishing target must specify between 1 and 47 ownership tags")
        require(all(isinstance(k, str) and 1 <= len(k) <= 128 and not k.lower().startswith("aws:")
                    and ",Value=" not in k and isinstance(v, str) and len(v) <= 256 for k, v in tags.items()),
                "Publishing target contains invalid AWS tags")
        require(tags.get("runs-on-installation") == name, "Publishing ownership tag must match installation name")
        require(not {PUBLICATION_TAG, DIGEST_TAG, RECIPE_TAG}.intersection(tags),
                "Publishing target cannot override artifact or publication identity tags")
        return cls(name, account, region, role, key, tuple(sorted(tags.items())))

    def identity(self) -> dict[str, Any]:
        return {
            "name": self.name, "account_id": self.account_id, "region": self.region,
            "publisher_role_arn": self.publisher_role_arn, "kms_key_arn": self.kms_key_arn,
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
        credentials = None
        if not str(identity.get("Arn", "")).startswith(expected):
            credentials = self.aws("sts", "assume-role", "--role-arn", self.target.publisher_role_arn,
                                   "--role-session-name", "image-publication", "--duration-seconds", "3600")["Credentials"]
        elif self.profile:
            # The CLI's explicit profile overrides ambient keys. Normalize its
            # resolved credentials so the Rust SDK uses the same verified login.
            credentials = self.aws("configure", "export-credentials", "--format", "process")
        if credentials:
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

    def upload(self, disk: Path, tags: dict[str, str], volume_gib: int, output: Path,
               on_snapshot: Callable[[str], None]) -> str:
        command = ["coldsnap", "--region", self.target.region]
        if self.profile:
            command += ["--profile", self.profile]
        command += ["upload", str(disk), "--kms-key-id", self.target.kms_key_arn,
                    "--volume-size", str(volume_gib), "--description", f"Image publication {tags[PUBLICATION_TAG]}",
                    "--no-progress"]
        for key, value in sorted(tags.items()):
            command += ["--tag", f"Key={key},Value={value}"]
        # Encrypted disks require uploading zero blocks too. Coldsnap's omit
        # mode does not preserve their read semantics under encryption.
        stdout_path = output / "upload.stdout"
        with (output / "upload.log").open("w") as log, stdout_path.open("w") as stdout:
            process = subprocess.Popen(command, env=self.environment, stdout=stdout, stderr=log, text=True)
            observed = False
            deadline = time.monotonic() + 3000
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Snapshot upload exceeded the 50-minute session limit")
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    if not observed:
                        snapshots, _ = self.discover(tags[PUBLICATION_TAG])
                        require(len(snapshots) <= 1, "Multiple snapshots unexpectedly share this publication ID")
                        if snapshots:
                            on_snapshot(snapshots[0])
                            observed = True
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        if process.returncode:
            raise RuntimeError(f"coldsnap upload failed; see {output / 'upload.log'}")
        snapshot_id = stdout_path.read_text().strip()
        require(re.fullmatch(r"snap-[0-9a-f]+", snapshot_id) is not None,
                "coldsnap did not return exactly one snapshot ID")
        on_snapshot(snapshot_id)
        return snapshot_id

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

    def register(self, name: str, snapshot_id: str, compatibility: dict[str, Any], tags: dict[str, str]) -> str:
        parameters = {
            "Name": name, "Architecture": compatibility["architecture"], "VirtualizationType": "hvm",
            "BootMode": compatibility["boot_mode"], "EnaSupport": compatibility["ena_support"],
            "RootDeviceName": "/dev/sda1",
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {
                "SnapshotId": snapshot_id, "DeleteOnTermination": True, "VolumeType": "gp3",
                "VolumeSize": compatibility["minimum_root_volume_gib"],
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


def parse_record(path: Path, target: PublishingTarget) -> dict[str, Any]:
    record = load_yaml(path)
    require(record.get("kind") in {"image-publication", "published-image"} and record.get("schema_version") == 1,
            "Expected a publication state or published image record with schema_version 1")
    require(record.get("target") == target.identity(), "Publication record belongs to a different publishing target")
    require(isinstance(record.get("publication_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", record["publication_id"]) is not None,
            "Publication record has an invalid publication ID")
    artifact = record.get("artifact")
    require(isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is not None,
            "Publication record has an invalid artifact digest")
    for key, prefix in (("snapshot_id", "snap"), ("ami_id", "ami")):
        require(record.get(key) is None or isinstance(record[key], str)
                and re.fullmatch(rf"{prefix}-[0-9a-f]+", record[key]) is not None,
                f"Publication record has an invalid {key}")
    for key, prefix in (("discovered_snapshot_ids", "snap"), ("discovered_ami_ids", "ami")):
        values = record.get(key, [])
        require(isinstance(values, list) and all(isinstance(value, str)
                and re.fullmatch(rf"{prefix}-[0-9a-f]+", value) is not None for value in values),
                f"Publication record has invalid {key}")
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
    write_yaml(path, record)
    if not snapshot_ids and record["status"] in {"uploading", "cleanup-needed"}:
        raise RuntimeError("No snapshot ID is visible yet; retain this record and retry cleanup after EBS discovery catches up")
    # Read and check the complete deletion set before performing any mutation.
    snapshots = {sid: cloud.snapshot(sid) for sid in snapshot_ids}
    images = {iid: cloud.image(iid) for iid in image_ids}
    for resource in [*snapshots.values(), *images.values()]:
        if resource:
            owned(resource, target, record["publication_id"], record["artifact"]["sha256"])
    for snapshot in snapshots.values():
        if snapshot:
            require(snapshot.get("Encrypted") is True and snapshot.get("KmsKeyId") == target.kms_key_arn,
                    "Refusing to delete a snapshot encrypted outside the publishing target")
    for image in images.values():
        if image:
            sources = {mapping["Ebs"]["SnapshotId"] for mapping in image.get("BlockDeviceMappings", [])
                       if "Ebs" in mapping and "SnapshotId" in mapping["Ebs"]}
            require(sources and sources.issubset(snapshots), "Image refers to snapshots outside this publication")
    record["status"] = "deleting"
    write_yaml(path, record)
    for image_id, image in images.items():
        if image and image.get("State") != "deregistered":
            cloud.deregister(image_id)
    for snapshot_id, snapshot in snapshots.items():
        if snapshot:
            cloud.delete_snapshot(snapshot_id)
    record["status"] = "deleted"
    record["deleted_at"] = datetime.now(timezone.utc).isoformat()
    write_yaml(path, record)
    sibling = path.parent / ("published-image.yaml" if path.name == "publication-state.yaml" else "publication-state.yaml")
    if sibling != path and sibling.exists():
        other = parse_record(sibling, target)
        if other["publication_id"] == record["publication_id"] and other["artifact"] == record["artifact"]:
            other.update(status="deleted", deleted_at=record["deleted_at"])
            write_yaml(sibling, other)
    return record


def wait_snapshot(cloud: Cloud, snapshot_id: str, target: PublishingTarget,
                  publication_id: str, digest: str) -> dict[str, Any]:
    for attempt in range(120):
        snapshot = cloud.snapshot(snapshot_id)
        if snapshot:
            owned(snapshot, target, publication_id, digest)
            require(snapshot.get("Encrypted") is True and snapshot.get("KmsKeyId") == target.kms_key_arn,
                    "Published snapshot does not use the target encryption key")
            if snapshot.get("State") == "completed":
                return snapshot
            require(snapshot.get("State") != "error", f"Snapshot {snapshot_id} entered an error state")
        if attempt != 119:
            time.sleep(5)
    raise RuntimeError(f"Snapshot {snapshot_id} did not complete within ten minutes")


def publish_image(build: BuiltImage, target: PublishingTarget, output: Path, name: str | None = None,
                  profile: str | None = None, cloud: Cloud | None = None) -> dict[str, Any]:
    compatibility = asdict(build.compatibility)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path, published_path = output / "publication-state.yaml", output / "published-image.yaml"
    require(not state_path.exists() and not published_path.exists(),
            "Output already contains a publication; use a new directory or clean up the recorded publication")
    publication_id = uuid.uuid4().hex
    name = name or f"{target.name}-{build.disk_sha256[:12]}-{publication_id[:12]}"
    require(re.fullmatch(r"[A-Za-z0-9()./_-]{3,128}", name) is not None, "AMI name must be 3 to 128 AWS-compatible characters")
    tags = {**dict(target.required_tags), PUBLICATION_TAG: publication_id,
            DIGEST_TAG: build.disk_sha256, RECIPE_TAG: build.recipe_id}
    record = {
        "schema_version": 1, "kind": "image-publication", "status": "uploading",
        "publication_id": publication_id, "target": target.identity(),
        "account_id": target.account_id, "region": target.region,
        "artifact": {"sha256": build.disk_sha256, "size_bytes": build.disk_size_bytes, "recipe_id": build.recipe_id},
        "compatibility": compatibility, "snapshot_id": None, "ami_id": None,
        "ami_name": name, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    cloud = cloud or Cloud(target, profile)
    cloud.authenticate()
    write_yaml(state_path, record)
    def remember_snapshot(snapshot_id: str) -> None:
        require(re.fullmatch(r"snap-[0-9a-f]+", snapshot_id) is not None, "Invalid uploaded snapshot identity")
        require(record["snapshot_id"] in {None, snapshot_id}, "Upload returned inconsistent snapshot identities")
        record["snapshot_id"] = snapshot_id
        write_yaml(state_path, record)

    try:
        snapshot_id = cloud.upload(build.disk_path, tags, compatibility["minimum_root_volume_gib"], output, remember_snapshot)
        record.update(snapshot_id=snapshot_id, status="completing-snapshot")
        write_yaml(state_path, record)
        cloud.authenticate()
        snapshot = wait_snapshot(cloud, snapshot_id, target, publication_id, build.disk_sha256)
        require(snapshot["VolumeSize"] == compatibility["minimum_root_volume_gib"], "Published snapshot has an unexpected volume size")
        require(build.disk_path.stat().st_size == build.disk_size_bytes and sha256(build.disk_path) == build.disk_sha256,
                "Built disk changed during publication; refusing to register it")
        record["status"] = "registering-image"
        write_yaml(state_path, record)
        image_id = cloud.register(name, snapshot_id, compatibility, tags)
        require(re.fullmatch(r"ami-[0-9a-f]+", image_id) is not None, "AWS returned an invalid AMI identity")
        record.update(ami_id=image_id, status="checking-image")
        write_yaml(state_path, record)
        for attempt in range(60):
            image = cloud.image(image_id)
            if image:
                owned(image, target, publication_id, build.disk_sha256)
                require(image.get("Architecture") == compatibility["architecture"]
                        and image.get("BootMode") == compatibility["boot_mode"]
                        and image.get("EnaSupport") is True,
                        "Registered image does not match the build's architecture and boot requirements")
                sources = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", [])
                           if "Ebs" in item and "SnapshotId" in item["Ebs"]]
                require(sources == [snapshot_id], "Registered image references a different disk")
                if image.get("State") == "available":
                    break
                require(image.get("State") not in {"failed", "error", "deregistered"}, "Registered image is unavailable")
            if attempt != 59:
                time.sleep(5)
        else:
            raise RuntimeError(f"Image {image_id} did not become available within five minutes")
        record["status"] = "available"
        write_yaml(state_path, record)
        result = {**record, "kind": "published-image"}
        write_yaml(published_path, result)
        return result
    except (Exception, KeyboardInterrupt) as error:
        record["status"] = "cleanup-needed"
        record["error"] = str(error)
        write_yaml(state_path, record)
        try:
            cleanup_record(state_path, target, profile, cloud)
        except (Exception, KeyboardInterrupt) as cleanup_error:
            record = load_yaml(state_path)
            record["status"] = "cleanup-needed"
            record["cleanup_error"] = str(cleanup_error)
            write_yaml(state_path, record)
            raise RuntimeError(f"Publication failed: {error}. Cleanup needs attention: {cleanup_error}. "
                               f"Recovery record: {state_path}") from error
        raise RuntimeError(f"Publication failed and its resources were cleaned up: {error}. Record: {state_path}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--build", type=Path, help="Built image manifest; performs no build or validation")
    operation.add_argument("--cleanup", type=Path, help="Published image or partial publication record to delete")
    parser.add_argument("--target", type=Path, required=True, help="Installation publishing.yaml contract")
    parser.add_argument("--output", type=Path, help="New directory for publication records and upload log")
    parser.add_argument("--profile", help="Existing AWS profile; omitted uses ambient credentials such as GitHub OIDC")
    parser.add_argument("--name", help="Optional AMI name")
    args = parser.parse_args(argv)
    if args.build and not args.output:
        parser.error("--build requires --output")
    if args.cleanup and (args.output or args.name):
        parser.error("--cleanup does not accept --output or --name")
    os.umask(0o077)
    def terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt("Publication interrupted")
    signal.signal(signal.SIGTERM, terminate)
    try:
        target = PublishingTarget.load(args.target)
        if args.cleanup:
            cleanup_record(args.cleanup, target, args.profile)
            print(f"Deleted publication recorded in {args.cleanup}")
        else:
            publish_image(BuiltImage.load(args.build), target, args.output, args.name, args.profile)
            print(args.output / "published-image.yaml")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
