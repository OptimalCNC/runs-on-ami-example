"""Retain the newest matching publication and retry unfinished retirement."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import (
    Cloud, DIGEST_TAG, FAMILY_TAG, ImageName, NAME_TAG, PUBLICATION_TAG,
    PublishingConfig, owned, require, run_cli,
)


@dataclass(frozen=True)
class PublishedVersion:
    image_id: str
    snapshot_id: str
    publication_id: str
    digest: str
    created_at: str

    @classmethod
    def parse(cls, image: dict[str, Any], name: ImageName) -> "PublishedVersion":
        tags = {tag["Key"]: tag["Value"] for tag in image["Tags"]}
        publication_id, digest = tags[PUBLICATION_TAG], tags[DIGEST_TAG]
        require(image["Name"] == f"{name.value}-{digest[:12]}-{publication_id[:12]}",
                "Image does not belong to the requested publication family")
        snapshots = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", [])
                     if "Ebs" in item and "SnapshotId" in item["Ebs"]]
        require(len(snapshots) == 1,
                "Published image must identify one backing snapshot")
        return cls(image["ImageId"], snapshots[0], publication_id, digest, image["CreationDate"])


def prune_images(config: PublishingConfig, profile: str | None = None,
                 cloud: Cloud | None = None) -> None:
    name, target = config.name, config.target
    cloud = cloud or Cloud(target, profile)
    cloud.authenticate()
    versions = sorted((PublishedVersion.parse(image, name) for image in cloud.versions(name)),
                      key=lambda version: (version.created_at, version.image_id), reverse=True)
    retired = versions[1:]
    retained_snapshot = versions[0].snapshot_id if versions else None
    # Check every selected snapshot before marking or deleting any publication.
    for version in versions:
        snapshot = cloud.snapshot(version.snapshot_id)
        require(snapshot is not None, "A published image is missing its backing snapshot")
        owned(snapshot, target, version.publication_id, version.digest)
        tags = {tag["Key"]: tag["Value"] for tag in snapshot.get("Tags", [])}
        require(tags.get(FAMILY_TAG) == name.value and tags.get(NAME_TAG) == name.value,
                "Snapshot does not have the exact published image name")
        require(snapshot.get("State") == "completed", "Published snapshot is not completed")
    for version in retired:
        require(version.snapshot_id != retained_snapshot,
                "An old image shares a snapshot with a retained image")
    pending_snapshots = cloud.retired_snapshots(name)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Publishing JSON configuration")
    parser.add_argument("--profile", help="AWS profile; omitted uses ambient credentials")
    args = parser.parse_args(argv)
    return run_cli(lambda: prune_images(PublishingConfig.load(args.config), args.profile))


if __name__ == "__main__":
    raise SystemExit(main())
