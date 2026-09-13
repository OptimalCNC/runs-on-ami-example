import copy
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import FakeCloud, deployment, module, result, smoke, tags
from accepted_image import AcceptedImage, prepare, selected_record, verify
from example import InvalidInput


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
        job = {"id": 7, "conclusion": "success", "runner_name": "runs-on--i-22222222222222222--123"}
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}):
            self.assertEqual(verify(cloud, accepted, report, job)["status"], "passed")
            cloud.machines[0]["CurrentInstanceBootMode"] = "legacy-bios"
            with self.assertRaisesRegex(InvalidInput, "boot mode"):
                verify(cloud, accepted, report, job)
        result["cloud"]["boot_mode"] = "legacy-bios"
        with self.assertRaisesRegex(InvalidInput, "actual UEFI"):
            selected_record(result, deployment(), "1" * 64)

    def test_main_dispatch_routes_exact_accepted_ami_with_new_run_identity(self):
        environment = {"CLOUD_ENABLED": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "124", "GITHUB_RUN_ATTEMPT": "2"}
        with patch.dict(os.environ, environment), patch("accepted_image.outputs") as output:
            prepare(deployment(), self.record)
            selected = output.call_args.args[0]
            self.assertEqual(selected["build_id"], "124-2-one")
            self.assertIn("ami=ami-11111111111111111", selected["label"])
            self.assertEqual(selected["recipe_id"], "1" * 64)
            self.assertEqual(selected["kernel_release"], self.accepted.kernel_release)
        with patch.dict(os.environ, {**environment, "GITHUB_REF": "refs/heads/untrusted"}), self.assertRaises(InvalidInput):
            prepare(deployment(), self.record)

    def test_app_evidence_matches_github_instance_and_new_dispatch(self):
        report = smoke("a", "2")
        for guest in (report["identity"], report["environment"]):
            guest["sentinel"] = "/var/tmp/ami-example-124-2-one-sentinel"
        job = {"id": 7, "conclusion": "success", "runner_name": "runs-on--i-22222222222222222--124"}
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "124", "GITHUB_RUN_ATTEMPT": "2"}):
            passed = verify(accepted_cloud(), self.accepted, report, job)
            self.assertEqual(passed["build_id"], "124-2-one")
            altered = copy.deepcopy(report)
            altered["identity"]["kernel_release"] = "6.8.0-aws"
            with self.assertRaisesRegex(InvalidInput, "kernel"):
                verify(accepted_cloud(), self.accepted, altered, job)
            with self.assertRaisesRegex(InvalidInput, "compile/run"):
                verify(accepted_cloud(), self.accepted, {**report, "ctest_passed": False}, job)

    def test_single_stage_selects_one_build_and_qualification_selects_two(self):
        workflow = module("workflow")
        environment = {"CLOUD_ENABLED": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "124", "GITHUB_RUN_ATTEMPT": "1", "FAULT": "none"}
        for stage, expected in (("single", '["one"]'), ("qualification", '["one","two"]'), ("stock", '["stock"]')):
            with patch.dict(os.environ, {**environment, "BUILD_STAGE": stage}), patch.object(workflow, "load_deployment", return_value=deployment()), \
                 patch.object(workflow, "outputs") as output:
                workflow.configure()
                self.assertEqual(output.call_args.args[0]["matrix"], expected)

    def test_completed_dispatch_recovery_uses_exact_run_attempt_and_commit(self):
        workflow = module("workflow")
        watchdog = module("watchdog")
        cloud = accepted_cloud()
        source = {"content": base64.b64encode(json.dumps(self.record).encode()).decode()}
        run = {"id": 124, "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
               "name": "Run Cobalt application", "head_sha": "2" * 40, "run_attempt": 2}
        job = {"name": "test / smoke-a", "runner_name": "runs-on--i-22222222222222222--124"}
        with patch("accepted_image.watchdog_module", return_value=watchdog), \
             patch.object(watchdog.github, "request", side_effect=[run, source]) as request, \
             patch.object(watchdog.github, "jobs", return_value=[job]) as jobs:
            selected = workflow.completed_dispatch(cloud, "124", "2")
            workflow.recover_dispatch_ownership(cloud, selected)
        jobs.assert_called_once_with("example/repo", "124", "2")
        self.assertEqual(request.call_args_list[0].args[0], "/repos/example/repo/actions/runs/124/attempts/2")
        self.assertEqual(request.call_args_list[1].args[0], "/repos/example/repo/contents/accepted-image.json?ref=" + "2" * 40)
        from example import BUILD_TAG, tags_of
        self.assertEqual(tags_of(cloud.machines[0])[BUILD_TAG], "124-2-one")
        self.assertEqual(tags_of(cloud.images[0])[BUILD_TAG], "123-1-one")
        self.assertEqual(cloud.machines[1]["State"]["Name"], "running")

    def test_completed_dispatch_recovery_does_not_retag_terminated_jobs(self):
        workflow = module("workflow")
        watchdog = module("watchdog")
        cloud = accepted_cloud()
        cloud.machines[0]["State"]["Name"] = "terminated"
        cloud.images = []
        run = {"id": 124, "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
               "name": "Run Cobalt application", "head_sha": "2" * 40, "run_attempt": 1}
        job = {"name": "test / smoke-a", "runner_name": "runs-on--i-22222222222222222--124"}
        with patch("accepted_image.watchdog_module", return_value=watchdog), \
             patch.object(watchdog.github, "request", return_value=run) as request, \
             patch.object(watchdog.github, "jobs", return_value=[job]):
            workflow.recover_dispatch_ownership(cloud, workflow.completed_dispatch(cloud, "124", "1"))
        request.assert_called_once()
        self.assertEqual(cloud.mutations, [])

    def test_old_completion_cleanup_preserves_new_running_attempt(self):
        workflow = module("workflow")
        watchdog = module("watchdog")
        cloud = accepted_cloud()
        cloud.machines[0]["Tags"] = tags(build="123-1-one", purpose="test")
        cloud.machines[1]["Tags"] = tags(build="123-2-one", purpose="test")
        earlier = {"id": 123, "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
                   "name": "Build and test Cobalt AMIs", "head_sha": "2" * 40, "run_attempt": 1}
        latest = {**earlier, "status": "in_progress", "run_attempt": 2}
        def response(path):
            return earlier if path.endswith("/attempts/1") else latest
        with tempfile.TemporaryDirectory() as temporary, patch("accepted_image.watchdog_module", return_value=watchdog), \
             patch.object(watchdog.github, "request", side_effect=response) as request, \
             patch.object(watchdog.github, "jobs", return_value=[]):
            report = workflow.cleanup_workflow(cloud, Path(temporary), "123", "1")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["scope"]["run_attempt"], "1")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "terminated")
        self.assertEqual(cloud.machines[1]["State"]["Name"], "running")
        request.assert_called_once_with("/repos/example/repo/actions/runs/123/attempts/1")

    def test_manual_active_attempt_does_not_authorize_cleanup(self):
        workflow = module("workflow")
        watchdog = module("watchdog")
        cloud = accepted_cloud()
        run = {"id": 123, "event": "workflow_dispatch", "head_branch": "main", "status": "in_progress",
               "name": "Build and test Cobalt AMIs", "head_sha": "2" * 40, "run_attempt": 2}
        with patch("accepted_image.watchdog_module", return_value=watchdog), \
             patch.object(watchdog.github, "request", return_value=run), patch.object(workflow, "cleanup") as cleanup:
            with self.assertRaisesRegex(InvalidInput, "completed"):
                workflow.cleanup_workflow(cloud, Path("unused"), "123")
        cleanup.assert_not_called()
        self.assertEqual(cloud.mutations, [])

    def test_recovery_failure_still_cleans_only_proven_completed_attempt(self):
        workflow = module("workflow")
        watchdog = module("watchdog")
        cloud = accepted_cloud()
        cloud.machines[0]["Tags"] = tags(build="123-1-one", purpose="test")
        cloud.machines[1]["Tags"] = tags(build="123-2-one", purpose="test")
        run = {"id": 123, "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
               "name": "Build and test Cobalt AMIs", "head_sha": "2" * 40, "run_attempt": 1}
        with tempfile.TemporaryDirectory() as temporary, patch("accepted_image.watchdog_module", return_value=watchdog), \
             patch.object(watchdog.github, "request", return_value=run), \
             patch.object(watchdog.github, "jobs", side_effect=OSError("GitHub jobs unavailable")):
            with self.assertRaisesRegex(OSError, "GitHub jobs"):
                workflow.cleanup_workflow(cloud, Path(temporary), "123", "1")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "terminated")
        self.assertEqual(cloud.machines[1]["State"]["Name"], "running")


if __name__ == "__main__":
    unittest.main()
