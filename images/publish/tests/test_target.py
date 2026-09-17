"""Check the access contract exported by the RunsOn installation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import yaml

from publish.image import PublishingTarget
from publish.github import load_github_target


class PublishingTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "publishing.yaml"
        self.contract = {
            "schema_version": 4,
            "kind": "ami-publishing-target",
            "name": "test-install",
            "account_id": "123456789012",
            "region": "us-east-1",
            "publisher_role_arn": "arn:aws:iam::123456789012:role/publisher",
            "required_tags": {"runs-on-installation": "test-install"},
        }

    def load(self, contract):
        self.path.write_text(yaml.safe_dump(contract))
        return PublishingTarget.load(self.path)

    def test_access_contract_needs_no_disk_or_upload_configuration(self):
        target = self.load(self.contract)
        self.assertEqual(target.publisher_role_arn, self.contract["publisher_role_arn"])
        self.assertEqual(target.required_tags, (("runs-on-installation", "test-install"),))
        self.assertIsNone(target.legacy_kms_key_arn)

    def test_access_contract_enforces_role_account_and_ownership_tags(self):
        for field, value, message in (
            ("publisher_role_arn", "arn:aws:iam::999999999999:role/publisher", "target AWS account"),
            ("required_tags", {}, "ownership tags"),
            ("required_tags", {"runs-on-installation": "another-install"}, "match installation name"),
            ("required_tags", {"runs-on-installation": "test-install", "image-publication-id": "fake"}, "cannot override"),
        ):
            with self.subTest(field=field, value=value):
                contract = dict(self.contract, **{field: value})
                with self.assertRaisesRegex(ValueError, message):
                    self.load(contract)

    def test_github_access_contract_authorizes_only_its_repository_and_environment(self):
        self.contract["authentication"] = {"github": {
            "method": "github-oidc", "audience": "sts.amazonaws.com",
            "repositories": [{"repository": "example/images", "environment": "release"}],
        }}
        target = self.load(self.contract)
        self.assertEqual(load_github_target(self.path, "example/images", "release"), target)
        for repository, environment in (("other/images", "release"), ("example/images", "staging")):
            with self.subTest(repository=repository, environment=environment):
                with self.assertRaisesRegex(ValueError, "does not authorize"):
                    load_github_target(self.path, repository, environment)

    def test_retained_destination_contracts_keep_their_encryption_identity(self):
        key = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
        for version in (1, 2, 3):
            with self.subTest(version=version):
                contract = deepcopy(self.contract)
                contract["schema_version"] = version
                contract["destination"] = {
                    "type": "ec2-ami", "upload_method": "ebs-direct-api", "disk_format": "raw",
                    "encrypted": version < 3, "required_tags": contract.pop("required_tags"),
                }
                if version < 3:
                    contract["destination"]["kms_key_arn"] = key
                target = self.load(contract)
                self.assertEqual(target.legacy_kms_key_arn, key if version < 3 else None)
                self.assertEqual(target.required_tags, (("runs-on-installation", "test-install"),))


if __name__ == "__main__":
    unittest.main()
