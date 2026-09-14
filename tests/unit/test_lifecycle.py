import copy
import dataclasses
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import FakeCloud, deployment, instance, module, result, tags
from cleanup import CleanupScope, cleanup
from example import CleanupContext, Cloud, InvalidInput, OWNER_TAG, RUNS_ON_TAG, read_json, tags_of, write_json


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

    def test_expiry_cleanup_cli_needs_only_stable_cleanup_context(self):
        cleanup_command = module("cleanup")
        cloud = FakeCloud()
        cloud.deployment = CleanupContext.parse({"account_id": "123456789012", "region": "us-east-1",
                                                "repository": "example/repo", "artifact_bucket": "example-artifacts"})
        source = self.output / "cleanup-context.json"
        write_json(source, dataclasses.asdict(cloud.deployment))
        with patch.object(cleanup_command, "Cloud", return_value=cloud), \
             patch("sys.argv", ["cleanup.py", "--cleanup-context", str(source), "--expired", "--apply",
                                "--output", str(self.output / "cleanup")]), patch("builtins.print"):
            cleanup_command.main()
        self.assertTrue(all(machine["State"]["Name"] == "terminated" for machine in cloud.machines))
        self.assertEqual(read_json(self.output / "cleanup/cleanup-report.json")["status"], "passed")

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

    def test_explicit_instance_adoption_does_not_adopt_other_users_of_same_image(self):
        runners = module("runner_instances")
        cloud = FakeCloud()
        for machine in cloud.machines:
            machine["Tags"] = [{"Key": RUNS_ON_TAG, "Value": "example/repo"}]
        observations = runners.adopt_test_instances(cloud, "123-1-one", ["i-22222222222222222"], instance_type="t3.small")
        self.assertEqual([instance["InstanceId"] for instance in observations], ["i-22222222222222222"])
        self.assertEqual(tags_of(cloud.machines[0])[OWNER_TAG], "example/repo")
        self.assertNotIn(OWNER_TAG, tags_of(cloud.machines[1]))

    def test_explicit_instance_adoption_rejects_mismatched_cloud_identity(self):
        runners = module("runner_instances")
        for changes in ({"ImageId": "ami-99999999999999999"}, {"Tags": []},
                        {"InstanceType": "c7i.2xlarge"}, {"Tags": tags(build="124-1-one")},
                        {"InstanceLifecycle": "spot"}):
            cloud = FakeCloud()
            cloud.machines[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(InvalidInput):
                runners.adopt_test_instances(cloud, "123-1-one", ["i-22222222222222222"], instance_type="t3.small")
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
    def test_downloaded_inputs_survive_packer_failure_without_duplicate_success_upload(self):
        build = module("build-image")
        for packer_fails in (False, True):
            with self.subTest(packer_fails=packer_fails), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                d = deployment()
                image = result()["payload"]
                write_json(root / "images/xenomai-cobalt/inputs.lock.json",
                           {"xenomai": image["xenomai"], "kernel": {"release": image["kernel_release"]}, "tools": {}})
                destination = root / "artifacts/123-1-one"
                cloud = MagicMock()
                cloud.call.side_effect = lambda service, operation, payload: {
                    "create-key-pair": {"KeyName": "fixture", "KeyPairId": "key-fixture", "KeyMaterial": "fixture-private-key"},
                    "delete-key-pair": {}, "describe-images": {"Images": [{
                        "ImageId": "ami-11111111111111111", "State": "available", "BootMode": "uefi",
                        "CreationDate": "2026-09-12T01:00:00Z", "BlockDeviceMappings": []}]},
                }[operation]
                uploads = {}
                def retain(path, key):
                    self.assertNotIn(key, uploads, "each artifact must create only one retained version")
                    self.assertNotIn(b"fixture-private-key", path.read_bytes())
                    uploads[key] = path.read_bytes()
                    return f"s3://example-artifacts/example/repo/{key}?versionId=immutable-version"
                cloud.retain.side_effect = retain
                def packer(command, **kwargs):
                    if command[1] == "build":
                        with tarfile.open(destination / "inputs.tar", "w") as archive:
                            member = tarfile.TarInfo("downloaded-source.txt")
                            member.size = len(b"actual downloaded inputs")
                            archive.addfile(member, io.BytesIO(b"actual downloaded inputs"))
                        if packer_fails:
                            raise subprocess.CalledProcessError(1, command)
                        write_json(destination / "image-manifest.json", image)
                        write_json(destination / "packer-manifest.json", {"builds": [{"artifact_id": "us-east-1:ami-11111111111111111"}]})
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(build, "ROOT", root), patch.object(build, "load_deployment", return_value=d), \
                     patch.object(build, "recipe", return_value=("1" * 64, {})), patch.object(build, "Cloud", return_value=cloud), \
                     patch.object(build, "inspect_deployment", return_value={"source_ami": {"RootDeviceName": "/dev/sda1"}}), \
                     patch.object(build, "controller_identity", return_value={"instance_id": "i-22222222222222222"}), \
                     patch.object(build, "run", side_effect=lambda command, **kwargs: "2" * 40 if command[1] == "rev-parse" else ""), \
                     patch.object(build.subprocess, "run", side_effect=packer), patch("preflight.inspect_ami"), \
                     patch.dict("os.environ", {}, clear=True), patch("sys.argv", ["build-image.py", "--deployment", "deployment.json", "--build-id", "123-1-one", "--output", str(destination), "--execute"]):
                    if packer_fails:
                        with self.assertRaises(subprocess.CalledProcessError):
                            build.main()
                    else:
                        build.main()
                uri = "s3://example-artifacts/example/repo/123-1-one/inputs.tar?versionId=immutable-version"
                self.assertEqual(read_json(destination / "artifact-index.json")["inputs.tar"], uri)
                with tarfile.open(fileobj=io.BytesIO(uploads["123-1-one/inputs.tar"])) as archive:
                    self.assertEqual(archive.extractfile("downloaded-source.txt").read(), b"actual downloaded inputs")
                self.assertFalse((root / ".work/123-1-one/builder-key.pem").exists())
                if not packer_fails:
                    self.assertIn(uri, read_json(destination / "image-result.json")["lifecycle"]["artifact_locations"])

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
                             "s3://example-artifacts/example/repo/reports/123-1-one/report.json?versionId=version%2Fone%2Btwo")

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
