import copy
import contextlib
import dataclasses
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jsonschema

from support import ROOT, FakeCloud, deployment, deployment_dict, guest, module, source_inventory, result, smoke, tags
from accepted_image import AcceptedImage, selected_record
from example import BUILD_TAG, InfrastructureBindings, InvalidInput, RUNS_ON_TAG, VerifiedParent, read_json, tags_of, write_json
from qualification import QualificationRun


QUALIFICATION = QualificationRun("124-2-one")


def qualification_result():
    value = result()
    value["validation"]["qualification"] = dataclasses.asdict(QUALIFICATION)
    return value


def qualification_evidence():
    cloud = FakeCloud()
    for machine in cloud.machines:
        machine["Tags"] = tags(build=QUALIFICATION.build_id, purpose="test") + [
            {"Key": RUNS_ON_TAG, "Value": "example/repo"}]
    reports = [smoke("a", "2"), smoke("b", "3")]
    for report in reports:
        for value in (report["identity"], report["environment"]):
            value["sentinel"] = f"/var/tmp/ami-example-{QUALIFICATION.build_id}-sentinel"
    instances = {"a": "i-22222222222222222", "b": "i-33333333333333333"}
    probe = {"status": "passed", "terminated": True, "build_id": QUALIFICATION.build_id,
             "instance_id": "i-" + "4" * 17, "guest": guest("probe", "4")}
    return cloud, probe, reports, instances


class QualificationContext(unittest.TestCase):
    def test_explicit_dispatch_preserves_original_image_execution(self):
        original = result()
        fallback = QualificationRun.from_result(original)
        self.assertEqual((fallback.build_id, fallback.run_id, fallback.run_attempt), ("123-1-one", "123", "1"))
        selected = qualification_result()
        self.assertEqual(QualificationRun.from_result(selected), QUALIFICATION)
        self.assertEqual(selected["execution"], original["execution"])

    def test_context_rejects_inconsistent_and_malformed_execution_identity(self):
        changes = ({"run_id": "123"}, {"run_attempt": 2}, {"run_id": "0"},
                   {"build_id": "124-2-stock"}, {"build_id": "invalid"}, {"extra": True})
        for change in changes:
            selected = qualification_result()
            selected["validation"]["qualification"].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidInput):
                QualificationRun.from_result(selected)
        for value in (None, {}, {"run_id": "124", "run_attempt": "2"}):
            selected = qualification_result()
            selected["validation"]["qualification"] = value
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                QualificationRun.from_result(selected)

    def test_historical_context_is_readable_without_caller_metadata(self):
        selected = qualification_result()
        selected["validation"]["qualification"].update(
            run_id="124", run_attempt="2", workflow_ref="historical workflow reference")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(QualificationRun.from_result(selected), QUALIFICATION)
            self.assertEqual(QualificationRun("124-2-two").run_id, "124")
            self.assertEqual(QualificationRun("124-2-two").run_attempt, "2")

    def test_schema_allows_explicit_context_and_rejects_incomplete_context(self):
        validator = jsonschema.Draft202012Validator(read_json(ROOT / "schemas/image-result.schema.json"))
        validator.validate(qualification_result())
        for value in (None, {}, {"build_id": "124-2-stock"},
                      {**dataclasses.asdict(QUALIFICATION), "run_attempt": 2}):
            selected = qualification_result()
            selected["validation"]["qualification"] = value
            with self.subTest(value=value):
                self.assertFalse(validator.is_valid(selected))

    def test_probe_uses_original_ami_ownership_and_new_instance_and_report_ownership(self):
        probe = module("probe-ami")
        cloud = MagicMock()
        cloud.deployment = deployment()
        instance_id = "i-44444444444444444"
        cloud.call.return_value = {"Instances": [{"InstanceId": instance_id}]}
        image = {"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 80}}],
                 "Tags": tags() + [{"Key": "ami-example:recipe-id", "Value": "1" * 64}]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "inspect_ami", return_value=image), \
             patch.object(probe, "inspect_instance_type", return_value={}), patch.object(probe, "inspect_management"), \
             patch.object(probe, "wait_online", side_effect=TimeoutError("boot deadline")), \
             patch.object(probe, "diagnostics", return_value=[]):
            with self.assertRaisesRegex(TimeoutError, "boot deadline"):
                probe.probe(cloud, deployment().source_ami, QUALIFICATION.build_id, Path(temporary), qualification_result())
            launch = cloud.call.call_args_list[0].args
            self.assertEqual(launch[:2], ("ec2", "run-instances"))
            for specification in launch[2]["TagSpecifications"]:
                self.assertEqual(tags_of(specification)[BUILD_TAG], QUALIFICATION.build_id)
            self.assertEqual(tags_of(image)[BUILD_TAG], "123-1-one")
            self.assertEqual(read_json(Path(temporary) / "probe.json")["build_id"], QUALIFICATION.build_id)
            self.assertTrue(cloud.retain.call_args_list)
            self.assertTrue(all(call.args[1].startswith(QUALIFICATION.build_id + "/probe/") for call in cloud.retain.call_args_list))
            cloud.wait_terminated.assert_called_once_with([instance_id])
            cloud.call.reset_mock()
            image["Tags"] = tags(build=QUALIFICATION.build_id)
            with self.assertRaisesRegex(InvalidInput, "candidate ownership"):
                probe.probe(cloud, deployment().source_ami, QUALIFICATION.build_id, Path(temporary), qualification_result())
            cloud.call.assert_not_called()

    def test_probe_cli_selects_qualification_context_or_explicit_operation(self):
        probe = module("probe-ami")
        for override, expected in (([], QUALIFICATION.build_id), (["--build-id", "125-1-one"], "125-1-one")):
            with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "ROOT", Path(temporary)), \
                 patch("sys.argv", ["probe-ami.py", "--deployment", "manifest.json", "--result", "candidate.json", "--output", str(Path(temporary) / "probe"), "--execute", *override]), \
                 patch.object(probe, "read_json", return_value=qualification_result()), \
                 patch.object(probe, "load_deployment", return_value=deployment()), patch.object(probe, "Cloud"), \
                 patch.object(probe, "probe", return_value={"instance_id": "i-44444444444444444"}) as operation, \
                 patch.object(probe, "write_json"):
                probe.main()
                self.assertEqual(operation.call_args.args[2], expected)
                self.assertEqual(operation.call_args.args[4]["execution"]["build_id"], "123-1-one")

    def test_parent_capture_uses_bindings_and_selection_without_manifest_or_placeholder_evidence(self):
        probe = module("probe-ami")
        value = deployment_dict()
        bindings = {field.name: value[field.name] for field in dataclasses.fields(InfrastructureBindings)}
        bindings["runs_on"] = None
        selected = {"id": "ami-55555555555555555", "owner": "111111111111"}
        image = {"ImageId": selected["id"], "OwnerId": selected["owner"], "Architecture": "x86_64",
                 "BootMode": "uefi-preferred", "RootDeviceName": "/dev/sda1",
                 "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}]}
        instance = {"InstanceId": "i-44444444444444444", "ImageId": selected["id"],
                    "InstanceType": bindings["instance_type"], "CurrentInstanceBootMode": "uefi"}
        cloud = MagicMock()
        cloud.deployment = InfrastructureBindings.parse(bindings)
        cloud.instance.return_value = instance
        cloud.call.side_effect = lambda _service, operation, _payload: (
            {"Images": [image]} if operation == "describe-images" else {"Instances": [instance]})
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "bindings.json", bindings)
            write_json(directory / "selection.json", selected)
            output = directory / "capture"
            with contextlib.chdir(directory), patch.dict(os.environ, {}, clear=True), \
                 patch.object(probe, "Cloud", return_value=cloud), patch.object(probe, "load_deployment") as manifest, \
                 patch.object(probe, "inspect_ami", return_value=image), patch.object(probe, "inspect_instance_type", return_value={}), \
                 patch.object(probe, "inspect_management"), patch.object(probe, "wait_online"), \
                 patch.object(probe, "command", return_value=source_inventory()), patch.object(probe, "diagnostics", return_value=[]):
                probe.main(["--capture-inventory", "--bindings", "bindings.json", "--selection", "selection.json",
                            "--output", "capture", "--build-id", "125-1-stock", "--execute"])
            manifest.assert_not_called()
            parent = VerifiedParent.load(read_json(output / "parent.json"), output)
            self.assertEqual((parent.id, parent.owner, parent.boot_mode),
                             (selected["id"], selected["owner"], "uefi-preferred"))
            self.assertEqual(parent.inventory_path, output / "inventory.json")
            self.assertEqual(parent.inventory, source_inventory())
            self.assertTrue(read_json(output / "probe.json")["terminated"])
            cloud.wait_terminated.assert_called_once_with([instance["InstanceId"]])

    def test_verification_uses_new_sentinels_and_expected_instances_without_rewriting_source(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, instances = qualification_evidence()
        selected = qualification_result()
        original = copy.deepcopy(selected)
        checked = verifier.verify(cloud, selected, probe, *reports, instances)
        self.assertEqual(checked["status"], "candidate")
        self.assertEqual(checked["execution"], original["execution"])
        self.assertEqual(checked["source"], original["source"])
        self.assertEqual(checked["validation"]["qualification"], dataclasses.asdict(QUALIFICATION))
        self.assertEqual([value["instance_id"] for value in checked["validation"]["runs_on"]],
                         [machine["InstanceId"] for machine in cloud.machines])

    def test_verification_rejects_source_execution_evidence_and_wrong_expected_instance(self):
        verifier = module("verify-run-results")
        for fault, message in (("probe", "direct boot belongs"), ("sentinel", "sentinel"),
                               ("environment", "sentinel"), ("ownership", "ownership"), ("instance", "expected instance and guest")):
            cloud, probe, reports, instances = qualification_evidence()
            if fault == "probe":
                probe["build_id"] = "123-1-one"
            elif fault == "sentinel":
                reports[0]["identity"]["sentinel"] = "/var/tmp/ami-example-123-1-one-sentinel"
            elif fault == "environment":
                reports[0]["environment"]["sentinel"] = "/var/tmp/ami-example-123-1-one-sentinel"
            elif fault == "ownership":
                cloud.machines[0]["Tags"] = tags(purpose="test") + [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
            else:
                instances["a"] = instances["b"]
            with self.subTest(fault=fault), self.assertRaisesRegex(InvalidInput, message):
                verifier.verify(cloud, qualification_result(), probe, *reports, instances)

    def test_verifier_cli_runs_without_environment_with_explicit_instance_ids(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, instances = qualification_evidence()
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.argv", ["verify-run-results.py", "--deployment", "local.json", "--result", "candidate.json", "--probe", "probe.json",
                                "--smoke-a", "smoke-a", "--smoke-b", "smoke-b",
                                "--instance-a", instances["a"], "--instance-b", instances["b"]]), \
             patch.object(verifier, "load_deployment", return_value=deployment()) as load, \
             patch.object(verifier, "Cloud", return_value=cloud), \
             patch.object(verifier, "read_json", side_effect=[qualification_result(), probe]), \
             patch.object(verifier, "read_smoke", side_effect=reports), patch.object(verifier, "write_json") as save:
            verifier.main()
        load.assert_called_once_with("local.json")
        self.assertEqual(save.call_args.args[1]["execution"]["build_id"], "123-1-one")
        self.assertEqual(len(save.call_args.args[1]["validation"]["runs_on"]), 2)

    def test_failed_verifier_cli_does_not_replace_result(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, instances = qualification_evidence()
        reports[1]["ctest_passed"] = False
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.argv", ["verify-run-results.py", "--deployment", "manifest.json", "--result", "candidate.json", "--probe", "probe.json",
                                "--smoke-a", "smoke-a", "--smoke-b", "smoke-b",
                                "--instance-a", instances["a"], "--instance-b", instances["b"]]), \
             patch.object(verifier, "load_deployment", return_value=deployment()), patch.object(verifier, "Cloud", return_value=cloud), \
             patch.object(verifier, "read_json", side_effect=[qualification_result(), probe]), \
             patch.object(verifier, "read_smoke", side_effect=reports), patch.object(verifier, "write_json") as save:
            with self.assertRaisesRegex(InvalidInput, "application failed"):
                verifier.main()
        save.assert_not_called()

    def test_acceptance_links_new_qualification_and_inspects_original_ami_ownership(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, instances = qualification_evidence()
        checked = verifier.verify(cloud, qualification_result(), probe, *reports, instances)
        checked["status"] = "qualified"
        checked["lifecycle"].update(retain=True, expires_at="2099-01-01T00:00:00Z",
                                   cleanup={"status": "passed", "retained_images": [checked["cloud"]["ami_id"]]})
        record = selected_record(checked, deployment(), "1" * 64, "s3://audit/124-2-one/qualification.json")
        accepted = AcceptedImage.parse(record, deployment())
        self.assertEqual(accepted.build_id, "123-1-one")
        self.assertEqual((accepted.qualification_run_id, accepted.qualification_run_attempt), ("124", "2"))
        self.assertEqual(accepted.qualification_url, "s3://audit/124-2-one/qualification.json")
        cloud.images[0].update(State="available", Architecture="x86_64", BootMode="uefi",
                               Tags=tags(expiry="2099-01-01T00:00:00Z") + [
                                   {"Key": "ami-example:recipe-id", "Value": "1" * 64},
                                   {"Key": "ami-example:retain", "Value": "true"}])
        self.assertEqual(accepted.inspect(cloud)["ImageId"], accepted.ami_id)


if __name__ == "__main__":
    unittest.main()
