import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


IMAGES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(IMAGES))
import validate
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
        self.guest = {**manifest, **manifest["runner_inventory"], "boot_mode": "uefi", "environment_passed": True}
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
                           ("environment_passed", False)):
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
import validate

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


class MetadataBoundary(unittest.TestCase):
    def test_report_platform_controls_metadata_without_changing_common_observations(self):
        import gzip
        import hashlib
        import os
        spec = importlib.util.spec_from_file_location("guest_report", IMAGES / "common/guest-report.py")
        reporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reporter)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = b"CONFIG_IKCONFIG_PROC=y\n"
            manifest = {"kernel_release": "kernel", "recipe_id": "recipe", "config_sha256": hashlib.sha256(config).hexdigest()}
            for name, data in (("etc/ami-example.json", json.dumps(manifest).encode()),
                               ("proc/config.gz", gzip.compress(config)), ("proc/cmdline", b"console=ttyS0"),
                               ("etc/machine-id", b"fresh-machine")):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o644)
            real_stat = Path.stat

            def root_owned(path, **kwargs):
                parts = list(real_stat(path, **kwargs))
                parts[4] = 0
                return os.stat_result(parts)

            def command(arguments, **_):
                if arguments == ["uname", "-r"]:
                    return "kernel\n"
                if arguments[0] == "dpkg-query":
                    return "python3\t3.12\tamd64\tinstalled\n"
                return "2.337.0\n"

            with patch.object(reporter, "Path", side_effect=lambda value: directory / str(value).lstrip("/")), \
                    patch.object(Path, "stat", root_owned), \
                    patch.object(reporter, "cobalt_identity", return_value={"core": "cobalt"}), \
                    patch.object(reporter, "sha", return_value="a" * 64), \
                    patch.object(reporter.glob, "glob", return_value=[]), \
                    patch.object(reporter.subprocess, "check_output", side_effect=command), \
                    patch.object(reporter, "metadata", return_value={"instanceId": "i-test"}) as metadata:
                vm = reporter.report("kernel", "recipe", "vm")
                metadata.assert_not_called()
                ec2 = reporter.report("kernel", "recipe")
                metadata.assert_called_once_with()
            self.assertNotIn("identity", vm)
            self.assertEqual(ec2.pop("identity"), {"instanceId": "i-test"})
            self.assertEqual(vm, ec2)


if __name__ == "__main__":
    unittest.main()
