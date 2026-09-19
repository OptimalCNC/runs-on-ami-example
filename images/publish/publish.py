"""Upload a raw disk and register an AMI."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import time
from typing import Any
import uuid

from .core import (
    Cloud, DIGEST_TAG, FAMILY_TAG, NAME_TAG, PUBLICATION_TAG, PublishingConfig,
    PublishingTarget, cleanup_record, owned, read_json, require, run_cli, write_json,
)


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


def publish_image(disk: Path, config: PublishingConfig, output: Path,
                  profile: str | None = None, cloud: Cloud | None = None) -> dict[str, Any]:
    name, target = config.name, config.target
    disk = disk.resolve(strict=True)
    require(disk.is_file(), "Raw disk must be a regular file")
    size = disk.stat().st_size
    require(size > 0, "Raw disk must not be empty")
    volume_gib = (size + 1024**3 - 1) // 1024**3
    with disk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    record_path = output / "published-image.json"
    require(not record_path.exists(),
            "Output already contains a publication; use a new directory or clean up the recorded publication")
    publication_id = uuid.uuid4().hex
    ami_name = f"{name.value}-{digest[:12]}-{publication_id[:12]}"
    tags = {**dict(target.required_tags), PUBLICATION_TAG: publication_id,
            DIGEST_TAG: digest, FAMILY_TAG: name.value, NAME_TAG: name.value}
    record = {
        "status": "pending", "image": name.value, "name": ami_name,
        "publication_id": publication_id, "target": target.identity(),
        "artifact": {"sha256": digest}, "snapshot_id": None, "ami_id": None,
    }
    cloud = cloud or Cloud(target, profile)
    cloud.authenticate()
    cloud.require_unencrypted_snapshots()
    write_json(record_path, record)
    try:
        snapshot_id = cloud.upload(disk, tags, volume_gib, output)
        record["snapshot_id"] = snapshot_id
        write_json(record_path, record)
        cloud.authenticate()
        snapshot = wait_snapshot(cloud, snapshot_id, target, publication_id, digest)
        require(snapshot["VolumeSize"] == volume_gib, "Published snapshot has an unexpected volume size")
        image_id = cloud.register(ami_name, snapshot_id, volume_gib, tags)
        record["ami_id"] = image_id
        write_json(record_path, record)
        for attempt in range(60):
            image = cloud.image(image_id)
            if image:
                owned(image, target, publication_id, digest)
                require(image.get("Architecture") == "x86_64"
                        and image.get("BootMode") == "uefi"
                        and image.get("EnaSupport") is True,
                        "Registered image does not match the supported architecture and boot requirements")
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
            cleanup_record(record_path, profile, cloud)
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
    parser.add_argument("--config", type=Path, required=True, help="Publishing JSON configuration")
    parser.add_argument("--image", type=Path, required=True, help="Raw disk to publish")
    parser.add_argument("--output", type=Path, required=True, help="Directory for publication records and upload log")
    parser.add_argument("--profile", help="AWS profile; omitted uses ambient credentials")
    args = parser.parse_args(argv)

    def run() -> None:
        publish_image(args.image, PublishingConfig.load(args.config), args.output, args.profile)
        print(args.output / "published-image.json")

    return run_cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
