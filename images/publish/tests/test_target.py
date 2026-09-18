"""Check the access contract exported by the RunsOn installation."""
import json
from pathlib import Path
import tempfile
import unittest

from publish.image import PublishingTarget


class PublishingTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "publishing.json"
        self.contract = {
            "name": "test-install",
            "account_id": "123456789012",
            "region": "us-east-1",
            "publisher_role_arn": "arn:aws:iam::123456789012:role/publisher",
            "required_tags": {"runs-on-installation": "test-install"},
        }

    def load(self, contract):
        self.path.write_text(json.dumps(contract))
        return PublishingTarget.load(self.path)

    def test_access_contract_needs_no_disk_or_upload_configuration(self):
        target = self.load(self.contract)
        self.assertEqual(target.publisher_role_arn, self.contract["publisher_role_arn"])
        self.assertEqual(target.required_tags, (("runs-on-installation", "test-install"),))

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


if __name__ == "__main__":
    unittest.main()
