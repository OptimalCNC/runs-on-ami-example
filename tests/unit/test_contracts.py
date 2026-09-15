import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jsonschema

from support import ROOT, FakeCloud, deployment, deployment_dict, guest, module, result, smoke
from example import BuildInputs, InfrastructureBindings, InvalidInput, build_id, load_deployment, recipe


def instance_ids():
    return {"a": "i-22222222222222222", "b": "i-33333333333333333"}


class Inputs(unittest.TestCase):
    def test_unresolved_installation_cannot_be_used_to_launch(self):
        with self.assertRaises(InvalidInput):
            BuildInputs.parse({"repository": "example/repo", "account_id": "123456789012", "region": "us-east-1"})

    def test_unqualified_architecture_wildcards_and_cross_account_roles_fail(self):
        for change in ({"instance_type": "c7i.*"}, {"controller_role_arn": "arn:aws:iam::999999999999:role/example"}, {"vcpus": True}):
            with self.subTest(change=change), self.assertRaises(InvalidInput):
                BuildInputs.parse({**deployment_dict(), **change})
        value = deployment_dict()
        value["source_ami"]["architecture"] = "arm64"
        with self.assertRaises(InvalidInput):
            BuildInputs.parse(value)

    def test_uefi_preferred_parent_has_a_fixed_qualified_boot_mode(self):
        value = deployment_dict()
        value["source_ami"]["boot_mode"] = "uefi-preferred"
        parsed = BuildInputs.parse(value)
        self.assertEqual(parsed.source_ami.boot_mode, "uefi-preferred")
        self.assertEqual(parsed.source_ami.effective_boot_mode, "uefi")

    def test_public_parent_and_small_instances_preserve_exact_settings(self):
        value = deployment_dict()
        value["source_ami"]["owner"] = "135269210855"
        value.update(instance_type="t3.micro", parent_root_volume_gib=30)
        parsed = BuildInputs.parse(value)
        self.assertEqual(parsed.source_ami.owner, "135269210855")
        self.assertEqual(parsed.instance_type, "t3.micro")
        self.assertEqual(parsed.parent_root_volume_gib, 30)
        self.assertEqual(parsed.root_volume_gib, 80)

    def test_inventory_configuration_does_not_claim_an_installed_runson(self):
        value = deployment_dict()
        parsed = InfrastructureBindings.parse({**{field.name: value[field.name] for field in dataclasses.fields(InfrastructureBindings)},
                                                "runs_on": None})
        self.assertIsNone(parsed.runs_on)
        with self.assertRaisesRegex(InvalidInput, "actual RunsOn installation"):
            parsed.runner_settings()

    def test_bootstrap_version_is_locked_independently_of_service_version(self):
        value = deployment_dict()
        inventory = result()["payload"]["parent_inventory"]
        inventory["packages_sha256"] = hashlib.sha256(inventory["package_inventory"].encode()).hexdigest()
        encoded = json.dumps(inventory).encode()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "infra").mkdir()
            for name in ("source_ami", "controller_ami"):
                value[name]["inventory_file"] = name + "-inventory.json"
                (root / "infra" / value[name]["inventory_file"]).write_bytes(encoded)
                value[name]["inventory_sha256"] = hashlib.sha256(encoded).hexdigest()
            config = root / "infra/deployment.json"
            config.write_text(json.dumps(value))
            installed = load_deployment(config).require_runs_on()
            self.assertEqual((installed.version, installed.bootstrap_version), ("3.2.0", "0.1.12"))
            value["runs_on"]["bootstrap_version"] = "0.1.13"
            config.write_text(json.dumps(value))
            with self.assertRaisesRegex(InvalidInput, "bootstrap version selected"):
                load_deployment(config)

    def test_recipe_ignores_retention_but_changes_for_guest_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.sh").write_text("compile\n")
            lock = {"recipe_files": ["build.sh"]}
            first, _ = recipe(lock, deployment(), root)
            second, _ = recipe(lock, dataclasses.replace(deployment(), retain_hours=72), root)
            self.assertEqual(first, second)
            changed, _ = recipe(lock, dataclasses.replace(deployment(), root_volume_gib=160), root)
            self.assertNotEqual(first, changed)
            runtime, _ = recipe(lock, dataclasses.replace(deployment(), instance_type="t3.micro"), root)
            self.assertEqual(first, runtime)
            builder, _ = recipe(lock, dataclasses.replace(deployment(), builder_instance_type="c7i.xlarge"), root)
            self.assertNotEqual(first, builder)
            (root / "build.sh").write_text("compile differently\n")
            self.assertNotEqual(first, recipe(lock, deployment(), root)[0])

    def test_build_attempts_are_distinct_and_shell_text_is_rejected(self):
        self.assertNotEqual(build_id("123", "1", "one"), build_id("123", "2", "one"))
        with self.assertRaises(InvalidInput):
            build_id("123; touch /tmp/no", "1", "one")


class ParentAdmission(unittest.TestCase):
    def setUp(self):
        self.preflight = module("preflight")
        self.cloud = MagicMock()
        self.cloud.deployment = deployment()
        self.identity = dataclasses.replace(deployment().source_ami, owner="135269210855")
        self.image = {"ImageId": self.identity.id, "OwnerId": self.identity.owner, "Architecture": "x86_64",
                      "State": "available", "VirtualizationType": "hvm", "RootDeviceType": "ebs", "BootMode": "uefi",
                      "Public": True, "DeprecationTime": "2099-01-01T00:00:00Z", "RootDeviceName": "/dev/sda1",
                      "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}]}
        self.cloud.call.return_value = {"Images": [self.image]}

    def test_future_deprecation_and_pinned_public_owner_are_accepted(self):
        self.assertEqual(self.preflight.inspect_ami(self.cloud, self.identity), self.image)
        self.cloud.call.assert_called_once_with("ec2", "describe-images", {
            "ImageIds": [self.identity.id], "Owners": [self.identity.owner]})

    def test_expired_deprecation_and_wrong_owner_are_rejected(self):
        for change in ({"DeprecationTime": "2020-01-01T00:00:00Z"}, {"OwnerId": "999999999999"}):
            self.cloud.call.return_value = {"Images": [{**self.image, **change}]}
            with self.subTest(change=change), self.assertRaises(InvalidInput):
                self.preflight.inspect_ami(self.cloud, self.identity)

    def test_volume_must_cover_the_ami_snapshot(self):
        self.preflight.inspect_root_volume(self.image, 30)
        with self.assertRaisesRegex(InvalidInput, "smaller than the AMI"):
            self.preflight.inspect_root_volume(self.image, 29)


class Evidence(unittest.TestCase):
    def setUp(self):
        self.verify = module("verify-run-results")
        self.probe = {"status": "passed", "terminated": True, "instance_id": "i-" + "4" * 17, "guest": guest("probe", "4")}

    def verified(self, first=None, second=None, cloud=None):
        return self.verify.verify(cloud or FakeCloud(), result(), self.probe, first or smoke("a", "2"), second or smoke("b", "3"), instance_ids())

    def test_runtime_verification_waits_for_cleanup_before_qualification(self):
        verified = self.verified()
        self.assertEqual(verified["status"], "candidate")
        self.assertTrue(all(r["status"] == "passed" for r in verified["validation"]["runs_on"]))

    def test_fast_terminated_runner_is_verifiable_before_watchdog_tagging(self):
        from example import RUNS_ON_TAG
        cloud = FakeCloud()
        cloud.machines[0]["Tags"] = [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
        cloud.machines[0]["State"]["Name"] = "terminated"
        self.assertEqual(self.verified(cloud=cloud)["validation"]["runs_on"][0]["status"], "passed")

    def test_stock_fallback_kernel_is_rejected(self):
        first = smoke("a", "2")
        first["identity"]["kernel_release"] = "6.8.0-aws"
        with self.assertRaisesRegex(InvalidInput, "kernel"):
            self.verified(first=first)

    def test_reused_instance_rejected_even_with_matching_reports(self):
        jobs = instance_ids()
        jobs["b"] = jobs["a"]
        with self.assertRaisesRegex(InvalidInput, "distinct"):
            self.verify.verify(FakeCloud(), result(), self.probe, smoke("a", "2"), smoke("b", "2"), jobs)

    def test_controller_catches_false_guest_ami_claim(self):
        cloud = FakeCloud()
        cloud.machines[0]["ImageId"] = "ami-99999999999999999"
        with self.assertRaisesRegex(InvalidInput, "controller"):
            self.verified(cloud=cloud)

    def test_expected_instance_must_match_guest_evidence(self):
        jobs = instance_ids()
        jobs["a"] = "i-99999999999999999"
        with self.assertRaisesRegex(InvalidInput, "expected instance and guest"):
            self.verify.verify(FakeCloud(), result(), self.probe, smoke("a", "2"), smoke("b", "3"), jobs)

    def test_bios_fallback_on_uefi_target_is_rejected(self):
        cloud = FakeCloud()
        cloud.machines[0]["CurrentInstanceBootMode"] = "legacy-bios"
        with self.assertRaisesRegex(InvalidInput, "boot mode"):
            self.verified(cloud=cloud)

    def test_changed_agent_failed_environment_sentinel_and_ctest_rejected(self):
        for field in ("agent", "bootstrap", "packages", "environment", "sentinel", "ctest"):
            first = smoke("a", "2")
            if field == "agent":
                first["identity"]["runner_version"] = "9.9.9"
            elif field == "bootstrap":
                first["identity"]["bootstrap_files"] = {}
            elif field == "packages":
                first["identity"]["packages_sha256"] = "9" * 64
            elif field == "environment":
                first["environment"]["environment_passed"] = False
            elif field == "sentinel":
                first["identity"]["sentinel_absent_at_start"] = False
            else:
                first["ctest_passed"] = False
            with self.subTest(field=field), self.assertRaises(InvalidInput):
                self.verified(first=first)

    def test_schema_prevents_creation_record_claiming_qualification(self):
        schema = json.loads((ROOT / "schemas/image-result.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        candidate = result()
        validator.validate(candidate)
        candidate["status"] = "qualified"
        self.assertFalse(validator.is_valid(candidate))
        qualified = self.verified()
        qualified["status"] = "qualified"
        qualified["lifecycle"]["cleanup"] = {"status": "passed"}
        validator.validate(qualified)

    def test_missing_mercury_or_mismatched_cobalt_is_rejected(self):
        for identity in (None, {"version": "3.3.3", "core": "mercury", "prefix": "/usr/xenomai"},
                         {"version": "3.3.2", "core": "cobalt", "prefix": "/usr/xenomai"}):
            first = smoke("a", "2")
            first["identity"]["xenomai"] = identity
            with self.subTest(identity=identity), self.assertRaises(InvalidInput):
                self.verified(first=first)

    def test_ctest_requires_the_cobalt_application_to_pass(self):
        read_smoke = module("verify-run-results").read_smoke
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "identity.json").write_text("{}")
            (directory / "environment.json").write_text("{}")
            cases = [("cobalt", "", True), ("hello", "", False), ("cobalt", "<failure/>", False),
                     ("cobalt", "<skipped/>", False), ("cobalt", "<error/>", False)]
            for name, content, passed in cases:
                (directory / "ctest.xml").write_text(f'<testsuite><testcase name="{name}">{content}</testcase></testsuite>')
                with self.subTest(name=name, content=content):
                    self.assertEqual(read_smoke(directory)["ctest_passed"], passed)


class Reproducibility(unittest.TestCase):
    def setUp(self):
        self.compare = module("compare-builds").compare
        self.first = result()
        self.first["status"] = "qualified"
        self.first["lifecycle"]["cleanup"] = {"status": "passed"}
        self.first["validation"]["direct_boot"] = {"status": "passed", "terminated": True, "instance_id": "i-44444444444444444", "guest": guest("probe", "4")}
        self.first["validation"]["runs_on"] = [{"status": "passed", "stage": stage, "instance_id": "i-" + digit * 17,
                                               "guest": guest(stage, digit), "application": {"name": "cobalt", "status": "passed"}}
                                               for stage, digit in (("a", "2"), ("b", "3"))]
        self.second = copy.deepcopy(self.first)
        self.second["execution"]["build_id"] = "123-1-two"
        self.second["cloud"]["ami_id"] = "ami-22222222222222222"

    def test_cloud_ids_are_not_payload(self):
        self.assertEqual(self.compare(self.first, self.second)["status"], "passed")

    def test_payload_change_reports_exact_file(self):
        self.second["payload"]["payload_hashes"]["/boot/vmlinuz"] = "2" * 64
        compared = self.compare(self.first, self.second)
        self.assertEqual(compared["status"], "failed")
        self.assertIn("/boot/vmlinuz", compared["differences"]["payload_hashes"])

    def test_cobalt_library_change_reports_exact_file(self):
        self.second["payload"]["xenomai_files"]["lib/libcobalt.so.2"]["sha256"] = "2" * 64
        compared = self.compare(self.first, self.second)
        self.assertEqual(compared["status"], "failed")
        self.assertIn("lib/libcobalt.so.2", compared["differences"]["xenomai_files"])

    def test_missing_cobalt_application_cannot_qualify_a_comparison(self):
        self.second["validation"]["runs_on"][0].pop("application")
        with self.assertRaisesRegex(InvalidInput, "Cobalt application"):
            self.compare(self.first, self.second)

    def test_raw_initramfs_variation_is_disclosed(self):
        self.second["payload"]["initramfs_sha256"] = "2" * 64
        compared = self.compare(self.first, self.second)
        self.assertEqual(compared["status"], "passed")
        self.assertFalse(compared["raw_initramfs_equal"])
        self.second["payload"]["initramfs_content"]["main/init"]["sha256"] = "3" * 64
        self.assertEqual(self.compare(self.first, self.second)["status"], "failed")

    def test_unqualified_reused_or_different_recipe_builds_cannot_compare(self):
        for field in ("qualified", "same-build", "recipe"):
            other = copy.deepcopy(self.second)
            if field == "qualified":
                other["status"] = "candidate"
            elif field == "same-build":
                other["execution"]["build_id"] = self.first["execution"]["build_id"]
            else:
                other["source"]["recipe_id"] = "3" * 64
            with self.subTest(field=field), self.assertRaises(InvalidInput):
                self.compare(self.first, other)

    def test_status_alone_cannot_replace_qualification_evidence(self):
        self.second["validation"]["runs_on"] = []
        with self.assertRaisesRegex(InvalidInput, "RunsOn"):
            self.compare(self.first, self.second)


if __name__ == "__main__":
    unittest.main()
