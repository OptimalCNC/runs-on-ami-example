import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


IMAGES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(IMAGES))
from validate import image as validate
from contracts import sha256


class Evidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        manifest = {
            "kernel_release": "6.12.90-cip24-xenomai-cobalt", "recipe_id": "a" * 64,
            "config_sha256": "b" * 64, "xenomai": {"version": "3.3.3", "core": "cobalt", "prefix": "/usr/xenomai"},
            "packages_sha256": "c" * 64, "snap_hashes": {},
            "runner_inventory": {"runner_version": "2.337.0", "runner_listener_sha256": "d" * 64,
                                 "bootstrap_files": {"/usr/local/bin/runs-on-bootstrap-v0.1.12": "e" * 64}},
        }
        self.image = SimpleNamespace(manifest=manifest, compatibility=SimpleNamespace(boot_mode="uefi"))
        self.guest = {**manifest, **manifest["runner_inventory"], "boot_mode": "uefi", "runner_uid": 1001, "process_limits_passed": True}
        self.write_guest()
        (self.output / "ctest.xml").write_text('<testsuite><testcase name="cobalt" status="run"/></testsuite>')

    def write_guest(self):
        (self.output / "guest-report.json").write_text(json.dumps(self.guest))

    def test_accepts_runtime_evidence_for_the_built_artifact(self):
        self.assertEqual(validate.validate_evidence(self.image, self.output), self.guest)

    def test_rejects_guest_with_other_payload_boot_mode_or_missing_process_limits(self):
        for key, wrong in (("kernel_release", "stock-kernel"), ("recipe_id", "f" * 64),
                           ("config_sha256", "f" * 64), ("packages_sha256", "f" * 64),
                           ("runner_listener_sha256", "f" * 64), ("boot_mode", "legacy-bios"),
                           ("runner_uid", 0), ("process_limits_passed", False)):
            with self.subTest(key=key):
                correct = self.guest[key]
                self.guest[key] = wrong
                self.write_guest()
                with self.assertRaisesRegex(ValueError, key):
                    validate.validate_evidence(self.image, self.output)
                self.guest[key] = correct

    def test_skipped_failed_missing_or_extra_tests_cannot_qualify(self):
        for tests in ('', '<testcase name="different"/>', '<testcase name="cobalt"><skipped/></testcase>',
                      '<testcase name="cobalt"><failure/></testcase>', '<testcase name="cobalt"><error/></testcase>',
                      '<testcase name="cobalt" status="notrun"/>', '<testcase name="cobalt"/><testcase name="cobalt"/>'):
            with self.subTest(tests=tests):
                (self.output / "ctest.xml").write_text(f'<testsuite>{tests}</testsuite>')
                with self.assertRaisesRegex(ValueError, "successfully executed"):
                    validate.validate_evidence(self.image, self.output)

    def test_vm_evidence_cannot_claim_ec2_identity(self):
        self.guest["identity"] = {"instanceId": "i-123"}
        self.write_guest()
        with self.assertRaisesRegex(ValueError, "EC2 identity"):
            validate.validate_evidence(self.image, self.output)


class Cleanup(unittest.TestCase):
    def test_sigterm_cli_reaps_vm_and_removes_temporary_secrets(self):
        script = '''
import json, signal, sys, tempfile
from pathlib import Path
from validate import image as validate

def work():
    with tempfile.TemporaryDirectory(prefix="cobalt-signal-test-") as directory:
        temporary = Path(directory)
        for name in ("ssh-key", "seed.img", "disk.qcow2"):
            (temporary / name).write_text("temporary validation input")
        with validate.running_vm(
                [sys.executable, "-c", "import time; time.sleep(60)"], temporary / "qemu.log") as vm:
            print(json.dumps({"pid": vm.pid, "directory": directory}), flush=True)
            signal.pause()

validate.main = work
raise SystemExit(validate.cli())
'''
        controller = subprocess.Popen([sys.executable, "-c", script], cwd=IMAGES,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        state = None
        try:
            ready, _, _ = select.select([controller.stdout], [], [], 10)
            self.assertTrue(ready, "validator did not report the running VM")
            line = controller.stdout.readline()
            self.assertTrue(line, "validator exited before starting its VM")
            state = json.loads(line)
            self.assertTrue(Path(state["directory"]).is_dir())
            controller.send_signal(signal.SIGTERM)
            _, error = controller.communicate(timeout=15)
            self.assertEqual(controller.returncode, 130, error)
            self.assertEqual(error, "VM validation interrupted.\n")
            self.assertFalse(Path(state["directory"]).exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(state["pid"], 0)
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.communicate()
            if state is not None:
                try:
                    os.kill(state["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                shutil.rmtree(state["directory"], ignore_errors=True)

    def test_real_vm_process_is_reaped_when_guest_work_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            process = None
            with self.assertRaisesRegex(RuntimeError, "guest failed"):
                with validate.running_vm(
                        [sys.executable, "-c", "import time; time.sleep(60)"], Path(temporary) / "qemu.log") as process:
                    self.assertIsNone(process.poll())
                    raise RuntimeError("guest failed")
            self.assertIsNotNone(process.poll())

    def test_missing_tool_records_failure_bound_to_unchanged_disk(self):
        import yaml
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            disk = directory / "disk.raw"
            disk.write_bytes(b"finalized disk")
            image = SimpleNamespace(disk_path=disk, disk_sha256=sha256(disk), recipe_id="a" * 64)
            output = directory / "reports"
            with patch.object(validate.shutil, "which", return_value=None), \
                    self.assertRaisesRegex(ValueError, "missing"):
                validate.validate(image, output, accelerator="tcg", cpus=1, memory_mib=512, timeout_seconds=10)
            result = yaml.safe_load((output / "validation.yaml").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["disk_unchanged"])
            self.assertEqual(result["artifact"]["disk_sha256"], sha256(disk))

    def test_modified_input_disk_prevents_passed_evidence(self):
        import yaml
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            disk = directory / "disk.raw"
            disk.write_bytes(b"finalized disk")
            image = SimpleNamespace(disk_path=disk, disk_sha256=sha256(disk), recipe_id="a" * 64)
            output = directory / "reports"

            def unexpected_change(_):
                disk.write_bytes(b"modified disk")
                return None

            with patch.object(validate.shutil, "which", side_effect=unexpected_change), \
                    self.assertRaisesRegex(ValueError, "disk changed"):
                validate.validate(image, output, accelerator="tcg", cpus=1, memory_mib=512, timeout_seconds=10)
            result = yaml.safe_load((output / "validation.yaml").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["disk_unchanged"])


class GuestTransfer(unittest.TestCase):
    def test_upload_contains_the_application_and_validation_owned_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = SimpleNamespace(manifest={"kernel_release": "cobalt"}, recipe_id="a" * 64)
            with patch.object(validate.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=b"", stderr=b"")):
                validate.run_guest(image, ["ssh"], directory, directory,
                                   time.monotonic() + 10, io.BytesIO())
            with tarfile.open(directory / "test.tar") as archive:
                self.assertEqual(set(archive.getnames()), {
                    "cobalt/CMakeLists.txt", "cobalt/main.c", "validate-guest.sh", "guest-report.py",
                })
                command = archive.extractfile("validate-guest.sh").read().decode()
                self.assertIn('python3 "$directory/guest-report.py"', command)
                self.assertNotIn("/usr/local/bin/ami-example-guest-report", command)
                self.assertNotIn("runner-image-env", command)



if __name__ == "__main__":
    unittest.main()
