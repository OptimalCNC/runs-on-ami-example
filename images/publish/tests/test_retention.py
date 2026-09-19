"""Exercise retention against an in-memory EC2 resource inventory."""
from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from publish.core import (
    AwsError, Cloud, ImageName, PublishingConfig, PublishingTarget, cleanup_record, read_json, write_json,
)
from publish.prune import prune_images
from publish.publish import publish_image


TARGET = PublishingTarget("123456789012", "us-east-1",
                          (("project", "disk-images"), ("environment", "testing")))
NAME = ImageName("debian-worker")
CONFIG = PublishingConfig(NAME, TARGET)


def tags(resource):
    return {tag["Key"]: tag["Value"] for tag in resource.get("Tags", [])}


def version(number, name="debian-worker", *, family="debian-worker", target=TARGET):
    publication, digest = f"{number:02x}" * 16, f"{number:02x}" * 32
    identity = {
        "OwnerId": target.account_id,
        "Tags": [{"Key": key, "Value": value} for key, value in {
            **dict(target.required_tags), "image-family": family,
            "Name": name, "image-publication-id": publication, "image-sha256": digest,
        }.items()],
    }
    snapshot = dict(deepcopy(identity), SnapshotId=f"snap-{number:x}", State="completed", Encrypted=False)
    image = dict(deepcopy(identity), ImageId=f"ami-{number:x}", State="available",
                 Name=f"{family}-{digest[:12]}-{publication[:12]}",
                 CreationDate=f"2026-09-{number:02d}T00:00:00.000Z",
                 BlockDeviceMappings=[{"Ebs": {"SnapshotId": snapshot["SnapshotId"], "Encrypted": False}}])
    return image, snapshot


class MemoryCloud(Cloud):
    def __init__(self, versions=(), target=TARGET):
        super().__init__(target)
        self.images = {image["ImageId"]: image for image, _ in versions}
        self.snapshots = {snapshot["SnapshotId"]: snapshot for _, snapshot in versions}
        self.mutations = []
        self.fail = set()

    def authenticate(self):
        pass

    def require_unencrypted_snapshots(self):
        pass

    def aws(self, service, operation, *arguments):
        args = dict(zip(arguments[::2], arguments[1::2]))
        if operation in {"describe-images", "describe-snapshots"}:
            images = operation == "describe-images"
            inventory = self.images if images else self.snapshots
            id_arg = "--image-ids" if images else "--snapshot-ids"
            owner_arg = "--owners" if images else "--owner-ids"
            values = list(inventory.values())
            if id_arg in args:
                values = [inventory[args[id_arg]]] if args[id_arg] in inventory else []
            if owner_arg in args:
                values = [item for item in values if item["OwnerId"] == args[owner_arg]]
            for selector in json.loads(args.get("--filters", "[]")):
                key = selector["Name"]
                if key.startswith("tag:"):
                    values = [item for item in values if any(
                        fnmatchcase(tags(item).get(key[4:], ""), value) for value in selector["Values"])]
                elif key == "state":
                    values = [item for item in values if item["State"] in selector["Values"]]
                elif key == "name":
                    prefix = selector["Values"][0].removesuffix("*")
                    values = [item for item in values if item["Name"].startswith(prefix)]
                else:
                    raise AssertionError(f"Unexpected EC2 filter: {key}")
            return {"Images" if images else "Snapshots": deepcopy(values)}
        if operation in self.fail:
            raise AwsError(operation, "AccessDenied")
        if operation == "create-tags":
            snapshot_id = args["--resources"]
            self.mutations.append((operation, snapshot_id))
            snapshot = self.snapshots[snapshot_id]
            merged = tags(snapshot) | {tag["Key"]: tag["Value"] for tag in json.loads(args["--tags"])}
            snapshot["Tags"] = [{"Key": key, "Value": value} for key, value in merged.items()]
        elif operation == "deregister-image":
            image_id = args["--image-id"]
            self.mutations.append((operation, image_id))
            self.images.pop(image_id, None)
        elif operation == "delete-snapshot":
            snapshot_id = args["--snapshot-id"]
            if any(mapping["Ebs"]["SnapshotId"] == snapshot_id
                   for image in self.images.values() for mapping in image["BlockDeviceMappings"]):
                raise AssertionError("Snapshot deleted while an image still references it")
            self.mutations.append((operation, snapshot_id))
            self.snapshots.pop(snapshot_id, None)
        elif operation == "register-image":
            image = json.loads(args["--cli-input-json"])
            image["Tags"] = image.pop("TagSpecifications")[0]["Tags"]
            image.update(ImageId="ami-ff", OwnerId=self.target.account_id, State="available",
                         CreationDate="2026-09-30T00:00:00.000Z")
            image["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] = False
            self.images["ami-ff"] = image
            return {"ImageId": "ami-ff"}
        else:
            raise AssertionError(f"Unexpected AWS operation: {operation}")
        return {}

    def upload(self, disk, identity, volume_gib, output):
        self.snapshots["snap-ff"] = {
            "SnapshotId": "snap-ff", "OwnerId": self.target.account_id,
            "State": "completed", "Encrypted": False, "VolumeSize": volume_gib,
            "Tags": [{"Key": key, "Value": value} for key, value in identity.items()],
        }
        if "upload" in self.fail:
            raise RuntimeError("Upload interrupted before returning its snapshot ID")
        return "snap-ff"


class RetentionTests(unittest.TestCase):
    def test_keeps_newest_and_leaves_other_resources_alone(self):
        resources = [version(n) for n in (3, 1, 4, 2, 5, 6, 7, 8, 9)]
        resources[4][0]["OwnerId"] = "999999999999"
        resources[5][0]["Tags"][0]["Value"] = "another-project"
        resources[6][0]["Tags"][2]["Value"] = "another-family"
        resources[7][0]["State"] = "pending"
        resources[8][0]["Name"] = "another-family-image"
        cloud = MemoryCloud(resources)
        prune_images(CONFIG, cloud=cloud)
        self.assertEqual(set(cloud.images), {f"ami-{n:x}" for n in range(4, 10)})
        self.assertEqual(set(cloud.snapshots), {f"snap-{n:x}" for n in range(4, 10)})
        for number in (1, 2, 3):
            operations = [event for event in cloud.mutations if event[1] in {f"ami-{number}", f"snap-{number}"}]
            self.assertEqual(operations, [
                ("create-tags", f"snap-{number}"), ("deregister-image", f"ami-{number}"),
                ("delete-snapshot", f"snap-{number}"),
            ])

    def test_does_not_retire_the_only_version(self):
        for count in range(2):
            with self.subTest(count=count):
                cloud = MemoryCloud([version(n) for n in range(1, count + 1)])
                prune_images(CONFIG, cloud=cloud)
                self.assertEqual(cloud.mutations, [])

    def test_pruning_one_caller_selected_name_leaves_another_family_untouched(self):
        cloud = MemoryCloud([version(1), version(2),
                             version(3, "fedora-builder", family="fedora-builder"),
                             version(4, "fedora-builder", family="fedora-builder")])
        prune_images(PublishingConfig(ImageName("fedora-builder"), TARGET), cloud=cloud)
        self.assertEqual(set(cloud.images), {"ami-1", "ami-2", "ami-4"})
        self.assertEqual(set(cloud.snapshots), {"snap-1", "snap-2", "snap-4"})

    def test_all_ownership_tags_are_required_for_retention_and_discovery(self):
        foreign = PublishingTarget(TARGET.account_id, TARGET.region,
                                   (("project", "disk-images"), ("environment", "production")))
        cloud = MemoryCloud([version(1), version(2), version(3, target=foreign)])
        cloud.snapshots["snap-3"]["Tags"].append({"Key": "image-retired", "Value": "true"})
        self.assertEqual(cloud.discover("03" * 16), ([], []))
        prune_images(CONFIG, cloud=cloud)
        self.assertEqual(set(cloud.images), {"ami-2", "ami-3"})
        self.assertEqual(set(cloud.snapshots), {"snap-2", "snap-3"})

    def test_wildcards_in_ownership_values_do_not_broaden_deletion_scope(self):
        target = PublishingTarget(TARGET.account_id, TARGET.region, (("project", "disk-*"),))
        cloud = MemoryCloud([version(1, target=target), version(2, target=target), version(3)], target)
        cloud.snapshots["snap-3"]["Tags"].append({"Key": "image-retired", "Value": "true"})
        self.assertEqual(cloud.discover("03" * 16), ([], []))
        prune_images(PublishingConfig(NAME, target), cloud=cloud)
        self.assertEqual(set(cloud.images), {"ami-2", "ami-3"})
        self.assertEqual(set(cloud.snapshots), {"snap-2", "snap-3"})

    def test_name_match_is_exact_and_missing_names_are_untouched(self):
        resources = [version(1), version(2),
                     version(3, "debian-worker-debug"),
                     version(4, "debian-worker-old"),
                     version(5, "Debian-worker"), version(6)]
        for resource in resources[-1]:
            resource["Tags"] = [tag for tag in resource["Tags"] if tag["Key"] != "Name"]
        cloud = MemoryCloud(resources)
        prune_images(CONFIG, cloud=cloud)
        self.assertEqual(set(cloud.images), {f"ami-{n}" for n in range(2, 7)})
        self.assertEqual(set(cloud.snapshots), {f"snap-{n}" for n in range(2, 7)})

    def test_refuses_to_delete_a_snapshot_with_a_different_name(self):
        cloud = MemoryCloud([version(1), version(2)])
        for tag in cloud.snapshots["snap-1"]["Tags"]:
            if tag["Key"] == "Name":
                tag["Value"] = "debian-worker-debug"
        with self.assertRaisesRegex(ValueError, "exact published image name"):
            prune_images(CONFIG, cloud=cloud)
        self.assertEqual(cloud.mutations, [])

    def test_checks_retained_snapshot_before_removing_previous_version(self):
        cloud = MemoryCloud([version(1), version(2)])
        cloud.snapshots["snap-2"]["State"] = "error"
        with self.assertRaisesRegex(ValueError, "not completed"):
            prune_images(CONFIG, cloud=cloud)
        self.assertEqual(cloud.mutations, [])

    def test_checks_all_old_snapshot_ownership_before_deleting_anything(self):
        cloud = MemoryCloud([version(n) for n in range(1, 5)])
        cloud.snapshots["snap-1"]["Tags"][-1]["Value"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "ownership or artifact digest"):
            prune_images(CONFIG, cloud=cloud)
        self.assertEqual(cloud.mutations, [])

    def test_recovers_snapshot_after_image_was_deregistered(self):
        cloud = MemoryCloud([version(n) for n in range(1, 5)])
        del cloud.images["ami-4"]  # Unmarked orphan from a different interrupted upload.
        cloud.fail.add("delete-snapshot")
        with self.assertRaises(AwsError):
            prune_images(CONFIG, cloud=cloud)
        self.assertNotIn("ami-2", cloud.images)
        self.assertEqual(tags(cloud.snapshots["snap-2"])["image-retired"], "true")
        cloud.fail.clear()
        prune_images(CONFIG, cloud=cloud)
        self.assertEqual(set(cloud.snapshots), {"snap-3", "snap-4"})
        self.assertEqual(set(cloud.images), {"ami-3"})

    def test_retirement_marker_must_succeed_before_deregistering(self):
        cloud = MemoryCloud([version(n) for n in range(1, 4)])
        cloud.fail.add("create-tags")
        with self.assertRaises(AwsError):
            prune_images(CONFIG, cloud=cloud)
        self.assertEqual(cloud.mutations, [])

    def test_retained_snapshot_cannot_be_deleted_through_a_stale_marker(self):
        cloud = MemoryCloud([version(1), version(2)])
        cloud.snapshots["snap-2"]["Tags"].append({"Key": "image-retired", "Value": "true"})
        with self.assertRaisesRegex(ValueError, "retained snapshot"):
            prune_images(CONFIG, cloud=cloud)
        self.assertEqual(cloud.mutations, [])

    def test_publication_failure_only_cleans_up_its_own_resources(self):
        for operation in ("upload", "register-image"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                cloud = MemoryCloud([version(n) for n in range(1, 4)])
                cloud.fail.add(operation)
                build, output = self.build_fixture(directory)
                with self.assertRaisesRegex(RuntimeError, "resources were cleaned up"):
                    publish_image(build, CONFIG, output, cloud=cloud)
                self.assertEqual(read_json(output / "published-image.json")["status"], "deleted")
                self.assertEqual(set(cloud.images), {"ami-1", "ami-2", "ami-3"})
                self.assertEqual(set(cloud.snapshots), {"snap-1", "snap-2", "snap-3"})

    def test_successful_publication_leaves_retention_to_separate_cleanup(self):
        cloud = MemoryCloud([version(1), version(2)])
        with tempfile.TemporaryDirectory() as directory:
            build, output = self.build_fixture(directory)
            record = publish_image(build, CONFIG, output, cloud=cloud)
            self.assertEqual(record["status"], "available")
            self.assertEqual(record["artifact"]["sha256"], hashlib.sha256(b"disk upload fixture").hexdigest())
            self.assertEqual(cloud.snapshots["snap-ff"]["VolumeSize"], 1)
            self.assertEqual(record["image"], "debian-worker")
            self.assertTrue(record["name"].startswith("debian-worker-"))
            self.assertEqual(tags(cloud.images["ami-ff"])["Name"], "debian-worker")
            self.assertEqual(tags(cloud.snapshots["snap-ff"])["Name"], "debian-worker")
        self.assertEqual(set(cloud.images), {"ami-1", "ami-2", "ami-ff"})
        self.assertEqual(set(cloud.snapshots), {"snap-1", "snap-2", "snap-ff"})
        self.assertEqual(cloud.mutations, [])

    def test_publication_uses_the_supplied_name_and_record_cleanup_needs_no_disk(self):
        cloud = MemoryCloud([version(1)])
        with tempfile.TemporaryDirectory() as directory:
            disk, output = self.build_fixture(directory)
            record = publish_image(disk, PublishingConfig(ImageName("fedora-builder"), TARGET), output, cloud=cloud)
            self.assertEqual(record["image"], "fedora-builder")
            self.assertTrue(record["name"].startswith("fedora-builder-"))
            self.assertEqual(record["target"], TARGET.identity())
            for resource in (cloud.images["ami-ff"], cloud.snapshots["snap-ff"]):
                self.assertEqual(tags(resource)["Name"], "fedora-builder")
                self.assertEqual(tags(resource)["image-family"], "fedora-builder")
                for key, value in TARGET.required_tags:
                    self.assertEqual(tags(resource)[key], value)
            disk.unlink()
            for _ in range(2):
                result = cleanup_record(output / "published-image.json", cloud=cloud)
                self.assertEqual(result["status"], "deleted")
        self.assertEqual(set(cloud.images), {"ami-1"})
        self.assertEqual(set(cloud.snapshots), {"snap-1"})

    def test_cleanup_rejects_a_different_destination_before_mutation(self):
        cloud = MemoryCloud()
        with tempfile.TemporaryDirectory() as directory:
            disk, output = self.build_fixture(directory)
            publish_image(disk, CONFIG, output, cloud=cloud)
            record_path = output / "published-image.json"
            record = read_json(record_path)
            record["target"]["region"] = "eu-west-1"
            write_json(record_path, record)
            with self.assertRaisesRegex(ValueError, "different publishing target"):
                cleanup_record(record_path, cloud=cloud)
            self.assertEqual(cloud.mutations, [])

    def test_delete_reads_an_existing_pending_record_without_configuration(self):
        cloud = MemoryCloud([version(1), version(2)])
        record = {
            "status": "pending", "publication_id": "01" * 16,
            "target": {
                "account_id": "123456789012", "region": "us-east-1",
                "required_tags": {"project": "disk-images", "environment": "testing"},
            },
            "artifact": {"sha256": "01" * 32}, "ami_id": None, "snapshot_id": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "published-image.json"
            path.write_text(json.dumps(record))
            self.assertEqual(cleanup_record(path, cloud=cloud)["status"], "deleted")
        self.assertEqual(set(cloud.images), {"ami-2"})
        self.assertEqual(set(cloud.snapshots), {"snap-2"})

    def test_separate_cleanup_failure_keeps_new_publication_available(self):
        cloud = MemoryCloud([version(1), version(2)])
        cloud.fail.add("delete-snapshot")
        with tempfile.TemporaryDirectory() as directory:
            build, output = self.build_fixture(directory)
            record = publish_image(build, CONFIG, output, cloud=cloud)
            with self.assertRaises(AwsError):
                prune_images(CONFIG, cloud=cloud)
            self.assertEqual(record["status"], "available")
            self.assertEqual(read_json(output / "published-image.json"), record)
        self.assertIn("ami-ff", cloud.images)
        self.assertIn("snap-ff", cloud.snapshots)
        cloud.fail.clear()
        prune_images(CONFIG, cloud=cloud)
        self.assertEqual(set(cloud.images), {"ami-ff"})
        self.assertEqual(set(cloud.snapshots), {"snap-ff"})

    @staticmethod
    def build_fixture(directory):
        disk = Path(directory) / "disk.raw"
        # Disk construction and validation are separate; these tests exercise AWS lifecycle only.
        disk.write_bytes(b"disk upload fixture")
        return disk, Path(directory) / "publication"


if __name__ == "__main__":
    unittest.main()
