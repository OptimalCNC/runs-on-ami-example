import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import FakeCloud, deployment, result, smoke, tags
import accepted_image
from accepted_image import AcceptedImage, prepare, selected_record, verify
from example import InvalidInput, file_sha, read_json, write_json


def qualified_record():
    value = result()
    value["status"] = "qualified"
    value["lifecycle"].update(retain=True, expires_at="2099-01-01T00:00:00Z",
                              cleanup={"status": "passed", "retained_images": [value["cloud"]["ami_id"]]})
    value["validation"].update(direct_boot={"status": "passed"}, runs_on=[{"status": "passed"}, {"status": "passed"}])
    return value


def accepted_cloud():
    cloud = FakeCloud()
    cloud.images[0].update(State="available", Architecture="x86_64", BootMode="uefi")
    cloud.images[0]["Tags"] = tags(expiry="2099-01-01T00:00:00Z") + [
        {"Key": "ami-example:recipe-id", "Value": "1" * 64}, {"Key": "ami-example:retain", "Value": "true"}]
    cloud.machines[0]["Tags"] = [{"Key": "ami-example:runs-on-repository", "Value": "example/repo"}]
    return cloud


class AcceptedImageContract(unittest.TestCase):
    def setUp(self):
        self.record = selected_record(qualified_record(), deployment(), "1" * 64)
        self.accepted = AcceptedImage.parse(self.record, deployment())

    def test_pending_or_expired_record_cannot_launch(self):
        with self.assertRaisesRegex(InvalidInput, "accepted yet"):
            AcceptedImage.parse({"schema_version": 1, "image": None}, deployment())
        self.record["image"]["expires_at"] = "2020-01-01T00:00:00Z"
        with self.assertRaisesRegex(InvalidInput, "expired"):
            AcceptedImage.parse(self.record, deployment())

    def test_unqualified_or_unretained_result_cannot_be_accepted(self):
        for value in (result(), {**qualified_record(), "status": "failed"}):
            with self.assertRaises(InvalidInput):
                selected_record(value, deployment(), "1" * 64)
        value = qualified_record()
        value["lifecycle"]["cleanup"]["retained_images"] = []
        with self.assertRaisesRegex(InvalidInput, "not retained"):
            selected_record(value, deployment(), "1" * 64)

    def test_live_ami_must_match_retained_identity_and_ownership(self):
        cloud = accepted_cloud()
        self.assertEqual(self.accepted.inspect(cloud)["ImageId"], self.accepted.ami_id)
        for key, value in (("OwnerId", "999999999999"), ("Public", True), ("State", "pending"),
                           ("Architecture", "arm64"), ("Tags", tags())):
            cloud = accepted_cloud()
            cloud.images[0][key] = value
            with self.subTest(key=key), self.assertRaises(InvalidInput):
                self.accepted.inspect(cloud)
            self.assertEqual(cloud.mutations, [])

    def test_uefi_preferred_ami_is_accepted_but_bios_fallback_is_rejected(self):
        result = qualified_record()
        result["cloud"]["ami_boot_mode"] = "uefi-preferred"
        record = selected_record(result, deployment(), "1" * 64)
        accepted = AcceptedImage.parse(record, deployment())
        self.assertEqual(accepted.ami_boot_mode, "uefi-preferred")
        cloud = accepted_cloud()
        cloud.images[0]["BootMode"] = "uefi-preferred"
        self.assertEqual(accepted.inspect(cloud)["BootMode"], "uefi-preferred")
        report = smoke("a", "2")
        self.assertEqual(verify(cloud, accepted, report, "123-1-one", "i-22222222222222222")["status"], "passed")
        cloud.machines[0]["CurrentInstanceBootMode"] = "legacy-bios"
        with self.assertRaisesRegex(InvalidInput, "boot mode"):
            verify(cloud, accepted, report, "123-1-one", "i-22222222222222222")
        result["cloud"]["boot_mode"] = "legacy-bios"
        with self.assertRaisesRegex(InvalidInput, "actual UEFI"):
            selected_record(result, deployment(), "1" * 64)

    def test_prepare_routes_exact_accepted_ami_with_explicit_execution_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            selected = prepare(deployment(), self.record, "124-2-one")
        self.assertEqual(selected["build_id"], "124-2-one")
        self.assertIn("ami=ami-11111111111111111", selected["label"])
        self.assertEqual(selected["recipe_id"], "1" * 64)
        self.assertEqual(selected["kernel_release"], self.accepted.kernel_release)
        with self.assertRaises(InvalidInput):
            prepare(deployment(), self.record, "invalid")

    def test_app_evidence_matches_explicit_instance_and_new_execution(self):
        report = smoke("a", "2")
        for guest in (report["identity"], report["environment"]):
            guest["sentinel"] = "/var/tmp/ami-example-124-2-one-sentinel"
        with patch.dict(os.environ, {}, clear=True):
            passed = verify(accepted_cloud(), self.accepted, report, "124-2-one", "i-22222222222222222")
        self.assertEqual(passed["build_id"], "124-2-one")
        self.assertEqual(passed["instance_id"], "i-22222222222222222")
        altered = copy.deepcopy(report)
        altered["identity"]["kernel_release"] = "6.8.0-aws"
        with self.assertRaisesRegex(InvalidInput, "kernel"):
            verify(accepted_cloud(), self.accepted, altered, "124-2-one", "i-22222222222222222")
        with self.assertRaisesRegex(InvalidInput, "compile/run"):
            verify(accepted_cloud(), self.accepted, {**report, "ctest_passed": False}, "124-2-one", "i-22222222222222222")
        with self.assertRaisesRegex(InvalidInput, "expected instance"):
            verify(accepted_cloud(), self.accepted, report, "124-2-one", "i-33333333333333333")

    def test_evidence_url_is_optional_and_supports_non_github_provenance(self):
        self.assertEqual(self.accepted.qualification_url, "")
        for evidence_url in ("s3://audit/qualification.json", "https://github.com/example/repo/actions/runs/123/attempts/1"):
            record = selected_record(qualified_record(), deployment(), "1" * 64, evidence_url)
            self.assertEqual(AcceptedImage.parse(record, deployment()).qualification_url, evidence_url)

    def test_prepare_cli_runs_without_environment_or_cloud(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "accepted.json"
            output = Path(temporary) / "launch.json"
            write_json(record, self.record)
            with patch.dict(os.environ, {}, clear=True), \
                 patch("sys.argv", ["accepted_image.py", "prepare", "--deployment", "local.json", "--record", str(record),
                                    "--build-id", "124-2-one", "--output", str(output)]), \
                 patch.object(accepted_image, "load_deployment", return_value=deployment()) as load, \
                 patch.object(accepted_image, "Cloud") as cloud:
                accepted_image.main()
            load.assert_called_once_with("local.json", inventories=False)
            cloud.assert_not_called()
            self.assertEqual(read_json(output)["build_id"], "124-2-one")
            self.assertIn("ami=" + self.accepted.ami_id, read_json(output)["label"])

    def test_accept_and_inspect_clis_use_explicit_files_without_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "qualified.json"
            record = Path(temporary) / "accepted.json"
            output = Path(temporary) / "admission.json"
            write_json(result, qualified_record())
            cloud = accepted_cloud()
            for arguments in (["accept", "--result", str(result), "--evidence-url", "s3://audit/qualified.json"],
                              ["inspect", "--output", str(output)]):
                with patch.dict(os.environ, {}, clear=True), \
                     patch("sys.argv", ["accepted_image.py", *arguments, "--record", str(record)]), \
                     patch.object(accepted_image, "load_deployment", return_value=deployment()), \
                     patch.object(accepted_image, "Cloud", return_value=cloud):
                    accepted_image.main()
            self.assertEqual(read_json(record)["image"]["qualification_sha256"], file_sha(result))
            self.assertEqual(read_json(record)["image"]["qualification_url"], "s3://audit/qualified.json")
            self.assertEqual(read_json(output)["ImageId"], self.accepted.ami_id)
            self.assertEqual(cloud.mutations, [])

    def test_verify_cli_reads_complete_smoke_evidence_without_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = directory / "accepted.json"
            output = directory / "verification.json"
            write_json(record, self.record)
            report = smoke("a", "2")
            write_json(directory / "identity.json", report["identity"])
            write_json(directory / "environment.json", report["environment"])
            (directory / "ctest.xml").write_text('<testsuite><testcase name="cobalt"/></testsuite>')
            with patch.dict(os.environ, {}, clear=True), \
                 patch("sys.argv", ["accepted_image.py", "verify", "--record", str(record), "--build-id", "123-1-one",
                                    "--smoke", str(directory), "--instance-id", "i-22222222222222222", "--output", str(output)]), \
                 patch.object(accepted_image, "load_deployment", return_value=deployment()), \
                 patch.object(accepted_image, "Cloud", return_value=accepted_cloud()):
                accepted_image.main()
            self.assertEqual(read_json(output)["status"], "passed")
            self.assertEqual(read_json(output)["instance_id"], "i-22222222222222222")


if __name__ == "__main__":
    unittest.main()
