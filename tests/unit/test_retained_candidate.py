import copy
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import ROOT, FakeCloud, deployment, guest, module, result, tags
from example import BUILD_TAG, Cloud, InvalidInput, file_sha, read_json, recipe, tags_of, write_json
from qualification import QualificationRun
from execution_record import ExecutionRecord, ImageSnapshot, RunAttempt
import retained_candidate as retained


SOURCE_URI = "s3://example-artifacts/example/repo/123-1-one/recovery/image-result.json?versionId=source-version"
CONTEXT = QualificationRun("124-2-one")


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

    def test_parent_inventory_location_is_not_a_recipe_or_admission_identity(self):
        candidate = source_candidate()
        relocated = dataclasses.replace(deployment(), source_ami=dataclasses.replace(
            deployment().source_ami, inventory_file="another/location/inventory.json"))
        self.assertEqual(recipe(read_json(ROOT / "images/xenomai-cobalt/inputs.lock.json"), relocated)[0],
                         candidate["source"]["recipe_id"])
        self.assertEqual(retained.validate_source(candidate, relocated).build_id, "123-1-one")
        candidate["source"]["parent_ami"]["inventory_sha256"] = "9" * 64
        with self.assertRaisesRegex(InvalidInput, "candidate parent identity differs"):
            retained.validate_source(candidate, relocated)

    def test_generated_and_historical_input_uris_identify_the_original_build(self):
        d = deployment()
        candidate = source_candidate()
        historical = candidate["lifecycle"]["artifact_locations"][0]
        responses = {"get-caller-identity": {"Account": d.account_id}, "get-bucket-versioning": {"Status": "Enabled"},
                     "get-bucket-location": {"LocationConstraint": None}, "head-object": {"VersionId": "generated-version"}}
        with tempfile.TemporaryDirectory() as temporary, patch("example.run"), \
             patch.object(Cloud, "call", side_effect=lambda _service, operation, *_: responses[operation]):
            archive = Path(temporary) / "inputs.tar"
            archive.write_bytes(b"locked source archive")
            generated = Cloud(d).retain(archive, "123-1-one/inputs.tar")
        self.assertEqual(generated,
                         "s3://example-artifacts/example/repo/reports/123-1-one/inputs.tar?versionId=generated-version")
        for uri in (generated, historical):
            candidate["lifecycle"]["artifact_locations"] = [uri]
            with self.subTest(uri=uri):
                self.assertEqual(retained.validate_source(candidate, d).build_id, "123-1-one")
                self.assertEqual(candidate["lifecycle"]["artifact_locations"], [uri])
        for uri in (generated.replace("123-1-one", "124-1-one"), generated.replace("example/repo/", "other/repo/"),
                    generated.replace("example-artifacts", "other-bucket"), generated.replace("/reports/", "/state/")):
            candidate["lifecycle"]["artifact_locations"] = [uri]
            with self.subTest(uri=uri), self.assertRaises(InvalidInput):
                retained.validate_source(candidate, d)

    def test_preparation_preserves_source_execution_and_writes_explicit_configuration(self):
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
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(retained, "download_json", side_effect=download), \
             patch.object(retained, "inspect_candidate") as inspect:
            destination = Path(temporary) / "qualification"
            selected = retained.prepare(cloud, CONTEXT, SOURCE_URI, destination, minimum_seconds=4500)
            self.assertEqual(read_json(destination / "image-result.json"), selected)
            configuration = read_json(destination / "image-config.json")
        self.assertEqual(selected["source"], original["source"])
        self.assertEqual(selected["execution"], original["execution"])
        self.assertEqual(selected["validation"]["qualification"], dataclasses.asdict(CONTEXT))
        self.assertEqual(selected["status"], "candidate")
        self.assertEqual(selected["lifecycle"]["artifact_locations"], original["lifecycle"]["artifact_locations"] + [SOURCE_URI])
        self.assertEqual(uploaded, {CONTEXT.build_id + "/qualification/image-result.json": selected})
        self.assertEqual(inspect.call_args.args[2], 4500)
        self.assertIn(original["cloud"]["ami_id"], configuration["label_a"])
        self.assertIn(CONTEXT.build_id, configuration["label_a"])

    def test_live_candidate_requires_original_tags_snapshots_creation_and_expiry(self):
        candidate = source_candidate()
        cloud = MagicMock(deployment=deployment())
        image = candidate_image(candidate)
        with patch.object(retained, "inspect_ami", return_value=image), patch.object(retained, "inspect_instance_type"):
            self.assertIs(retained.inspect_candidate(cloud, candidate), image)
        cloud.deployment = deployment().cleanup()
        with patch.object(retained, "inspect_ami", return_value=image), patch.object(retained, "inspect_instance_type") as runtime, \
             patch.object(retained, "inspect_root_volume") as root_volume:
            self.assertIs(retained.inspect_candidate(cloud, candidate, minimum_seconds=None, for_launch=False), image)
            runtime.assert_not_called()
            root_volume.assert_not_called()
        cloud.deployment = deployment()
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

    def execution_record(self, candidate):
        image = {**candidate_image(candidate), "BootMode": candidate["cloud"]["ami_boot_mode"]}
        return ExecutionRecord.parse({"schema_version": 1, "repository": "example/repo", "run_id": "124", "run_attempt": "2",
            "cleanup": dataclasses.asdict(deployment().cleanup()), "configuration": None, "instance_type": "t3.small",
            "build_id": CONTEXT.build_id, "deadline_seconds": 45 * 60, "registration_seconds": 600,
            "plan": {"configure": {"name": "Prepare selected image", "needs": [], "adopt": False},
                     "probe": {"name": "Boot selected image", "needs": ["configure"], "adopt": False},
                     "test": {"name": "Run selected image", "needs": ["probe"], "adopt": True}},
            "terminal": "test", "image": ImageSnapshot.capture(image).as_dict()})

    def test_retained_monitor_uses_only_the_explicit_plan_and_retains_admission_failures(self):
        watchdog = module("watchdog")
        candidate = source_candidate()
        record = self.execution_record(candidate)
        jobs = {"configure": {"conclusion": "success"}, "probe": {"status": "in_progress"}}
        self.assertEqual(watchdog.registration_waiting(jobs, record.plan), [])
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(ImageSnapshot, "inspect", side_effect=InvalidInput("ownership changed")), \
             self.assertRaisesRegex(InvalidInput, "ownership changed"):
            try:
                watchdog.monitor(cloud, record, Path(temporary))
            finally:
                self.assertEqual(read_json(Path(temporary) / "watchdog.json")["error"], "ownership changed")
        self.assertEqual(cloud.retained, ["124-2-one/watchdog.json"])

    def test_recovery_uses_registered_historical_image_and_exact_instance(self):
        watchdog = module("watchdog")
        candidate = source_candidate()
        candidate["source"]["recipe_files"]["historical-recipe"] = "f" * 64
        record = self.execution_record(candidate)
        cloud = FakeCloud()
        cloud.deployment = cloud.deployment.cleanup()
        cloud.machines[0]["Tags"] = [{"Key": "ami-example:runs-on-repository", "Value": "example/repo"}]
        job = {"name": "Run selected image", "runner_name": "runs-on--i-22222222222222222--124"}
        attempt = RunAttempt.parse("example/repo", "124", "2")
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(watchdog.github, "completed", return_value=attempt) as completed, \
             patch.object(watchdog.github, "jobs", return_value=[job]) as jobs, \
             patch.object(watchdog, "registered_records", return_value=[record]), \
             patch.object(ImageSnapshot, "inspect", return_value=candidate_image(candidate)) as inspect:
            watchdog.recover(cloud, "example/repo", "124", "2", Path(temporary))
        completed.assert_called_once_with("example/repo", "124", "2")
        jobs.assert_called_once_with("example/repo", "124", "2")
        inspect.assert_called_once_with(cloud)
        self.assertEqual(tags_of(cloud.machines[0])[BUILD_TAG], CONTEXT.build_id)
        self.assertEqual(tags_of(cloud.machines[1])[BUILD_TAG], "123-1-one")
        self.assertEqual(tags_of(cloud.images[0])[BUILD_TAG], "123-1-one")

    def test_retained_monitor_enforces_its_supplied_deadline_and_retains_the_failure(self):
        watchdog = module("watchdog")
        candidate = source_candidate()
        record = self.execution_record(candidate)
        cloud = FakeCloud()
        jobs = [{"name": "Prepare selected image", "conclusion": "success"},
                {"name": "Boot selected image", "status": "in_progress"}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(ImageSnapshot, "inspect", return_value=candidate_image(candidate)), \
             patch.object(watchdog.github, "jobs", return_value=jobs), \
             patch.object(watchdog, "adopt_test_instances", return_value=[]), \
             patch.object(watchdog.time, "monotonic", side_effect=[0, record.deadline_seconds]):
            with self.assertRaisesRegex(InvalidInput, "execution deadline"):
                watchdog.monitor(cloud, record, Path(temporary))
            report = read_json(Path(temporary) / "watchdog.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(cloud.retained, ["124-2-one/watchdog.json"])


class Finalization(unittest.TestCase):
    def setUp(self):
        self.finalizer = module("finalize-image")
        self.candidate = source_candidate()
        self.candidate["validation"]["qualification"] = dataclasses.asdict(CONTEXT)
        validation = self.candidate["validation"]
        validation["direct_boot"] = {"status": "passed", "build_id": CONTEXT.build_id,
                                     "instance_id": "i-44444444444444444", "terminated": True,
                                     "guest": guest("probe", "4")}
        validation["runs_on"] = []
        for stage, digit in (("a", "2"), ("b", "3")):
            identity = guest(stage, digit)
            identity["sentinel"] = f"/var/tmp/ami-example-{CONTEXT.build_id}-sentinel"
            validation["runs_on"].append({"status": "passed", "stage": stage, "guest": identity,
                                           "instance_id": identity["identity"]["instanceId"],
                                           "application": {"name": "cobalt", "status": "passed"}})
        self.report = {"status": "passed", "account_id": deployment().account_id, "region": deployment().region,
                       "scope": {"owner": deployment().repository, "build": CONTEXT.build_id,
                                 "run_id": None, "run_attempt": None, "expired": False},
                       "remaining": {"instances": [], "images": [], "snapshots": [], "volumes": [], "key_pairs": []},
                       "errors": [], "retained_images": []}

    def test_retained_source_is_only_claimed_after_live_readback(self):
        for failure in (None, InvalidInput("source ownership changed")):
            with self.subTest(failure=failure), \
                 patch.object(self.finalizer, "inspect_candidate", side_effect=failure) as inspect:
                checked = self.finalizer.finalize(FakeCloud(), CONTEXT.build_id, self.candidate, self.report)
                inspect.assert_called_once()
                self.assertEqual(checked["execution"], self.candidate["execution"])
                self.assertEqual(checked["status"], "failed" if failure else "qualified")
                self.assertEqual(checked["lifecycle"]["cleanup"]["retained_images"],
                                 [] if failure else [self.candidate["cloud"]["ami_id"]])
        self.assertEqual(self.candidate["status"], "candidate")

    def test_status_alone_cannot_replace_runtime_or_execution_evidence(self):
        for fault in ("pending-probe", "wrong-probe-build", "one-stage", "reused-instance", "wrong-image", "failed-application"):
            value = copy.deepcopy(self.candidate)
            validation = value["validation"]
            if fault == "pending-probe":
                validation["direct_boot"]["status"] = "pending"
            elif fault == "wrong-probe-build":
                validation["direct_boot"]["build_id"] = "124-1-one"
            elif fault == "one-stage":
                validation["runs_on"].pop()
            elif fault == "reused-instance":
                second = validation["runs_on"][1]
                second["instance_id"] = validation["runs_on"][0]["instance_id"]
                second["guest"]["identity"]["instanceId"] = second["instance_id"]
            elif fault == "wrong-image":
                validation["runs_on"][0]["guest"]["identity"]["imageId"] = "ami-99999999999999999"
            else:
                validation["runs_on"][0]["application"]["status"] = "failed"
            with self.subTest(fault=fault), patch.object(self.finalizer, "inspect_candidate") as inspect:
                checked = self.finalizer.finalize(FakeCloud(), CONTEXT.build_id, value, self.report)
                self.assertEqual(checked["status"], "failed")
                self.assertTrue(checked["validation"]["errors"])
                self.assertEqual(checked["lifecycle"]["cleanup"]["status"], "passed")
                inspect.assert_not_called()

    def test_same_execution_retention_requires_the_image_in_cleanup_evidence(self):
        self.candidate["execution"]["build_id"] = CONTEXT.build_id
        for retained_images in ([], [self.candidate["cloud"]["ami_id"]]):
            self.report["retained_images"] = retained_images
            with self.subTest(retained_images=retained_images), patch.object(self.finalizer, "inspect_candidate") as inspect:
                checked = self.finalizer.finalize(FakeCloud(), CONTEXT.build_id, self.candidate, self.report)
                self.assertEqual(checked["status"], "qualified" if retained_images else "failed")
                inspect.assert_not_called()

    def test_cleanup_must_cover_the_same_cloud_target_and_execution_without_leftovers(self):
        for fault in ("pending", "wrong-build", "wrong-owner", "wrong-account", "wrong-region", "leftover", "error"):
            report = copy.deepcopy(self.report)
            if fault == "pending":
                report["status"] = "planned"
            elif fault == "wrong-build":
                report["scope"]["build"] = "124-1-one"
            elif fault == "wrong-owner":
                report["scope"]["owner"] = "other/repo"
            elif fault == "wrong-account":
                report["account_id"] = "999999999999"
            elif fault == "wrong-region":
                report["region"] = "us-west-2"
            elif fault == "leftover":
                report["remaining"]["instances"] = ["i-22222222222222222"]
            else:
                report["errors"] = ["artifact upload failed"]
            with self.subTest(fault=fault), patch.object(self.finalizer, "inspect_candidate") as inspect:
                checked = self.finalizer.finalize(FakeCloud(), CONTEXT.build_id, self.candidate, report)
                self.assertEqual(checked["status"], "failed")
                self.assertEqual(checked["lifecycle"]["cleanup"]["status"], "failed")
                inspect.assert_not_called()

    def test_missing_cleanup_writes_a_failed_result_from_standalone_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "verified.json"
            destination = root / "final.json"
            write_json(source, self.candidate)
            args = ["finalize-image.py", "--deployment", str(root / "deployment.json"),
                    "--build-id", CONTEXT.build_id, "--result", str(source),
                    "--cleanup", str(root / "missing.json"), "--output", str(destination)]
            with patch("sys.argv", args), self.assertRaisesRegex(InvalidInput, "qualification failed"):
                self.finalizer.main()
            failed = read_json(destination)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["execution"], self.candidate["execution"])


class ArtifactRetention(unittest.TestCase):
    def test_retains_diagnostics_and_index_without_application_build_products(self):
        retention = module("retain-artifacts")
        cloud = FakeCloud()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("identity.json", "tests/ctest.xml", "build/generated.json", "input.tar", "runtime.log", "artifact-index.json"):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            locations = retention.retain(cloud, directory, "local/application")
            self.assertEqual(set(locations), {"identity.json", "tests/ctest.xml", "runtime.log"})
            self.assertEqual(read_json(directory / "artifact-index.json"), locations)
            self.assertEqual(cloud.retained[-1], "local/application/artifact-index.json")
            self.assertEqual(len(cloud.retained), 4)


if __name__ == "__main__":
    unittest.main()
