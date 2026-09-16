import copy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

EXECUTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXECUTION))
import execute
from contract import ExecutionPlan
from fixtures import image_value, installation_value


def plan():
    return ExecutionPlan.parse(installation_value(), image_value(), "ExampleOrg/project")


def identity():
    return {"accountId": "123456789012", "region": "us-east-1", "imageId": "ami-0123456789abcdef0",
            "instanceId": "i-0123456789abcdef0", "instanceType": "t3.small", "architecture": "x86_64"}


def profile():
    return {"Code": "Success", "InstanceProfileArn": plan().installation.runner_profile_arn}


def manifest_value():
    return {
        "schema_version": 1, "recipe_id": "c" * 64, "kernel_release": "6.12.90-cip24-xenomai-cobalt",
        "config_sha256": "1" * 64, "packages_sha256": "2" * 64,
        "xenomai": {"core": "cobalt", "prefix": "/usr/xenomai", "version": "3.3.3"}, "snap_hashes": {},
        "runner_inventory": {"runner_version": "2.337.0", "runner_listener_sha256": "3" * 64,
                             "bootstrap_files": {"/usr/local/bin/runs-on-bootstrap-v0.1.12": "4" * 64}},
        "normalized_configuration": {str(execute.REPORTER): "5" * 64},
    }


class MetadataTests(unittest.TestCase):
    def test_imdsv2_requests_identity_and_profile_without_credentials_or_proxies(self):
        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                bodies = [b"token", json.dumps(identity()).encode(), json.dumps(profile()).encode()]
                return io.BytesIO(bodies[len(self.requests) - 1])

        opener = Opener()
        with patch.object(execute.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(execute.ec2_metadata(), (identity(), profile()))
        self.assertEqual(build.call_args.args[0].proxies, {})
        requests = opener.requests
        self.assertEqual([item[0].full_url for item in requests], [
            "http://169.254.169.254/latest/api/token",
            "http://169.254.169.254/latest/dynamic/instance-identity/document",
            "http://169.254.169.254/latest/meta-data/iam/info",
        ])
        self.assertEqual(requests[0][0].method, "PUT")
        self.assertTrue(all(timeout == 5 for _, timeout in requests))
        self.assertTrue(all(request.get_header("X-aws-ec2-metadata-token") == "token" for request, _ in requests[1:]))

    def test_wrong_image_profile_or_machine_cannot_become_execution_instance(self):
        expected = plan()
        matched = execute.Instance.from_metadata(identity(), profile()).for_plan(expected)
        self.assertEqual(matched.observed.ami_id, expected.image.ami_id)
        for key, value in (("accountId", "999999999999"), ("region", "us-west-2"),
                           ("imageId", "ami-00000000000000000"), ("instanceType", "t3.medium"),
                           ("architecture", "arm64")):
            with self.subTest(key=key):
                changed = {**identity(), key: value}
                with self.assertRaisesRegex(ValueError, "differs"):
                    execute.Instance.from_metadata(changed, profile()).for_plan(expected)
        with self.assertRaisesRegex(ValueError, "runner_profile_arn"):
            execute.Instance.from_metadata(identity(), {**profile(), "InstanceProfileArn": "other-profile"}).for_plan(expected)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan()
        self.manifest = execute.ImageManifest.parse(manifest_value(), self.plan.image)
        self.instance = execute.Instance.from_metadata(identity(), profile()).for_plan(self.plan)
        self.guest = {"schema_version": 1, **self.manifest.expected_report, "identity": identity()}

    def test_runtime_payload_and_exact_instance_match_the_published_manifest(self):
        accepted = execute.GuestEvidence.parse(self.guest, self.manifest, self.instance)
        self.assertEqual(accepted.report["identity"]["imageId"], self.plan.image.ami_id)

    def test_runtime_changes_cannot_be_qualified(self):
        for key, value in (("kernel_release", "stock-kernel"), ("recipe_id", "a" * 64),
                           ("config_sha256", "b" * 64), ("packages_sha256", "b" * 64),
                           ("runner_listener_sha256", "b" * 64), ("boot_mode", "legacy-bios"),
                           ("environment_passed", False), ("environment_passed", 1), ("bootstrap_files", {})):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    execute.GuestEvidence.parse({**self.guest, key: value}, self.manifest, self.instance)
        changed = copy.deepcopy(self.guest)
        changed["identity"]["instanceId"] = "i-00000000000000000"
        with self.assertRaisesRegex(ValueError, "instanceId"):
            execute.GuestEvidence.parse(changed, self.manifest, self.instance)

    def test_bootstrap_manifest_must_match_publication_compatibility(self):
        changed = manifest_value()
        changed["runner_inventory"]["bootstrap_files"] = {"/usr/local/bin/runs-on-bootstrap-v9.9.9": "a" * 64}
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            execute.ImageManifest.parse(changed, self.plan.image)

    def test_manifest_digest_is_checked_before_content_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_value()))
            path.chmod(0o644)
            original_fstat = os.fstat

            def root_owned(fd):
                fields = list(original_fstat(fd))
                fields[4] = 0
                return os.stat_result(fields)

            with patch.object(execute.os, "fstat", side_effect=root_owned):
                with self.assertRaisesRegex(ValueError, "digest differs"):
                    execute.ImageManifest.load(path, self.plan.image)
                image = replace(self.plan.image, manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(execute.ImageManifest.load(path, image).recipe_id, image.recipe_id)
                path.chmod(0o666)
                with self.assertRaisesRegex(ValueError, "root-owned"):
                    execute.ImageManifest.load(path, image)


class ApplicationTests(unittest.TestCase):
    def result(self, xml):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctest.xml"
            path.write_text(xml)
            return execute.ApplicationEvidence.load(path)

    def test_only_executed_cobalt_with_primary_mode_marker_is_accepted(self):
        case = '<testcase name="cobalt" status="run"><system-out>' + execute.SUCCESS_MARKER + '</system-out></testcase>'
        self.assertEqual(self.result('<testsuite tests="1" failures="0">' + case + '</testsuite>').executed_tests, 1)
        invalid = [
            '<testsuite/>',
            '<testsuite>' + case + case + '</testsuite>',
            '<testsuite>' + case.replace('name="cobalt"', 'name="other"') + '</testsuite>',
            '<testsuite>' + case.replace('status="run"', 'status="notrun"') + '</testsuite>',
            '<testsuite>' + case.replace(execute.SUCCESS_MARKER, 'compiled successfully') + '</testsuite>',
            '<testsuite errors="1">' + case + '</testsuite>',
            '<testsuite tests="2">' + case + '</testsuite>',
        ]
        invalid.extend('<testsuite>' + case.replace('</testcase>', f'<{tag}/></testcase>') + '</testsuite>'
                       for tag in ('failure', 'error', 'skipped'))
        for xml in invalid:
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                self.result(xml)


class FailureEvidenceTests(unittest.TestCase):
    def test_inputs_failure_still_writes_failed_execution_without_cloud_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs.json"
            inputs.write_text("{}")
            output = root / "reports"
            with self.assertRaisesRegex(ValueError, "GitHub repository"):
                execute.execute(inputs, output, root=root, environment={})
            result = yaml.safe_load((output / "execution.yaml").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["stage"], "inputs")
            self.assertNotIn("instance", result)
            self.assertNotIn("application", result)
            self.assertIn("finished_at", result)

    def test_source_checkout_must_match_the_triggering_sha(self):
        run = execute.GitHubRun("ExampleOrg/project", "a" * 40, "123", "1", "https://github.com/run")
        with patch.object(execute.subprocess, "check_output", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "HEAD differs"):
                execute.checked_out_source(run, Path("/unused"))

    def test_failed_command_keeps_log_and_reaps_the_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "command.log"
            with self.assertRaises(subprocess.CalledProcessError):
                execute.run_logged([sys.executable, "-c", "print('partial evidence', flush=True); raise SystemExit(7)"],
                                   log, timeout=5, root=root)
            self.assertEqual(log.read_text(), "partial evidence\n")

    def test_timeout_stops_command_and_preserves_partial_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "command.log"
            with self.assertRaises(subprocess.TimeoutExpired):
                execute.run_logged([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                                   log, timeout=0.1, root=root)
            self.assertEqual(log.read_text(), "started\n")

    def test_sigterm_persists_failed_result_and_reaps_active_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "reports"
            inputs = root / "inputs.json"
            inputs.write_text(json.dumps(plan().as_inputs()))
            harness = root / "harness.py"
            harness.write_text(f'''
import os, pathlib, signal, sys
sys.path.insert(0, {str(EXECUTION)!r})
import execute
execute.signal.signal(signal.SIGTERM, execute.interrupted)
execute.runner_identity = lambda: {{"uid": 1001, "username": "runner"}}
execute.checked_out_source = lambda run, root: run.sha
def metadata():
    command = [sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(60)"]
    execute.run_logged(command, pathlib.Path({str(output / "active.log")!r}), timeout=60, root=pathlib.Path({str(root)!r}))
execute.ec2_metadata = metadata
raise SystemExit(execute.main(["--inputs", {str(inputs)!r}, "--output", {str(output)!r}]))
''')
            environment = {**os.environ, "GITHUB_REPOSITORY": "ExampleOrg/project", "GITHUB_SHA": "a" * 40,
                           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            process = subprocess.Popen([sys.executable, str(harness)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    log = output / "active.log"
                    if log.exists() and log.read_text().strip():
                        child = int(log.read_text().strip())
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(child, "test command did not start")
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=10), 130)
                result = yaml.safe_load((output / "execution.yaml").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["type"], "KeyboardInterrupt")
                self.assertEqual(result["image"]["ami_id"], plan().image.ami_id)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if child is not None:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
