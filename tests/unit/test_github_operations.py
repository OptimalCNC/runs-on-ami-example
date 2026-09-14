import dataclasses
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from support import FakeCloud, module
from example import BUILD_TAG, InvalidInput, OWNER_TAG, RUNS_ON_TAG, VersionedBucket, read_json, tags_of, write_json
from execution_record import ExecutionRecord, ImageSelection, JobPlan, RunAttempt, registered_records, retain_record

github = module("github-api")
watchdog = module("watchdog")


def record(**changes):
    value = {"schema_version": 1, "repository": "example/repo", "run_id": "123", "run_attempt": "1",
             "account_id": "123456789012", "region": "us-east-1", "instance_type": "t3.small",
             "build_id": "123-1-one", "plan": {
                 "ready": {"name": "Admission", "needs": [], "adopt": False},
                 "check": {"name": "Runtime acceptance", "needs": ["ready"], "adopt": True}},
             "terminal": "check", "deadline_seconds": 600, "registration_seconds": 60, "image": None}
    value.update(changes)
    return ExecutionRecord.parse(value)


def job(name="Runtime acceptance", digit="2", **changes):
    value = {"name": name, "id": 9, "runner_name": f"runs-on--i-{digit * 17}--123",
             "status": "completed", "conclusion": "success"}
    value.update(changes)
    return value


class GitHubEvidence(unittest.TestCase):
    def test_explicit_job_mapping_ignores_similar_names_and_keeps_instance_evidence(self):
        selected = github.selected_jobs([job(), job(name="Other caller / Runtime acceptance", digit="3")], record().plan)
        self.assertEqual(list(selected), ["check"])
        self.assertEqual(selected["check"]["instance_id"], "i-" + "2" * 17)
        self.assertEqual(selected["check"]["id"], 9)
        with self.assertRaisesRegex(InvalidInput, "ambiguous"):
            github.selected_jobs([job(), job()], record().plan)

    def test_hosted_job_runner_names_are_not_interpreted_as_ec2_instances(self):
        selected = github.selected_jobs([job(name="Admission", runner_name="GitHub Actions 1")], record().plan)
        self.assertNotIn("instance_id", selected["ready"])
        with self.assertRaises(InvalidInput):
            github.selected_jobs([job(runner_name="runs-on--not-an-instance--123")], record().plan)

    def test_jobs_pagination_preserves_exact_attempt(self):
        pages = [[job()] * 100, [job(digit="3")]]
        with patch.object(github, "request", side_effect=[{"jobs": value} for value in pages]) as request:
            self.assertEqual(len(github.jobs("example/repo", "123", "2")), 101)
        self.assertEqual([call.args[0] for call in request.call_args_list], [
            f"/repos/example/repo/actions/runs/123/attempts/2/jobs?per_page=100&page={number}" for number in (1, 2)])

    def test_completion_proves_requested_attempt_without_workflow_names_or_source_sha(self):
        value = {"id": 123, "run_attempt": 1, "status": "completed"}
        with patch.object(github, "request", return_value=value) as request:
            self.assertEqual(github.completed("example/repo", "123", "1"), RunAttempt.parse("example/repo", "123", "1"))
        request.assert_called_once_with("/repos/example/repo/actions/runs/123/attempts/1")
        for changes in ({"run_attempt": 2}, {"id": 124}, {"status": "in_progress"}):
            with self.subTest(changes=changes), patch.object(github, "request", return_value={**value, **changes}), self.assertRaises(InvalidInput):
                github.completed("example/repo", "123", "1")

    def test_jobs_cli_uses_only_explicit_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / "execution.json", Path(temporary) / "jobs.json"
            write_json(source, record().as_dict())
            with patch.dict("os.environ", {}, clear=True), patch.object(github, "jobs", return_value=[job()]) as fetch, \
                 patch("sys.argv", ["github-api.py", "jobs", "--record", str(source), "--output", str(target)]):
                github.main()
            fetch.assert_called_once_with("example/repo", "123", "1")
            self.assertEqual(read_json(target)["check"]["instance_id"], "i-" + "2" * 17)


class ExecutionRecords(unittest.TestCase):
    def test_plan_rejects_unresolvable_or_ambiguous_job_dependencies(self):
        for change in ({"needs": ["missing"]}, {"needs": ["check"]}, {"name": "Admission"}, {"adopt": "true"}):
            plan = record().plan.as_dict()
            plan["check"].update(change)
            with self.subTest(change=change), self.assertRaises(InvalidInput):
                JobPlan.parse(plan)
        plan = record().plan.as_dict()
        plan["ready"]["needs"] = ["check"]
        with self.assertRaisesRegex(InvalidInput, "cycle"):
            JobPlan.parse(plan)

    def test_record_rejects_another_attempt_before_registration(self):
        with self.assertRaisesRegex(InvalidInput, "another run attempt"):
            record(build_id="123-2-one")

    def test_registration_retains_exact_plan_and_image_selection(self):
        cloud = FakeCloud()
        selected = record(image={"kind": "candidate", "record": {"selected_image": "fixture"}})
        with tempfile.TemporaryDirectory() as temporary, patch.object(ImageSelection, "inspect") as inspect, \
             patch.object(watchdog, "retain_record") as retain:
            target = Path(temporary) / "execution.json"
            watchdog.register(cloud, selected, target)
            self.assertEqual(ExecutionRecord.parse(read_json(target)), selected)
        inspect.assert_called_once_with(cloud)
        retain.assert_called_once_with(cloud, selected, target)

    def test_registration_creates_once_and_identical_retries_reuse_the_record(self):
        records_module = module("execution_record")
        cloud = FakeCloud()
        selected = record()
        bucket = VersionedBucket("example-artifacts", "123456789012", "us-east-1")
        conflict = subprocess.CalledProcessError(254, ["aws"], stderr="An error occurred (PreconditionFailed) when calling PutObject")
        with tempfile.TemporaryDirectory() as temporary, patch.object(cloud, "artifacts", return_value=bucket, create=True):
            target = Path(temporary) / "execution.json"
            write_json(target, selected.as_dict())
            with patch.object(records_module, "run", return_value='{"VersionId":"original-version"}') as upload:
                first = retain_record(cloud, selected, target)
            self.assertEqual(upload.call_args.args[0][upload.call_args.args[0].index("--if-none-match") + 1], "*")
            with patch.object(records_module, "run", side_effect=conflict), \
                 patch.object(records_module, "download_record", return_value=(selected, "original-version")):
                self.assertEqual(retain_record(cloud, selected, target), first)
                with self.assertRaisesRegex(InvalidInput, "different plan or image"):
                    retain_record(cloud, record(deadline_seconds=90), target)

    def test_recovery_records_are_loaded_from_the_selected_prefix_and_immutable_version(self):
        records_module = module("execution_record")
        selected = record()
        cloud = FakeCloud()
        bucket = VersionedBucket("example-artifacts", "123456789012", "us-east-1")
        key = "example/repo/executions/123/1/123-1-one.json"
        calls = []
        def call(service, operation, payload):
            calls.append((operation, payload))
            return {"Contents": [{"Key": key}]} if operation == "list-objects-v2" else {"VersionId": "original-version"}
        def download(command, **kwargs):
            self.assertEqual(command[command.index("--version-id") + 1], "original-version")
            self.assertEqual(command[command.index("--key") + 1], key)
            write_json(command[-1], selected.as_dict())
            return json.dumps({"VersionId": "original-version"})
        with tempfile.TemporaryDirectory() as temporary, patch.object(cloud, "artifacts", return_value=bucket, create=True), \
             patch.object(cloud, "call", side_effect=call), patch.object(records_module, "run", side_effect=download):
            self.assertEqual(registered_records(cloud, selected.attempt, Path(temporary)), [selected])
        self.assertEqual(calls[0][1]["Prefix"], "example/repo/executions/123/1/")


class Monitoring(unittest.TestCase):
    def test_registration_clock_starts_after_all_explicit_prerequisites(self):
        selected = record()
        self.assertEqual(watchdog.registration_waiting({"ready": {"status": "in_progress"}}, selected.plan), [])
        self.assertEqual(watchdog.registration_waiting({"ready": {"conclusion": "success"}}, selected.plan), ["check"])
        self.assertEqual(watchdog.registration_waiting({"ready": {"conclusion": "success"}, "check": {"status": "in_progress"}}, selected.plan), [])

    def test_registration_timeout_is_reported_without_cancelling_or_cleaning_resources(self):
        cloud = FakeCloud()
        counter = [0]
        jobs = [job(name="Admission", runner_name="", conclusion="success"), job(runner_name="", status="queued", conclusion=None)]
        with tempfile.TemporaryDirectory() as temporary, patch.object(watchdog.github, "jobs", return_value=jobs), \
             patch.object(watchdog.github, "cancel") as cancel, \
             patch.object(watchdog.time, "monotonic", side_effect=lambda: counter[0]), \
             patch.object(watchdog.time, "sleep", side_effect=lambda seconds: counter.__setitem__(0, counter[0] + seconds)):
            with self.assertRaisesRegex(InvalidInput, "registration"):
                watchdog.monitor(cloud, record(), Path(temporary))
            self.assertIn("registration", read_json(Path(temporary) / "watchdog.json")["error"])
        cancel.assert_not_called()
        self.assertEqual(cloud.mutations, [])
        self.assertEqual(cloud.retained, ["123-1-one/watchdog.json"])

    def test_independent_deadline_applies_when_no_job_is_runnable(self):
        cloud = FakeCloud()
        counter = [0]
        with tempfile.TemporaryDirectory() as temporary, patch.object(watchdog.github, "jobs", return_value=[]), \
             patch.object(watchdog.time, "monotonic", side_effect=lambda: counter[0]), \
             patch.object(watchdog.time, "sleep", side_effect=lambda seconds: counter.__setitem__(0, counter[0] + seconds)):
            with self.assertRaisesRegex(InvalidInput, "independent execution"):
                watchdog.monitor(cloud, record(deadline_seconds=30), Path(temporary))
            self.assertEqual(counter[0], 30)

    def test_successful_job_keeps_cloud_observation_for_its_exact_instance(self):
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as temporary, patch.object(watchdog.github, "jobs", return_value=[job()]):
            report = watchdog.monitor(cloud, record(), Path(temporary))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(list(report["observed_instances"]), ["i-" + "2" * 17])


class OwnershipRecovery(unittest.TestCase):
    def test_active_attempt_is_rejected_before_discovery_or_mutation(self):
        cloud = FakeCloud()
        with patch.object(watchdog.github, "request", return_value={"id": 123, "run_attempt": 1, "status": "in_progress"}), \
             patch.object(watchdog, "registered_records") as records, self.assertRaisesRegex(InvalidInput, "completed"):
            watchdog.recover(cloud, "example/repo", "123", "1", Path("unused"))
        records.assert_not_called()
        self.assertEqual(cloud.mutations, [])

    def test_completed_attempt_recovers_only_registered_jobs_and_uses_saved_selection(self):
        cloud = FakeCloud()
        cloud.deployment = dataclasses.replace(cloud.deployment, instance_type="t3.medium")
        for machine in cloud.machines:
            machine["Tags"] = [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
        selected = record(run_id="124", run_attempt="2", build_id="124-2-one",
                          image={"kind": "accepted", "record": {"saved": "selection"}})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(watchdog.github, "completed", return_value=selected.attempt) as completed, \
             patch.object(watchdog, "registered_records", return_value=[selected]), \
             patch.object(watchdog.github, "jobs", return_value=[job(), job(name="Different run", digit="3")]) as jobs, \
             patch.object(ImageSelection, "inspect", return_value=(cloud.images[0], "2099-01-01T00:00:00Z", "t3.medium")) as inspect:
            report = watchdog.recover(cloud, "example/repo", "124", "2", Path(temporary))
        completed.assert_called_once_with("example/repo", "124", "2")
        jobs.assert_called_once_with("example/repo", "124", "2")
        inspect.assert_called_once_with(cloud, recovery=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(tags_of(cloud.machines[0])[BUILD_TAG], "124-2-one")
        self.assertNotIn(OWNER_TAG, tags_of(cloud.machines[1]))
        self.assertTrue(all(machine["State"]["Name"] == "running" for machine in cloud.machines))

    def test_terminated_runner_does_not_require_an_image_that_may_have_been_cleaned_up(self):
        cloud = FakeCloud()
        cloud.machines[0]["State"]["Name"] = "terminated"
        selected = record(image={"kind": "accepted", "record": {"saved": "selection"}})
        with tempfile.TemporaryDirectory() as temporary, patch.object(watchdog.github, "completed", return_value=selected.attempt), \
             patch.object(watchdog, "registered_records", return_value=[selected]), \
             patch.object(watchdog.github, "jobs", return_value=[job()]), patch.object(ImageSelection, "inspect") as inspect:
            self.assertEqual(watchdog.recover(cloud, "example/repo", "123", "1", Path(temporary))["status"], "passed")
        inspect.assert_not_called()
        self.assertEqual(cloud.mutations, [])

    def test_recovery_failure_retains_evidence_and_leaves_cleanup_to_the_caller(self):
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as temporary, patch.object(watchdog.github, "completed", return_value=record().attempt), \
             patch.object(watchdog, "registered_records", side_effect=OSError("S3 unavailable")):
            with self.assertRaisesRegex(OSError, "S3 unavailable"):
                watchdog.recover(cloud, "example/repo", "123", "1", Path(temporary))
            self.assertEqual(read_json(Path(temporary) / "recovery.json")["status"], "failed")
        self.assertEqual(cloud.mutations, [])
        self.assertEqual(cloud.retained, ["recovery/123/1/recovery.json"])


if __name__ == "__main__":
    unittest.main()
