import copy
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import ROOT, FakeCloud, deployment, module, result, tags
from example import BUILD_TAG, InvalidInput, file_sha, read_json, recipe, tags_of, write_json
from qualification import QualificationRun
import retained_candidate as retained


SOURCE_URI = "s3://example-artifacts/example/repo/123-1-one/recovery/image-result.json?versionId=source-version"
REQUEST_URI = "s3://example-artifacts/example/repo/124-2-one/qualification/request.json?versionId=request-version"
CONTEXT = QualificationRun("124-2-one", "124", "2",
                           "example/repo/.github/workflows/qualify-retained-image.yml@refs/heads/main")
ENVIRONMENT = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
               "GITHUB_REPOSITORY": "example/repo", "GITHUB_WORKFLOW_REF": CONTEXT.workflow_ref,
               "GITHUB_RUN_ID": "124", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "3" * 40,
               "CONFIGURED_BUILD_ID": CONTEXT.build_id, "CANDIDATE_RESULT_URI": SOURCE_URI, "BUILD_VARIANT": "one"}


def source_candidate():
    value = result()
    lock_path = ROOT / "images/xenomai-cobalt/inputs.lock.json"
    recipe_id, hashes = recipe(read_json(lock_path), deployment())
    value["source"].update(recipe_id=recipe_id, recipe_files=hashes, input_lock_sha256=file_sha(lock_path))
    value["payload"]["recipe_id"] = recipe_id
    value["lifecycle"].update(retain=True, expires_at="2099-01-01T00:00:00Z",
                              artifact_locations=["s3://example-artifacts/example/repo/123-1-one/inputs.tar?versionId=inputs-version"])
    return value


def candidate_image(value):
    return {"ImageId": value["cloud"]["ami_id"], "CreationDate": value["lifecycle"]["created_at"],
            "RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [
                {"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": value["cloud"]["snapshot_ids"][0], "VolumeSize": 80}}],
            "Tags": tags(expiry=value["lifecycle"]["expires_at"]) + [
                {"Key": "ami-example:recipe-id", "Value": value["source"]["recipe_id"]},
                {"Key": "ami-example:retain", "Value": "true"}]}


class RetainedCandidateContract(unittest.TestCase):
    def test_immutable_reference_rejects_wrong_scope_and_ambiguous_versions(self):
        reference = retained.VersionedArtifact.parse(SOURCE_URI, deployment())
        self.assertEqual((reference.bucket, reference.key, reference.version),
                         ("example-artifacts", "example/repo/123-1-one/recovery/image-result.json", "source-version"))
        invalid = [SOURCE_URI.replace("example-artifacts", "other-bucket"),
                   SOURCE_URI.replace("example/repo/", "other/repo/"),
                   SOURCE_URI.replace("123-1-one/", "../"), SOURCE_URI.replace("123-1-one/", "%2e%2e/"),
                   SOURCE_URI.replace("123-1-one/", "123-1-one//"), SOURCE_URI.split("?")[0],
                   SOURCE_URI.replace("source-version", "null"), SOURCE_URI.replace("source-version", ""),
                   SOURCE_URI + "&versionId=other", SOURCE_URI + "&extra=value", SOURCE_URI + "#fragment"]
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(InvalidInput):
                retained.VersionedArtifact.parse(uri, deployment())

    def test_download_binds_version_and_account_and_rejects_different_response(self):
        cloud = MagicMock(deployment=deployment())
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "candidate.json"
            write_json(target, source_candidate())
            with patch.object(retained, "run", return_value=json.dumps({"VersionId": "source-version"})) as fetch:
                self.assertEqual(retained.download_json(cloud, SOURCE_URI, target)["status"], "candidate")
                command = fetch.call_args.args[0]
                self.assertEqual(command[command.index("--version-id") + 1], "source-version")
                self.assertEqual(command[command.index("--expected-bucket-owner") + 1], deployment().account_id)
                cloud.artifacts.assert_called_once()
            with patch.object(retained, "run", return_value=json.dumps({"VersionId": "other"})), \
                 self.assertRaisesRegex(InvalidInput, "different candidate evidence version"):
                retained.download_json(cloud, SOURCE_URI, target)

    def test_source_requires_exact_recipe_for_launch_but_allows_historical_cleanup(self):
        candidate = source_candidate()
        self.assertEqual(retained.validate_source(candidate, deployment()).build_id, "123-1-one")
        candidate["source"]["recipe_files"]["old-recipe"] = "f" * 64
        with self.assertRaisesRegex(InvalidInput, "configured locked inputs"):
            retained.validate_source(candidate, deployment())
        candidate["source"]["parent_ami"]["id"] = "ami-" + "9" * 17
        candidate["execution"]["runs_on_version"] = "3.1.0"
        candidate["cloud"]["instance_type"] = "t3.medium"
        self.assertEqual(retained.validate_source(candidate, deployment(), for_launch=False).build_id, "123-1-one")
        candidate["payload"]["recipe_id"] = "e" * 64
        with self.assertRaisesRegex(InvalidInput, "recipe identities"):
            retained.validate_source(candidate, deployment(), for_launch=False)

    def test_preparation_preserves_source_execution_and_retains_new_request(self):
        workflow = module("workflow")
        original = source_candidate()
        cloud = MagicMock(deployment=deployment())
        uploaded = {}
        def retain(path, key):
            uploaded[key] = read_json(path)
            return "s3://example-artifacts/example/repo/" + key + "?versionId=retained-version"
        cloud.retain.side_effect = retain
        def download(_cloud, uri, path):
            self.assertEqual(uri, SOURCE_URI)
            write_json(path, original)
            return copy.deepcopy(original)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, ENVIRONMENT, clear=True), \
             patch.object(workflow, "ROOT", Path(temporary)), patch.object(workflow, "load_deployment", return_value=deployment()), \
             patch.object(workflow, "Cloud", return_value=cloud), patch.object(retained, "download_json", side_effect=download), \
             patch.object(retained, "inspect_candidate") as inspect, patch.object(workflow, "outputs") as outputs:
            workflow.qualification_prepare()
            selected = read_json(Path(temporary) / "artifacts" / CONTEXT.build_id / "image-result.json")
        self.assertEqual(selected["source"], original["source"])
        self.assertEqual(selected["execution"], original["execution"])
        self.assertEqual(selected["validation"]["qualification"], dataclasses.asdict(CONTEXT))
        self.assertEqual(selected["status"], "candidate")
        self.assertEqual(selected["lifecycle"]["artifact_locations"], original["lifecycle"]["artifact_locations"] + [SOURCE_URI])
        self.assertEqual(uploaded[CONTEXT.build_id + "/qualification/request.json"],
                         {"build_id": CONTEXT.build_id, "workflow_sha": ENVIRONMENT["GITHUB_SHA"], "source_result_uri": SOURCE_URI})
        self.assertEqual(inspect.call_args.args[2], deployment().deadlines["boot_seconds"] +
                         2 * deployment().deadlines["registration_seconds"] + 1800)
        self.assertIn(original["cloud"]["ami_id"], outputs.call_args.args[0]["label_a"])
        self.assertIn(CONTEXT.build_id, outputs.call_args.args[0]["label_a"])

    def test_live_candidate_requires_original_tags_snapshots_creation_and_expiry(self):
        candidate = source_candidate()
        cloud = MagicMock(deployment=deployment())
        image = candidate_image(candidate)
        with patch.object(retained, "inspect_ami", return_value=image), patch.object(retained, "inspect_instance_type"):
            self.assertIs(retained.inspect_candidate(cloud, candidate), image)
        with patch.object(retained, "inspect_ami", return_value=image), patch.object(retained, "inspect_instance_type") as runtime, \
             patch.object(retained, "inspect_root_volume") as root_volume:
            self.assertIs(retained.inspect_candidate(cloud, candidate, minimum_seconds=None, for_launch=False), image)
            runtime.assert_not_called()
            root_volume.assert_not_called()
        for fault in ("build", "retention", "recipe", "snapshot", "creation", "expiry"):
            candidate = source_candidate()
            image = candidate_image(candidate)
            if fault in ("build", "retention", "recipe"):
                key = {"build": BUILD_TAG, "retention": "ami-example:retain", "recipe": "ami-example:recipe-id"}[fault]
                next(tag for tag in image["Tags"] if tag["Key"] == key)["Value"] = "different"
            elif fault == "snapshot":
                image["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"] = "snap-" + "9" * 17
            elif fault == "creation":
                image["CreationDate"] = "2026-09-12T01:01:00Z"
            else:
                candidate["lifecycle"]["expires_at"] = "2020-01-01T00:00:00Z"
                image = candidate_image(candidate)
            with self.subTest(fault=fault), patch.object(retained, "inspect_ami", return_value=image), \
                 patch.object(retained, "inspect_instance_type"), self.assertRaises(InvalidInput):
                retained.inspect_candidate(cloud, candidate)

    def test_retained_watchdog_has_no_phantom_build_deadline_and_cancels_on_admission_failure(self):
        watchdog = module("watchdog")
        jobs = {"configure": {"conclusion": "success"}, "probe": {"status": "in_progress"}}
        self.assertEqual(watchdog.registration_waiting(jobs, retained=True), [])
        self.assertEqual(watchdog.registration_waiting(jobs), ["build"])
        candidate = source_candidate()
        candidate["validation"]["qualification"] = dataclasses.asdict(CONTEXT)
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as temporary, patch.object(retained, "inspect_candidate", side_effect=InvalidInput("expired")), \
             patch.object(watchdog.github, "cancel") as cancel:
            with self.assertRaisesRegex(InvalidInput, "expired"):
                watchdog.monitor(cloud, "one", "124", "2", Path(temporary), result=candidate)
            self.assertEqual(read_json(Path(temporary) / "watchdog.json")["error"], "expired")
        cancel.assert_called_once_with("example/repo", "124")
        self.assertEqual(cloud.retained, ["124-2-one/watchdog.json"])

    def test_recovery_binds_completed_attempt_request_commit_and_exact_job_instance(self):
        workflow = module("workflow")
        watchdog = module("watchdog")
        candidate = source_candidate()
        cloud = FakeCloud()
        cloud.deployment = dataclasses.replace(cloud.deployment, instance_type="t3.medium",
                                                source_ami=dataclasses.replace(cloud.deployment.source_ami, id="ami-" + "9" * 17))
        candidate["source"]["recipe_files"]["historical-recipe"] = "f" * 64
        cloud.machines[0]["Tags"] = [{"Key": "ami-example:runs-on-repository", "Value": "example/repo"}]
        job = {"name": "one / smoke-a", "runner_name": "runs-on--i-22222222222222222--124"}
        run = {"id": 124, "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
               "name": "Qualify retained Cobalt image", "head_sha": "3" * 40, "run_attempt": 2}
        request = {"build_id": CONTEXT.build_id, "workflow_sha": run["head_sha"], "source_result_uri": SOURCE_URI}
        with tempfile.TemporaryDirectory() as temporary, patch.object(workflow, "ROOT", Path(temporary)), \
             patch("accepted_image.watchdog_module", return_value=watchdog), \
             patch.object(watchdog.github, "request", return_value=run), \
             patch.object(watchdog.github, "jobs", return_value=[job]) as jobs, \
             patch.object(retained, "request_uri", return_value=REQUEST_URI) as pointer, \
             patch.object(retained, "download_json", side_effect=[request, candidate]) as download, \
             patch.object(retained, "inspect_candidate", return_value=candidate_image(candidate)):
            dispatch = workflow.completed_dispatch(cloud, "124", "2")
            workflow.recover_dispatch_ownership(cloud, dispatch)
        jobs.assert_called_once_with("example/repo", "124", "2")
        pointer.assert_called_once_with(cloud, CONTEXT.build_id)
        self.assertEqual([call.args[1] for call in download.call_args_list], [REQUEST_URI, SOURCE_URI])
        self.assertEqual(tags_of(cloud.machines[0])[BUILD_TAG], CONTEXT.build_id)
        self.assertEqual(tags_of(cloud.machines[1])[BUILD_TAG], "123-1-one")
        self.assertEqual(tags_of(cloud.images[0])[BUILD_TAG], "123-1-one")
        for fault in ({"build_id": "124-1-one"}, {"workflow_sha": "4" * 40}):
            cloud.mutations.clear()
            with self.subTest(fault=fault), patch("accepted_image.watchdog_module", return_value=watchdog), \
                 patch.object(watchdog.github, "jobs", return_value=[job]), \
                 patch.object(retained, "request_uri", return_value=REQUEST_URI), \
                 patch.object(retained, "download_json", return_value={**request, **fault}), \
                 self.assertRaisesRegex(InvalidInput, "another run attempt or commit"):
                workflow.recover_dispatch_ownership(cloud, dispatch)
            self.assertEqual(cloud.mutations, [])

    def test_retained_deadline_cancels_before_retention_within_hosted_job_limit(self):
        watchdog = module("watchdog")
        candidate = source_candidate()
        candidate["validation"]["qualification"] = dataclasses.asdict(CONTEXT)
        cloud = FakeCloud()
        self.assertEqual(cloud.deployment.deadlines["workflow_seconds"], 14400)
        events = []
        jobs = [{"name": "one / configure", "conclusion": "success"},
                {"name": "one / probe", "status": "in_progress"}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(retained, "inspect_candidate", return_value=candidate_image(candidate)), \
             patch.object(watchdog.github, "jobs", return_value=jobs), \
             patch.object(watchdog, "adopt_test_instances", return_value=[]), \
             patch.object(watchdog.time, "monotonic", side_effect=[0, 45 * 60]), \
             patch.object(watchdog.github, "cancel", side_effect=lambda *args: events.append("cancel")), \
             patch.object(cloud, "retain", side_effect=lambda *args: events.append("retain")):
            with self.assertRaisesRegex(InvalidInput, "workflow deadline"):
                watchdog.monitor(cloud, "one", "124", "2", Path(temporary), result=candidate)
            report = read_json(Path(temporary) / "watchdog.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(events, ["cancel", "retain"])

    def test_finalizer_only_claims_retained_source_after_live_readback(self):
        workflow = module("workflow")
        for failure in (None, InvalidInput("source ownership changed")):
            candidate = source_candidate()
            candidate["validation"]["qualification"] = dataclasses.asdict(CONTEXT)
            cloud = FakeCloud()
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_json(root / "artifacts/handoff" / (CONTEXT.build_id + "-build/image-result.json"), candidate)
                write_json(root / "artifacts/handoff" / (CONTEXT.build_id + "-watchdog/watchdog.json"), {"status": "passed"})
                with patch.dict(os.environ, ENVIRONMENT, clear=True), patch.object(workflow, "ROOT", root), \
                     patch.object(workflow, "load_deployment", return_value=deployment()), patch.object(workflow, "Cloud", return_value=cloud), \
                     patch.object(workflow.subprocess, "run"), \
                     patch.object(workflow, "cleanup", return_value={"status": "passed", "retained_images": []}) as cleanup, \
                     patch.object(retained, "inspect_candidate", side_effect=failure) as inspect:
                    if failure:
                        with self.assertRaisesRegex(InvalidInput, "qualification failed"):
                            workflow.finalize()
                    else:
                        workflow.finalize()
                checked = read_json(root / "artifacts/final" / CONTEXT.build_id / "image-result.json")
                self.assertEqual(cleanup.call_args.args[1].build, CONTEXT.build_id)
                inspect.assert_called_once()
                self.assertEqual(checked["execution"], candidate["execution"])
                self.assertEqual(checked["status"], "failed" if failure else "qualified")
                self.assertEqual(checked["lifecycle"]["cleanup"]["retained_images"], [] if failure else [candidate["cloud"]["ami_id"]])


if __name__ == "__main__":
    unittest.main()
