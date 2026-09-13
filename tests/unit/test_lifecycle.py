import copy
import dataclasses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import FakeCloud, deployment, instance, module, result, tags
from cleanup import CleanupScope, cleanup
from example import Cloud, InvalidInput, OWNER_TAG, RUNS_ON_TAG, tags_of


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.scope = CleanupScope.parse("example/repo", build="123-1-one")

    def test_dry_run_does_not_mutate(self):
        cloud = FakeCloud()
        self.assertEqual(cleanup(cloud, self.scope, self.output)["status"], "planned")
        self.assertEqual(cloud.mutations, [])

    def test_attempt_scope_contains_all_variants_but_no_other_attempts(self):
        scope = CleanupScope.parse("example/repo", run_id="123", run_attempt="1")
        for variant in ("one", "two", "stock"):
            self.assertTrue(scope.includes({"Tags": tags(build=f"123-1-{variant}")}))
            self.assertFalse(scope.includes({"Tags": tags(build=f"123-2-{variant}")}))
        with self.assertRaises(InvalidInput):
            CleanupScope.parse("example/repo", expired=True, run_attempt="1")

    def test_only_selected_build_is_deleted(self):
        cloud = FakeCloud()
        cloud.machines += [instance("9", Tags=tags(build="124-1-one"), ImageId="ami-99999999999999999"),
                           instance("8", Tags=tags(owner="other/repo"), ImageId="ami-88888888888888888")]
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(cloud.instance("i-" + "9" * 17)["State"]["Name"], "running")
        self.assertEqual(cloud.instance("i-" + "8" * 17)["State"]["Name"], "running")

    def test_stack_marker_alone_never_transfers_runner_ownership(self):
        cloud = FakeCloud()
        cloud.machines[0]["Tags"] = [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "running")
        self.assertNotIn(OWNER_TAG, tags_of(cloud.machines[0]))
        other = FakeCloud()
        other.machines[0]["Tags"] = []
        report = cleanup(other, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(other.machines[0]["State"]["Name"], "running")
        self.assertTrue(other.images)

    def test_other_dispatch_using_retained_image_survives_original_build_cleanup(self):
        cloud = FakeCloud()
        cloud.images[0]["Tags"].append({"Key": "ami-example:retain", "Value": "true"})
        cloud.machines[0]["Tags"] = tags(build="124-1-one", purpose="test")
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "running")
        self.assertTrue(cloud.images)
        dispatch = CleanupScope.parse("example/repo", build="124-1-one")
        report = cleanup(cloud, dispatch, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "terminated")
        self.assertTrue(cloud.images)
        self.assertTrue(cloud.snapshots)

    def test_expired_image_waits_for_other_dispatch_without_adopting_it(self):
        cloud = FakeCloud()
        cloud.images[0]["Tags"].append({"Key": "ami-example:retain", "Value": "true"})
        cloud.machines[0]["Tags"] = tags(build="124-1-one", purpose="test", expiry="2099-01-01T00:00:00Z")
        report = cleanup(cloud, CleanupScope.parse("example/repo", expired=True), self.output, apply=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "running")
        self.assertTrue(cloud.images)
        self.assertTrue(cloud.snapshots)

    def test_github_job_adoption_does_not_adopt_other_users_of_same_image(self):
        watchdog = module("watchdog")
        cloud = FakeCloud()
        for machine in cloud.machines:
            machine["Tags"] = [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
        selected = {"smoke-a": {"runner_name": "runs-on--i-22222222222222222--123"}}
        observations = watchdog.adopt_test_instances(cloud, "123-1-one", selected)
        self.assertEqual([instance["InstanceId"] for instance in observations], ["i-22222222222222222"])
        self.assertEqual(tags_of(cloud.machines[0])[OWNER_TAG], "example/repo")
        self.assertNotIn(OWNER_TAG, tags_of(cloud.machines[1]))

    def test_github_job_adoption_rejects_mismatched_cloud_identity(self):
        watchdog = module("watchdog")
        for changes in ({"ImageId": "ami-99999999999999999"}, {"Tags": []},
                        {"InstanceType": "c7i.2xlarge"}, {"Tags": tags(build="124-1-one")},
                        {"InstanceLifecycle": "spot"}):
            cloud = FakeCloud()
            cloud.machines[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InvalidInput):
                watchdog.adopt_test_instances(cloud, "123-1-one", {"smoke-a": {"runner_name": "runs-on--i-22222222222222222--123"}})
            self.assertEqual(cloud.mutations, [])

    def test_explicit_retention_keeps_image_and_snapshot_but_stops_instances(self):
        cloud = FakeCloud()
        cloud.images[0]["Tags"].append({"Key": "ami-example:retain", "Value": "true"})
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(cloud.images)
        self.assertTrue(cloud.snapshots)
        self.assertTrue(all(i["State"]["Name"] == "terminated" for i in cloud.machines))
        expired = CleanupScope.parse("example/repo", expired=True)
        report = cleanup(cloud, expired, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertFalse(cloud.images)

    def test_existing_foreign_build_tags_are_never_overwritten(self):
        cloud = FakeCloud()
        cloud.machines[0]["Tags"] = tags(build="124-1-one") + [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(cloud.machines[0]["State"]["Name"], "running")
        self.assertFalse(any(operation == "create-tags" for operation, _ in cloud.mutations))

    def test_referenced_snapshot_never_deleted(self):
        cloud = FakeCloud()
        shared = copy.deepcopy(cloud.images[0])
        shared["ImageId"] = "ami-99999999999999999"
        shared["Tags"] = []
        cloud.images.append(shared)
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["protected_snapshots"], ["snap-11111111111111111"])
        self.assertTrue(cloud.snapshots)
        self.assertFalse(any(name == "delete-snapshot" for name, _ in cloud.mutations))

    def test_retention_failure_still_terminates_compute_and_preserves_image(self):
        cloud = FakeCloud()
        cloud.fail_retention = True
        with self.assertRaises(OSError):
            cleanup(cloud, self.scope, self.output, apply=True)
        self.assertTrue(all(i["State"]["Name"] == "terminated" for i in cloud.machines))
        self.assertTrue(cloud.images)
        self.assertTrue(cloud.snapshots)

    def test_orphan_volumes_and_keys_are_removed(self):
        cloud = FakeCloud()
        cloud.volumes = [{"VolumeId": "vol-11111111111111111", "Tags": tags(purpose="builder"), "State": "available", "Attachments": []}]
        cloud.keys = [{"KeyPairId": "key-11111111111111111", "Tags": tags(purpose="builder")}]
        report = cleanup(cloud, self.scope, self.output, apply=True)
        self.assertEqual(report["status"], "passed")
        self.assertFalse(cloud.volumes)
        self.assertFalse(cloud.keys)

    def test_missing_or_malformed_expiry_is_never_swept(self):
        expired = CleanupScope.parse("example/repo", expired=True)
        self.assertFalse(expired.includes({"Tags": tags(expiry="not-a-time")}))
        self.assertFalse(expired.includes({"Tags": tags(expiry="2099-01-01T00:00:00Z")}))


class Deadlines(unittest.TestCase):
    def test_ssm_output_accepts_both_aws_url_forms_and_rejects_other_buckets(self):
        parse = module("probe-ami").ssm_output_key
        prefix = "example/repo/ssm/123-1-one"
        for value in (f"https://bucket.s3.us-east-1.amazonaws.com/{prefix}/stdout",
                      f"https://s3.us-east-1.amazonaws.com/bucket/{prefix}/stdout"):
            self.assertEqual(parse(value, "bucket", "us-east-1", prefix), prefix + "/stdout")
        with self.assertRaises(InvalidInput):
            parse(f"https://s3.us-east-1.amazonaws.com/other/{prefix}/stdout", "bucket", "us-east-1", prefix)
        with self.assertRaises(InvalidInput):
            parse("https://bucket.s3.us-east-1.amazonaws.com/unrelated/stdout", "bucket", "us-east-1", prefix)

    def test_queued_job_clock_starts_only_after_prerequisite(self):
        watchdog = module("watchdog")
        self.assertEqual(watchdog.registration_waiting({"build": {"status": "in_progress"}, "smoke-a": {"status": "queued"}}), [])
        self.assertEqual(watchdog.registration_waiting({"probe": {"conclusion": "success"}, "smoke-a": {"status": "queued"}}), ["smoke-a"])

    def test_never_registered_runner_is_cancelled_externally(self):
        watchdog = module("watchdog")
        cloud = FakeCloud()
        cloud.deployment = dataclasses.replace(deployment(), deadlines={"boot_seconds": 60, "registration_seconds": 60, "workflow_seconds": 600})
        counter = [0]
        jobs = [{"name": "qualify (one) / one / configure", "conclusion": "success", "status": "completed"},
                {"name": "qualify (one) / one / build", "status": "queued", "conclusion": None}]
        with tempfile.TemporaryDirectory() as temporary, patch.object(watchdog.github, "jobs", return_value=jobs), \
             patch.object(watchdog.github, "cancel") as cancel, patch.object(watchdog, "adopt_test_instances", return_value=[]), \
             patch.object(watchdog.time, "monotonic", side_effect=lambda: counter[0]), \
             patch.object(watchdog.time, "sleep", side_effect=lambda seconds: counter.__setitem__(0, counter[0] + seconds)):
            with self.assertRaisesRegex(InvalidInput, "registration"):
                watchdog.monitor(cloud, "one", "123", "1", Path(temporary))
            cancel.assert_called_once_with("example/repo", "123")
            self.assertTrue(cloud.retained)

    def test_failed_management_probe_terminates_in_finally(self):
        probe = module("probe-ami")
        cloud = MagicMock()
        cloud.deployment = deployment()
        instance_id = "i-44444444444444444"
        def call(service, operation, payload):
            if operation == "run-instances":
                return {"Instances": [{"InstanceId": instance_id}]}
            if operation == "terminate-instances":
                return {}
            raise AssertionError(operation)
        cloud.call.side_effect = call
        image = {"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 80}}],
                 "Tags": tags() + [{"Key": "ami-example:recipe-id", "Value": "1" * 64}]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "inspect_ami", return_value=image), \
             patch.object(probe, "inspect_instance_type", return_value={"BurstablePerformanceSupported": True}), \
             patch.object(probe, "inspect_management"), \
             patch.object(probe, "wait_online", side_effect=TimeoutError("boot deadline")), patch.object(probe, "diagnostics", return_value=[]):
            with self.assertRaises(TimeoutError):
                probe.probe(cloud, deployment().source_ami, "123-1-one", Path(temporary), result())
            cloud.wait_terminated.assert_called_once_with([instance_id])
            self.assertTrue((Path(temporary) / "probe.json").exists())

    def test_initial_inventory_uses_small_standard_instance_and_parent_disk(self):
        probe = module("probe-ami")
        cloud = MagicMock()
        cloud.deployment = dataclasses.replace(deployment(), runs_on=None)
        cloud.call.return_value = {"Instances": [{"InstanceId": "i-44444444444444444"}]}
        image = {"RootDeviceName": "/dev/sda1", "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 30}}]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "inspect_ami", return_value=image), \
             patch.object(probe, "inspect_instance_type", return_value={"BurstablePerformanceSupported": True}), \
             patch.object(probe, "inspect_management"), patch.object(probe, "wait_online", side_effect=TimeoutError("boot deadline")), \
             patch.object(probe, "diagnostics", return_value=[]):
            with self.assertRaises(TimeoutError):
                probe.probe(cloud, deployment().source_ami, "123-1-stock", Path(temporary))
        launch = next(call.args[2] for call in cloud.call.call_args_list if call.args[1] == "run-instances")
        self.assertEqual(launch["InstanceType"], "t3.small")
        self.assertEqual(launch["CreditSpecification"], {"CpuCredits": "standard"})
        self.assertEqual(launch["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"], 30)
        self.assertTrue(launch["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"])
        self.assertEqual(launch["IamInstanceProfile"]["Name"], deployment().probe_profile_name)
        self.assertEqual((launch["MinCount"], launch["MaxCount"]), (1, 1))


class Artifacts(unittest.TestCase):
    @staticmethod
    def responses(service, operation, payload=None):
        return {"get-caller-identity": {"Account": "123456789012"}, "head-bucket": {},
                "get-bucket-versioning": {"Status": "Enabled"}, "get-bucket-location": {"LocationConstraint": None},
                "head-object": {"VersionId": "version/one+two"}}[operation]

    def test_retained_artifact_identifies_immutable_version(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(Cloud, "call", side_effect=self.responses), patch("example.run"):
            artifact = Path(temporary) / "report.json"
            artifact.write_text("{}\n")
            cloud = Cloud(deployment())
            self.assertEqual(cloud.retain(artifact, "123-1-one/report.json"),
                             "s3://example-artifacts/example/repo/123-1-one/report.json?versionId=version%2Fone%2Btwo")

    def test_unversioned_bucket_is_rejected_before_upload(self):
        def responses(service, operation, payload=None):
            return {"Status": "Suspended"} if operation == "get-bucket-versioning" else self.responses(service, operation, payload)
        with tempfile.TemporaryDirectory() as temporary, patch.object(Cloud, "call", side_effect=responses), patch("example.run") as upload:
            artifact = Path(temporary) / "report.json"
            artifact.write_text("{}\n")
            cloud = Cloud(deployment())
            with self.assertRaisesRegex(InvalidInput, "versioning"):
                cloud.retain(artifact, "123-1-one/report.json")
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
