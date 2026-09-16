"""Publishing boundaries and cloud-resource lifecycle without AWS access."""

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts import BuiltImage, Compatibility, read_yaml, sha256, write_yaml
import publish


ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/example-image-publisher"
KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012"
SNAPSHOT = "snap-0123456789abcdef0"
AMI = "ami-0123456789abcdef0"
TAGS = {"runs-on-installation": "example"}


def target_value():
    """The handoff shape exported by the independent installer."""
    return {
        "schema_version": 3,
        "kind": "ami-publishing-target",
        "name": "example",
        "account_id": ACCOUNT,
        "region": REGION,
        "publisher_role_arn": ROLE,
        "authentication": {
            "local": {
                "method": "sts-assume-role",
                "principal_arns": [f"arn:aws:iam::{ACCOUNT}:role/Developer"],
            },
            "github": None,
        },
        "destination": {
            "type": "ec2-ami",
            "upload_method": "ebs-direct-api",
            "disk_format": "raw",
            "encrypted": False,
            "required_tags": deepcopy(TAGS),
        },
    }


def legacy_target_value(version):
    value = target_value()
    value["schema_version"] = version
    value["destination"].update(encrypted=True, kms_key_arn=KEY)
    return value


class PublishingTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "publishing.yaml"

    def load(self, value):
        write_yaml(self.path, value)
        return publish.PublishingTarget.load(self.path)

    def test_accepts_the_installation_publishing_contract(self):
        self.assertIsInstance(self.load(target_value()), publish.PublishingTarget)

    def test_github_entries_do_not_change_the_publication_identity(self):
        value = target_value()
        value["authentication"]["github"] = {
            "method": "github-oidc", "audience": "sts.amazonaws.com",
            "provider_arn": f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com",
            "repositories": [
                {"repository": "example/images", "environment": "image-publish", "subject": "repo:example/images:environment:image-publish"},
                {"repository": "other/images", "environment": "release", "subject": "repo:other/images:environment:release"},
            ],
        }
        self.assertEqual(self.load(value), self.load(target_value()))

    def test_retained_version_one_target_preserves_publication_identity_for_cleanup(self):
        value = legacy_target_value(1)
        value["authentication"]["github"] = {
            "method": "github-oidc", "repository": "example/images", "environment": "image-publish",
            "subject": "repo:example/images:environment:image-publish", "audience": "sts.amazonaws.com",
            "provider_arn": f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com",
        }
        self.assertEqual(self.load(value).identity(), {
            "name": "example", "account_id": ACCOUNT, "region": REGION,
            "publisher_role_arn": ROLE, "kms_key_arn": KEY, "required_tags": TAGS,
        })

    def test_retained_version_two_target_preserves_the_same_cleanup_identity(self):
        self.assertEqual(self.load(legacy_target_value(2)).identity(), self.load(legacy_target_value(1)).identity())

    def test_current_target_explicitly_records_no_encryption_key(self):
        target = self.load(target_value())
        self.assertIsNone(target.legacy_kms_key_arn)
        self.assertEqual(target.identity()["encrypted"], False)
        self.assertNotIn("kms_key_arn", target.identity())

    def test_current_target_rejects_encryption_and_kms_key_settings(self):
        for fields in ({"encrypted": True}, {"encrypted": None}, {"encrypted": 0},
                       {"kms_key_arn": KEY}, {"kms_key_arn": None}):
            with self.subTest(fields=fields):
                value = target_value()
                value["destination"].update(fields)
                with self.assertRaisesRegex(ValueError, "unencrypted destination"):
                    self.load(value)

    def test_rejects_unrecognized_contract(self):
        for field, invalid in (("schema_version", 4), ("schema_version", True), ("kind", "runs-on-installation")):
            with self.subTest(field=field):
                value = target_value()
                value[field] = invalid
                with self.assertRaises(ValueError):
                    self.load(value)

    def test_rejects_a_role_from_another_account(self):
        value = target_value()
        value["publisher_role_arn"] = ROLE.replace(ACCOUNT, "999999999999")
        with self.assertRaises(ValueError):
            self.load(value)

    def test_accepts_the_role_named_by_the_contract(self):
        value = target_value()
        value["publisher_role_arn"] = f"arn:aws:iam::{ACCOUNT}:role/custom-publishing-role"
        self.assertEqual(self.load(value).publisher_role_arn, value["publisher_role_arn"])

    def test_rejects_an_encryption_key_outside_the_destination(self):
        for key in (KEY.replace(ACCOUNT, "999999999999"), KEY.replace(REGION, "us-west-2")):
            with self.subTest(key=key):
                value = legacy_target_value(2)
                value["destination"]["kms_key_arn"] = key
                with self.assertRaises(ValueError):
                    self.load(value)

    def test_rejects_an_unsupported_publishing_method(self):
        value = target_value()
        value["destination"]["upload_method"] = "s3-import"
        with self.assertRaises(ValueError):
            self.load(value)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.target = publish.PublishingTarget("example", ACCOUNT, REGION, ROLE, None, tuple(TAGS.items()))
        self.publisher = {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/example-image-publisher/github-job",
        }
        self.developer = {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/Developer"}
        self.credentials = {"Credentials": {
            "AccessKeyId": "temporary-key", "SecretAccessKey": "temporary-secret", "SessionToken": "temporary-token",
        }}

    def test_already_assumed_github_publisher_does_not_assume_again(self):
        with patch.dict(os.environ, {
                "AWS_ACCESS_KEY_ID": "oidc-key", "AWS_SECRET_ACCESS_KEY": "oidc-secret",
                "AWS_SESSION_TOKEN": "oidc-token"}, clear=True):
            cloud = publish.Cloud(self.target)
        with patch.object(cloud, "aws", side_effect=[
                self.publisher, self.credentials["Credentials"], self.publisher]) as aws:
            cloud.authenticate()
        self.assertEqual(aws.call_args_list, [
            call("sts", "get-caller-identity"),
            call("configure", "export-credentials", "--format", "process"),
            call("sts", "get-caller-identity"),
        ])
        self.assertEqual(cloud.environment["AWS_ACCESS_KEY_ID"], "temporary-key")

    def test_ambient_profile_selection_is_resolved_before_the_rust_uploader_runs(self):
        for selector in ("AWS_DEFAULT_PROFILE", "AWS_PROFILE"):
            with self.subTest(selector=selector):
                with patch.dict(os.environ, {selector: "publisher"}, clear=True):
                    cloud = publish.Cloud(self.target)
                observations = []
                responses = [self.publisher, self.credentials["Credentials"], self.publisher]

                def aws(*arguments):
                    observations.append((arguments, dict(cloud.environment), cloud.profile))
                    return responses.pop(0)

                with patch.object(cloud, "aws", side_effect=aws):
                    cloud.authenticate()
                self.assertEqual(observations[1][0], ("configure", "export-credentials", "--format", "process"))
                self.assertEqual(observations[1][1][selector], "publisher")
                self.assertEqual(observations[2][0], ("sts", "get-caller-identity"))
                self.assertEqual(observations[2][1]["AWS_ACCESS_KEY_ID"], "temporary-key")
                self.assertEqual(observations[2][1]["AWS_SECRET_ACCESS_KEY"], "temporary-secret")
                self.assertEqual(observations[2][1]["AWS_SESSION_TOKEN"], "temporary-token")
                self.assertNotIn("AWS_PROFILE", observations[2][1])
                self.assertNotIn("AWS_DEFAULT_PROFILE", observations[2][1])
                self.assertIsNone(observations[2][2])

    def test_already_assumed_profile_exports_the_same_verified_credentials_for_upload(self):
        cloud = publish.Cloud(self.target, profile="publisher")
        with patch.object(cloud, "aws", side_effect=[
                self.publisher, self.credentials["Credentials"], self.publisher]) as aws:
            cloud.authenticate()
        self.assertEqual(aws.call_args_list, [
            call("sts", "get-caller-identity"),
            call("configure", "export-credentials", "--format", "process"),
            call("sts", "get-caller-identity"),
        ])
        self.assertIsNone(cloud.profile)
        self.assertEqual(cloud.environment["AWS_ACCESS_KEY_ID"], "temporary-key")
        self.assertEqual(cloud.environment["AWS_SESSION_TOKEN"], "temporary-token")

    def test_local_login_assumes_the_exact_role_then_verifies_the_new_identity(self):
        with patch.dict(os.environ, {"AWS_PROFILE": "old", "AWS_DEFAULT_PROFILE": "old"}):
            cloud = publish.Cloud(self.target, profile="developer")
        with patch.object(cloud, "aws", side_effect=[self.developer, self.credentials, self.publisher]) as aws:
            cloud.authenticate()
        self.assertEqual(aws.call_args_list, [
            call("sts", "get-caller-identity"),
            call("sts", "assume-role", "--role-arn", ROLE, "--role-session-name", "image-publication",
                 "--duration-seconds", "3600"),
            call("sts", "get-caller-identity"),
        ])
        self.assertIsNone(cloud.profile)
        self.assertEqual(cloud.environment["AWS_ACCESS_KEY_ID"], "temporary-key")
        self.assertEqual(cloud.environment["AWS_SECRET_ACCESS_KEY"], "temporary-secret")
        self.assertEqual(cloud.environment["AWS_SESSION_TOKEN"], "temporary-token")
        self.assertNotIn("AWS_PROFILE", cloud.environment)
        self.assertNotIn("AWS_DEFAULT_PROFILE", cloud.environment)

    def test_a_trusted_login_in_another_account_can_assume_the_exact_target_role(self):
        cloud = publish.Cloud(self.target)
        external = {
            "Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/Developer",
        }
        with patch.object(cloud, "aws", side_effect=[external, self.credentials, self.publisher]) as aws:
            cloud.authenticate()
        self.assertEqual(aws.call_args_list[1].args[:4], ("sts", "assume-role", "--role-arn", ROLE))
        self.assertEqual(aws.call_args_list[-1], call("sts", "get-caller-identity"))

    def test_rejects_unexpected_identity_returned_after_assumption(self):
        for identity in (self.developer, {**self.publisher, "Account": "999999999999"}):
            with self.subTest(identity=identity):
                cloud = publish.Cloud(self.target)
                with patch.object(cloud, "aws", side_effect=[self.developer, self.credentials, identity]):
                    with self.assertRaises(ValueError):
                        cloud.authenticate()

    def test_a_similarly_named_role_is_not_accepted_as_the_publisher(self):
        cloud = publish.Cloud(self.target)
        other = {**self.publisher, "Arn": self.publisher["Arn"].replace("publisher/", "publisher-extra/")}
        with patch.object(cloud, "aws", side_effect=[other, self.credentials, self.publisher]) as aws:
            cloud.authenticate()
        self.assertEqual(aws.call_args_list[1].args[:4], ("sts", "assume-role", "--role-arn", ROLE))

    def test_reauthentication_renews_from_the_original_local_login(self):
        cloud = publish.Cloud(self.target, profile="developer")
        observations = []
        responses = [self.developer, self.credentials, self.publisher] * 2

        def aws(*arguments):
            observations.append((arguments[:2], cloud.profile, cloud.environment.get("AWS_ACCESS_KEY_ID")))
            return responses.pop(0)

        with patch.object(cloud, "aws", side_effect=aws):
            cloud.authenticate()
            cloud.authenticate()
        self.assertEqual(observations[0][1], "developer")
        self.assertEqual(observations[3][1], "developer")
        self.assertEqual(observations[0][2], observations[3][2])
        self.assertEqual(observations[2][1:], (None, "temporary-key"))
        self.assertEqual(observations[5][1:], (None, "temporary-key"))


class FakeCloud:
    """A resource store that can lose a response after AWS has created a resource."""

    def __init__(self):
        self.calls = []
        self.snapshots = {}
        self.images = {}
        self.failure = None
        self.after_upload = None
        self.encryption_by_default = False

    def authenticate(self):
        self.calls.append(("authenticate",))

    def require_unencrypted_snapshots(self):
        self.calls.append(("require_unencrypted_snapshots",))
        if self.encryption_by_default:
            raise ValueError("EBS encryption by default is enabled")

    def upload(self, disk, tags, volume_gib, output, on_snapshot):
        self.calls.append(("upload", disk, deepcopy(tags), volume_gib))
        # This durable record must exist before the first resource can be created.
        record = read_yaml(output / "publication-state.yaml")
        if record["publication_id"] != tags["image-publication-id"]:
            raise AssertionError("resource creation has no matching recovery record")
        self.snapshots[SNAPSHOT] = {
            "SnapshotId": SNAPSHOT, "OwnerId": ACCOUNT, "Encrypted": False,
            "State": "completed", "VolumeSize": volume_gib,
            "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
        }
        if self.after_upload:
            self.after_upload(disk)
        if self.failure == "upload":
            raise RuntimeError("upload response lost")
        on_snapshot(SNAPSHOT)
        return SNAPSHOT

    def register(self, name, snapshot_id, compatibility, tags):
        self.calls.append(("register", name, snapshot_id, deepcopy(compatibility), deepcopy(tags)))
        self.images[AMI] = {
            "ImageId": AMI, "OwnerId": ACCOUNT, "State": "available",
            "Architecture": compatibility["architecture"], "BootMode": compatibility["boot_mode"],
            "EnaSupport": compatibility["ena_support"],
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": snapshot_id, "Encrypted": False}}],
            "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
        }
        if self.failure == "register":
            raise RuntimeError("registration response lost")
        return AMI

    def discover(self, publication_id):
        self.calls.append(("discover", publication_id))

        def matches(resource):
            return any(tag == {"Key": "image-publication-id", "Value": publication_id}
                       for tag in resource["Tags"])

        return ([key for key, value in self.snapshots.items() if matches(value)],
                [key for key, value in self.images.items() if matches(value)])

    def snapshot(self, snapshot_id):
        self.calls.append(("snapshot", snapshot_id))
        return deepcopy(self.snapshots.get(snapshot_id))

    def image(self, image_id):
        self.calls.append(("image", image_id))
        return deepcopy(self.images.get(image_id))

    def deregister(self, image_id):
        self.calls.append(("deregister", image_id))
        self.images.pop(image_id, None)

    def delete_snapshot(self, snapshot_id):
        self.calls.append(("delete_snapshot", snapshot_id))
        if self.failure == "delete":
            raise RuntimeError("temporary snapshot deletion failure")
        self.snapshots.pop(snapshot_id, None)

    def mutations(self):
        return [call for call in self.calls if call[0] in {"deregister", "delete_snapshot"}]


class UploadProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.disk = self.root / "disk.raw"
        self.disk.write_bytes(b"disk")
        target = publish.PublishingTarget("example", ACCOUNT, REGION, ROLE, None, tuple(TAGS.items()))
        self.cloud = publish.Cloud(target)
        self.tags = {**TAGS, "image-publication-id": "a" * 32}

    def test_encryption_preflight_requires_an_explicit_disabled_setting(self):
        for response in ({"EbsEncryptionByDefault": True}, {}, {"EbsEncryptionByDefault": 0}):
            with self.subTest(response=response), patch.object(self.cloud, "aws", return_value=response) as aws:
                with self.assertRaisesRegex(ValueError, "encryption by default"):
                    self.cloud.require_unencrypted_snapshots()
                aws.assert_called_once_with("ec2", "get-ebs-encryption-by-default")
        with patch.object(self.cloud, "aws", return_value={"EbsEncryptionByDefault": False}) as aws:
            self.cloud.require_unencrypted_snapshots()
            aws.assert_called_once_with("ec2", "get-ebs-encryption-by-default")

    def test_encryption_preflight_does_not_ignore_access_denied(self):
        with patch.object(self.cloud, "aws", side_effect=publish.AwsError("get-ebs-encryption-by-default", "AccessDenied")):
            with self.assertRaises(publish.AwsError):
                self.cloud.require_unencrypted_snapshots()

    def test_timeout_stops_and_reaps_the_upload_process(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("coldsnap", 10), 0]
        remember = Mock()
        with patch.object(publish.subprocess, "Popen", return_value=process), \
                patch.object(publish.time, "monotonic", side_effect=[0, 3001]):
            with self.assertRaisesRegex(RuntimeError, "session limit"):
                self.cloud.upload(self.disk, self.tags, 1, self.root, remember)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list, [call(timeout=10), call()])
        remember.assert_not_called()

    def test_snapshot_identity_is_reported_while_the_upload_is_still_running(self):
        process = Mock()
        process.poll.side_effect = [None, 0, 0]
        process.wait.side_effect = subprocess.TimeoutExpired("coldsnap", 5)
        process.returncode = 0
        seen = []

        def start(command, **options):
            self.assertEqual(command[:4], ["coldsnap", "--region", REGION, "upload"])
            self.assertNotIn("--kms-key-id", command)
            self.assertNotIn("--parent-snapshot-id", command)
            self.assertNotIn(KEY, command)
            options["stdout"].write(SNAPSHOT + "\n")
            options["stdout"].flush()
            return process

        def remember(snapshot_id):
            seen.append((snapshot_id, process.poll.call_count))

        with patch.object(publish.subprocess, "Popen", side_effect=start), \
                patch.object(self.cloud, "discover", return_value=([SNAPSHOT], [])):
            result = self.cloud.upload(self.disk, self.tags, 1, self.root, remember)
        self.assertEqual(result, SNAPSHOT)
        self.assertEqual(seen[0], (SNAPSHOT, 1))
        self.assertEqual(seen[-1][0], SNAPSHOT)
        process.terminate.assert_not_called()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        disk = self.root / "disk.raw"
        disk.write_bytes(bytes(range(256)) * 2)
        manifest = self.root / "manifest.json"
        manifest.write_text("{}")
        self.build = BuiltImage(
            self.root / "build.yaml", disk, sha256(disk), disk.stat().st_size, "a" * 64,
            manifest, sha256(manifest), {}, Compatibility("x86_64", "uefi", False, 1, True, "0.1.12"),
        )
        self.target = publish.PublishingTarget("example", ACCOUNT, REGION, ROLE, None, tuple(TAGS.items()))
        self.cloud = FakeCloud()
        self.output = self.root / "publication"
        self.state = self.output / "publication-state.yaml"
        self.published = self.output / "published-image.yaml"

    def execute(self):
        return publish.publish_image(self.build, self.target, self.output, cloud=self.cloud)

    def test_publishes_the_selected_artifact_and_records_the_exact_ami(self):
        result = self.execute()
        self.assertEqual(result["kind"], "published-image")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["ami_id"], AMI)
        self.assertEqual(result["snapshot_id"], SNAPSHOT)
        self.assertEqual(result["account_id"], ACCOUNT)
        self.assertEqual(result["region"], REGION)
        self.assertIs(result["target"]["encrypted"], False)
        self.assertNotIn("kms_key_arn", result["target"])
        self.assertIs(self.cloud.snapshots[SNAPSHOT]["Encrypted"], False)
        self.assertEqual(result["artifact"], {
            "sha256": self.build.disk_sha256, "size_bytes": self.build.disk_size_bytes,
            "recipe_id": self.build.recipe_id,
            "manifest_sha256": self.build.manifest_sha256,
        })
        self.assertEqual(result["compatibility"], self.build.compatibility.as_dict())
        self.assertEqual(read_yaml(self.published), result)
        upload = next(item for item in self.cloud.calls if item[0] == "upload")
        self.assertEqual(upload[1], self.build.disk_path)
        self.assertEqual(upload[2], {
            **TAGS, "image-publication-id": result["publication_id"],
            "image-sha256": self.build.disk_sha256, "image-recipe-id": self.build.recipe_id,
        })
        self.assertEqual(self.cloud.mutations(), [])

    def test_account_encryption_default_blocks_before_upload_or_publication_state(self):
        self.cloud.encryption_by_default = True
        with self.assertRaisesRegex(ValueError, "encryption by default"):
            self.execute()
        self.assertEqual(self.cloud.calls, [("authenticate",), ("require_unencrypted_snapshots",)])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.published.exists())

    def test_legacy_encrypted_target_cannot_create_new_publications(self):
        self.target = replace(self.target, legacy_kms_key_arn=KEY)
        with self.assertRaisesRegex(ValueError, "supported only for cleanup"):
            self.execute()
        self.assertEqual(self.cloud.calls, [])
        self.assertFalse(self.state.exists())

    def test_encryption_default_change_during_upload_cleans_up_without_registration(self):
        def encrypt_uploaded_snapshot(disk):
            self.cloud.snapshots[SNAPSHOT].update(Encrypted=True, KmsKeyId=KEY)
        self.cloud.after_upload = encrypt_uploaded_snapshot
        with self.assertRaisesRegex(RuntimeError, "Published snapshot must be unencrypted"):
            self.execute()
        self.assertEqual(self.cloud.mutations(), [("delete_snapshot", SNAPSHOT)])
        self.assertFalse(any(item[0] == "register" for item in self.cloud.calls))
        self.assertFalse(self.published.exists())
        self.assertEqual(read_yaml(self.state)["status"], "deleted")

    def test_missing_snapshot_encryption_evidence_is_not_accepted(self):
        self.cloud.after_upload = lambda disk: self.cloud.snapshots[SNAPSHOT].pop("Encrypted")
        with self.assertRaisesRegex(RuntimeError, "Published snapshot must be unencrypted"):
            self.execute()
        self.assertFalse(any(item[0] == "register" for item in self.cloud.calls))
        self.assertFalse(self.published.exists())

    def test_registered_image_must_also_report_an_unencrypted_disk(self):
        register = self.cloud.register
        def encrypted_image(*arguments):
            image_id = register(*arguments)
            self.cloud.images[image_id]["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] = True
            return image_id
        with patch.object(self.cloud, "register", side_effect=encrypted_image):
            with self.assertRaisesRegex(RuntimeError, "Registered image must use an unencrypted snapshot"):
                self.execute()
        self.assertEqual(self.cloud.mutations(), [("deregister", AMI), ("delete_snapshot", SNAPSHOT)])
        self.assertFalse(self.published.exists())

    def test_existing_publication_is_not_overwritten(self):
        self.execute()
        before = self.published.read_bytes()
        self.cloud.calls.clear()
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.published.read_bytes(), before)
        self.assertEqual(self.cloud.calls, [])

    def test_publication_carries_the_manifest_digest_verified_with_the_build(self):
        expected = self.build.manifest_sha256
        self.build.manifest_path.write_text("a different local file after the build was parsed")
        result = self.execute()
        self.assertEqual(result["artifact"]["manifest_sha256"], expected)
        self.assertNotEqual(result["artifact"]["manifest_sha256"], sha256(self.build.manifest_path))

    def test_lost_upload_response_discovers_and_deletes_the_partial_snapshot(self):
        self.cloud.failure = "upload"
        with self.assertRaisesRegex(RuntimeError, "resources were cleaned up"):
            self.execute()
        self.assertEqual(self.cloud.mutations(), [("delete_snapshot", SNAPSHOT)])
        self.assertEqual(self.cloud.snapshots, {})
        self.assertEqual(read_yaml(self.state)["status"], "deleted")
        self.assertFalse(self.published.exists())

    def test_lost_registration_response_recovers_both_resources_in_dependency_order(self):
        self.cloud.failure = "register"
        with self.assertRaisesRegex(RuntimeError, "resources were cleaned up"):
            self.execute()
        self.assertEqual(self.cloud.mutations(), [("deregister", AMI), ("delete_snapshot", SNAPSHOT)])
        self.assertEqual(self.cloud.images, {})
        self.assertEqual(self.cloud.snapshots, {})
        self.assertEqual(read_yaml(self.state)["status"], "deleted")
        self.assertFalse(self.published.exists())

    def test_artifact_changed_during_upload_is_cleaned_without_registration(self):
        self.cloud.after_upload = lambda disk: disk.write_bytes(b"x" * self.build.disk_size_bytes)
        with self.assertRaisesRegex(RuntimeError, "changed during publication"):
            self.execute()
        self.assertEqual(self.cloud.mutations(), [("delete_snapshot", SNAPSHOT)])
        self.assertFalse(any(item[0] == "register" for item in self.cloud.calls))

    def test_owned_publication_cleanup_is_repeatable(self):
        self.execute()
        self.cloud.calls.clear()
        result = publish.cleanup_record(self.published, self.target, cloud=self.cloud)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(read_yaml(self.state)["status"], "deleted")
        self.assertEqual(read_yaml(self.published)["status"], "deleted")
        self.assertEqual(self.cloud.mutations(), [("deregister", AMI), ("delete_snapshot", SNAPSHOT)])
        self.cloud.calls.clear()
        result = publish.cleanup_record(self.published, self.target, cloud=self.cloud)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(self.cloud.mutations(), [])

    def test_cleanup_target_mismatch_is_rejected_before_authentication(self):
        self.execute()
        self.cloud.calls.clear()
        wrong = replace(self.target, region="us-west-2")
        with self.assertRaises(ValueError):
            publish.cleanup_record(self.published, wrong, cloud=self.cloud)
        self.assertEqual(self.cloud.calls, [])

    def test_cleanup_checks_all_owners_before_deleting_any_resource(self):
        self.execute()
        originals = deepcopy(self.cloud.snapshots), deepcopy(self.cloud.images)
        corruptions = [
            ("snapshot", "OwnerId", "999999999999"),
            ("snapshot", "Tags", []),
            ("image", "OwnerId", "999999999999"),
            ("image", "Tags", []),
            ("image", "BlockDeviceMappings", [{"Ebs": {"SnapshotId": "snap-99999999999999999"}}]),
        ]
        for resource_type, field, value in corruptions:
            with self.subTest(resource=resource_type, field=field):
                self.cloud.snapshots, self.cloud.images = deepcopy(originals)
                resources, resource_id = ((self.cloud.snapshots, SNAPSHOT) if resource_type == "snapshot"
                                          else (self.cloud.images, AMI))
                resources[resource_id][field] = value
                self.cloud.calls.clear()
                with self.assertRaises(ValueError):
                    publish.cleanup_record(self.published, self.target, cloud=self.cloud)
                self.assertEqual(self.cloud.mutations(), [])
                self.assertNotEqual(read_yaml(self.published)["status"], "deleted")

    def test_retained_encrypted_publication_cleanup_uses_its_original_key_identity(self):
        self.execute()
        legacy_target = replace(self.target, legacy_kms_key_arn=KEY)
        for path in (self.state, self.published):
            record = read_yaml(path)
            record["target"] = legacy_target.identity()
            write_yaml(path, record)
        self.cloud.snapshots[SNAPSHOT].update(Encrypted=True, KmsKeyId=KEY)
        self.cloud.encryption_by_default = True
        self.cloud.calls.clear()
        for corruptions in ({"Encrypted": False}, {"KmsKeyId": KEY.replace(ACCOUNT, "999999999999")}):
            with self.subTest(corruptions=corruptions):
                self.cloud.snapshots[SNAPSHOT].update(Encrypted=True, KmsKeyId=KEY)
                self.cloud.snapshots[SNAPSHOT].update(corruptions)
                with self.assertRaisesRegex(ValueError, "outside the publishing target"):
                    publish.cleanup_record(self.published, legacy_target, cloud=self.cloud)
                self.assertEqual(self.cloud.mutations(), [])
        self.cloud.snapshots[SNAPSHOT].update(Encrypted=True, KmsKeyId=KEY)
        result = publish.cleanup_record(self.published, legacy_target, cloud=self.cloud)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(self.cloud.mutations(), [("deregister", AMI), ("delete_snapshot", SNAPSHOT)])
        self.assertNotIn(("require_unencrypted_snapshots",), self.cloud.calls)

    def test_failed_cleanup_keeps_a_recovery_record_for_an_explicit_retry(self):
        self.cloud.failure = "delete"

        def fail_after_upload(disk):
            raise RuntimeError("upload connection lost")

        self.cloud.after_upload = fail_after_upload
        with self.assertRaisesRegex(RuntimeError, "Cleanup needs attention"):
            self.execute()
        record = read_yaml(self.state)
        self.assertEqual(record["status"], "cleanup-needed")
        self.assertIn(SNAPSHOT, record["discovered_snapshot_ids"])
        self.assertIn(SNAPSHOT, self.cloud.snapshots)
        self.assertFalse(self.published.exists())
        self.cloud.failure = None
        publish.cleanup_record(self.state, self.target, cloud=self.cloud)
        self.assertEqual(read_yaml(self.state)["status"], "deleted")
        self.assertEqual(self.cloud.snapshots, {})

    def test_invisible_partial_upload_keeps_recovery_pending_until_discovery_succeeds(self):
        self.cloud.failure = "upload"
        with patch.object(self.cloud, "discover", return_value=([], [])):
            with self.assertRaisesRegex(RuntimeError, "Cleanup needs attention"):
                self.execute()
        self.assertEqual(read_yaml(self.state)["status"], "cleanup-needed")
        self.assertEqual(self.cloud.mutations(), [])
        self.assertIn(SNAPSHOT, self.cloud.snapshots)
        publish.cleanup_record(self.state, self.target, cloud=self.cloud)
        self.assertEqual(read_yaml(self.state)["status"], "deleted")
        self.assertEqual(self.cloud.snapshots, {})


if __name__ == "__main__":
    unittest.main()
