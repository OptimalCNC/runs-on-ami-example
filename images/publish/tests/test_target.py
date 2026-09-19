"""Exercise caller-supplied destinations and AWS credentials."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from publish.core import Cloud, ImageName, PublishingConfig, PublishingTarget


class PublishingConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "publishing.json"
        self.contract = {
            "name": "worker-image",
            "account_id": "123456789012",
            "region": "us-east-1",
            "tags": {"project": "disk-images", "environment": "testing"},
        }

    def load(self, contract):
        self.path.write_text(json.dumps(contract))
        return PublishingConfig.load(self.path)

    def test_config_combines_image_name_and_destination(self):
        config = self.load(self.contract)
        self.assertEqual(config.name.value, self.contract["name"])
        self.assertEqual(config.target.account_id, self.contract["account_id"])
        self.assertEqual(config.target.region, self.contract["region"])
        self.assertEqual(dict(config.target.required_tags), self.contract["tags"])

    def test_destination_rejects_invalid_account_and_ownership_tags(self):
        for field, value, message in (
            ("account_id", "not-an-account", "12-digit"),
            ("region", "", "Invalid AWS region"),
            ("tags", None, "at most 45 tags"),
            ("tags", {"aws:project": "example"}, "invalid AWS tags"),
            ("tags", {"project": "example", "image-publication-id": "fake"}, "cannot override"),
            ("tags", {"Name": "example"}, "cannot override"),
            ("tags", {"image-retired": "true"}, "cannot override"),
        ):
            with self.subTest(field=field, value=value):
                contract = dict(self.contract, **{field: value})
                with self.assertRaisesRegex(ValueError, message):
                    self.load(contract)

    def test_name_cannot_expand_retention_scope_or_overflow_ami_name(self):
        for name in ("", "*", "worker?", "worker*", "worker/../other", "a" * 103):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ImageName(name)
        self.assertEqual(ImageName("a" * 102).value, "a" * 102)

    def test_tags_can_be_empty_or_omitted(self):
        for contract in (dict(self.contract, tags={}),
                         {key: value for key, value in self.contract.items() if key != "tags"}):
            with self.subTest(contract=contract):
                self.assertEqual(self.load(contract).target.required_tags, ())

    def test_rejects_missing_name_and_unknown_fields_instead_of_ignoring_scope(self):
        for contract in (
            {key: value for key, value in self.contract.items() if key != "name"},
            dict(self.contract, required_tags={"unexpected": "value"}),
        ):
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                self.load(contract)


class CredentialTests(unittest.TestCase):
    target = PublishingTarget("123456789012", "us-east-1", (("project", "disk-images"),))

    def authenticate(self, credentials, account="123456789012", profile=None):
        calls = []

        def run(command, **kwargs):
            calls.append((command, dict(kwargs["env"])))
            if "export-credentials" in command:
                result = credentials
            elif "get-caller-identity" in command:
                result = {"Account": account, "Arn": f"arn:aws:iam::{account}:user/publisher"}
            else:
                raise AssertionError(f"Unexpected credential operation: {command}")
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        cloud = Cloud(self.target, profile)
        with patch("publish.core.subprocess.run", side_effect=run):
            cloud.authenticate()
        return cloud, calls

    def test_permanent_profile_credentials_need_no_session_token_or_specific_role(self):
        environment = {"AWS_PROFILE": "ambient", "AWS_DEFAULT_PROFILE": "ambient",
                       "AWS_SESSION_TOKEN": "stale", "AWS_SECURITY_TOKEN": "stale"}
        with patch.dict(os.environ, environment, clear=True):
            cloud, calls = self.authenticate({"AccessKeyId": "test-key", "SecretAccessKey": "test-secret"},
                                             profile="chosen")
        self.assertEqual(calls[0][0][calls[0][0].index("--profile") + 1], "chosen")
        self.assertNotIn("--profile", calls[1][0])
        for key in environment:
            self.assertNotIn(key, cloud.environment)
        self.assertEqual(calls[1][1]["AWS_ACCESS_KEY_ID"], "test-key")
        self.assertEqual(cloud.environment["AWS_SECRET_ACCESS_KEY"], "test-secret")

    def test_ambient_temporary_credentials_are_shared_with_upload(self):
        credentials = {"AccessKeyId": "test-key", "SecretAccessKey": "test-secret", "SessionToken": "test-token"}
        cloud, calls = self.authenticate(credentials)
        self.assertEqual(cloud.environment["AWS_SESSION_TOKEN"], "test-token")
        with tempfile.TemporaryDirectory() as directory, patch("publish.core.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "snap-test\n", "")
            cloud.upload(Path("disk.raw"), {"image-publication-id": "test"}, 1, Path(directory))
            self.assertEqual(run.call_args.kwargs["env"], calls[1][1])

    def test_wrong_account_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "target account"):
            self.authenticate({"AccessKeyId": "test-key", "SecretAccessKey": "test-secret"},
                              account="999999999999")


if __name__ == "__main__":
    unittest.main()
