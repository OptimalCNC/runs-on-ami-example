import copy
import dataclasses
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jsonschema

from support import ROOT, FakeCloud, deployment, evidence, guest, module, result, smoke, tags
from accepted_image import AcceptedImage, selected_record
from example import BUILD_TAG, InvalidInput, RUNS_ON_TAG, read_json, tags_of
from qualification import QualificationRun


QUALIFICATION = QualificationRun(
    "124-2-one", "124", "2", "example/repo/.github/workflows/qualify-retained-image.yml@refs/heads/main")


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
    jobs = evidence()
    for stage, job in jobs.items():
        job.update(name=f"one / {stage}", runner_name=job["runner_name"].removesuffix("123") + "124")
    probe = {"status": "passed", "terminated": True, "build_id": QUALIFICATION.build_id,
             "instance_id": "i-" + "4" * 17, "guest": guest("probe", "4")}
    return cloud, probe, reports, jobs


class QualificationContext(unittest.TestCase):
    def test_explicit_dispatch_preserves_original_image_execution(self):
        original = result()
        fallback = QualificationRun.from_result(original)
        self.assertEqual((fallback.build_id, fallback.run_id, fallback.run_attempt), ("123-1-one", "123", "1"))
        selected = qualification_result()
        self.assertEqual(QualificationRun.from_result(selected), QUALIFICATION)
        self.assertEqual(selected["execution"], original["execution"])

    def test_context_rejects_partial_inconsistent_and_malformed_dispatches(self):
        changes = ({"build_id": "124-1-one"}, {"run_id": "123"}, {"run_attempt": 2}, {"run_id": "0"},
                   {"build_id": "124-2-stock"}, {"workflow_ref": " "}, {"workflow_ref": None}, {"extra": True})
        for change in changes:
            selected = qualification_result()
            selected["validation"]["qualification"].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidInput):
                QualificationRun.from_result(selected)
        for value in (None, {}, {"build_id": "124-2-one"}):
            selected = qualification_result()
            selected["validation"]["qualification"] = value
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                QualificationRun.from_result(selected)

    def test_current_context_requires_exact_repository_main_manual_dispatch(self):
        environment = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_REPOSITORY": "example/repo", "GITHUB_WORKFLOW_REF": QUALIFICATION.workflow_ref,
                       "GITHUB_RUN_ID": "124", "GITHUB_RUN_ATTEMPT": "2"}
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(QualificationRun.current("example/repo"), QUALIFICATION)
            self.assertEqual(QualificationRun.current("example/repo", "two").build_id, "124-2-two")
        for field, value in (("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/topic"),
                             ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_WORKFLOW_REF", ""),
                             ("GITHUB_WORKFLOW_REF", QUALIFICATION.workflow_ref.replace("main", "topic")),
                             ("GITHUB_WORKFLOW_REF", QUALIFICATION.workflow_ref.replace("example/repo", "other/repo"))):
            with self.subTest(field=field, value=value), patch.dict(os.environ, {**environment, field: value}, clear=True), \
                 self.assertRaises(InvalidInput):
                QualificationRun.current("example/repo")

    def test_schema_allows_explicit_context_and_rejects_incomplete_context(self):
        validator = jsonschema.Draft202012Validator(read_json(ROOT / "schemas/image-result.schema.json"))
        validator.validate(qualification_result())
        for value in (None, {"build_id": "124-2-one"}, {**dataclasses.asdict(QUALIFICATION), "run_attempt": 2},
                      {**dataclasses.asdict(QUALIFICATION), "workflow_ref": " "}):
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
                 patch("sys.argv", ["probe-ami.py", "--result", "candidate.json", "--execute", *override]), \
                 patch.object(probe, "read_json", return_value=qualification_result()), \
                 patch.object(probe, "load_deployment", return_value=deployment()), patch.object(probe, "Cloud"), \
                 patch.object(probe, "probe", return_value={"instance_id": "i-44444444444444444"}) as operation, \
                 patch.object(probe, "write_json"):
                probe.main()
                self.assertEqual(operation.call_args.args[2], expected)
                self.assertEqual(operation.call_args.args[4]["execution"]["build_id"], "123-1-one")

    def test_verification_uses_new_sentinels_and_job_instances_without_rewriting_source(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, jobs = qualification_evidence()
        selected = qualification_result()
        original = copy.deepcopy(selected)
        checked = verifier.verify(cloud, selected, probe, *reports, jobs)
        self.assertEqual(checked["status"], "candidate")
        self.assertEqual(checked["execution"], original["execution"])
        self.assertEqual(checked["source"], original["source"])
        self.assertEqual(checked["validation"]["qualification"], dataclasses.asdict(QUALIFICATION))
        self.assertEqual([value["instance_id"] for value in checked["validation"]["runs_on"]],
                         [machine["InstanceId"] for machine in cloud.machines])

    def test_verification_rejects_source_dispatch_evidence_and_wrong_github_instance(self):
        verifier = module("verify-run-results")
        for fault, message in (("probe", "direct boot belongs"), ("sentinel", "sentinel"),
                               ("environment", "sentinel"), ("ownership", "ownership"), ("github", "GitHub job and guest")):
            cloud, probe, reports, jobs = qualification_evidence()
            if fault == "probe":
                probe["build_id"] = "123-1-one"
            elif fault == "sentinel":
                reports[0]["identity"]["sentinel"] = "/var/tmp/ami-example-123-1-one-sentinel"
            elif fault == "environment":
                reports[0]["environment"]["sentinel"] = "/var/tmp/ami-example-123-1-one-sentinel"
            elif fault == "ownership":
                cloud.machines[0]["Tags"] = tags(purpose="test") + [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
            else:
                jobs["smoke-a"]["runner_name"] = jobs["smoke-b"]["runner_name"]
            with self.subTest(fault=fault), self.assertRaisesRegex(InvalidInput, message):
                verifier.verify(cloud, qualification_result(), probe, *reports, jobs)

    def test_verifier_cli_queries_exact_new_run_attempt(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, jobs = qualification_evidence()
        with patch("sys.argv", ["verify-run-results.py", "--result", "candidate.json", "--probe", "probe.json",
                                "--smoke-a", "smoke-a", "--smoke-b", "smoke-b"]), \
             patch.object(verifier, "load_deployment", return_value=deployment()), patch.object(verifier, "Cloud", return_value=cloud), \
             patch.object(verifier, "read_json", side_effect=[qualification_result(), probe]), \
             patch.object(verifier, "read_smoke", side_effect=reports), \
             patch.object(verifier.github, "jobs", return_value=list(jobs.values())) as query, patch.object(verifier, "write_json") as save:
            verifier.main()
        query.assert_called_once_with("example/repo", "124", "2")
        self.assertEqual(save.call_args.args[1]["execution"]["build_id"], "123-1-one")
        self.assertEqual(len(save.call_args.args[1]["validation"]["runs_on"]), 2)

    def test_acceptance_links_new_qualification_and_inspects_original_ami_ownership(self):
        verifier = module("verify-run-results")
        cloud, probe, reports, jobs = qualification_evidence()
        checked = verifier.verify(cloud, qualification_result(), probe, *reports, jobs)
        checked["status"] = "qualified"
        checked["lifecycle"].update(retain=True, expires_at="2099-01-01T00:00:00Z",
                                   cleanup={"status": "passed", "retained_images": [checked["cloud"]["ami_id"]]})
        record = selected_record(checked, deployment(), "1" * 64)
        accepted = AcceptedImage.parse(record, deployment())
        self.assertEqual(accepted.build_id, "123-1-one")
        self.assertEqual((accepted.qualification_run_id, accepted.qualification_run_attempt), ("124", "2"))
        self.assertEqual(accepted.qualification_url, "https://github.com/example/repo/actions/runs/124/attempts/2")
        cloud.images[0].update(State="available", Architecture="x86_64", BootMode="uefi",
                               Tags=tags(expiry="2099-01-01T00:00:00Z") + [
                                   {"Key": "ami-example:recipe-id", "Value": "1" * 64},
                                   {"Key": "ami-example:retain", "Value": "true"}])
        self.assertEqual(accepted.inspect(cloud)["ImageId"], accepted.ami_id)
        record["image"]["qualification_url"] = "https://github.com/example/repo/actions/runs/123/attempts/1"
        with self.assertRaisesRegex(InvalidInput, "qualification URL"):
            AcceptedImage.parse(record, deployment())


if __name__ == "__main__":
    unittest.main()
