import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from support import ROOT, module


def image_environment_module():
    loader = importlib.machinery.SourceFileLoader("runner_image_env", str(ROOT / "images/common/runner-image-env"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


class SourceInventory(unittest.TestCase):
    def test_plain_ubuntu_inventory_records_absent_runner_without_executing_it(self):
        spec = importlib.util.spec_from_file_location("source_inventory", ROOT / "images/common/inventory.py")
        command = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(command)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "etc").mkdir()
            (directory / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
            packages = "python3\t3.12.3\tamd64\tinstalled\n"
            with patch.object(command, "Path", side_effect=lambda path: directory / str(path).lstrip("/")), \
                    patch.object(command, "packages", return_value=packages), \
                    patch.object(command.glob, "glob", return_value=[]), \
                    patch.object(command.subprocess, "check_output") as execute:
                captured = command.inventory()
            execute.assert_not_called()
            self.assertEqual(captured["os_version"], "24.04")
            self.assertIsNone(captured["runner_version"])
            self.assertIsNone(captured["runner_listener_sha256"])
            self.assertEqual(captured["bootstrap_files"], {})
            self.assertEqual(captured["packages_sha256"], hashlib.sha256(packages.encode()).hexdigest())


class StandaloneSmoke(unittest.TestCase):
    def test_identity_uses_explicit_inputs_without_runner_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            reporter = directory / "ami-example-guest-report"
            reporter.write_text(
                "#!/usr/bin/python3\n"
                "import json, pathlib, sys\n"
                "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
                "pathlib.Path(args.pop('--output')).write_text(json.dumps(args))\n"
            )
            reporter.chmod(0o755)
            output = directory / "reports with spaces"
            completed = subprocess.run([
                "/bin/bash", str(ROOT / "images/xenomai-cobalt/smoke.sh"), "identity",
                "--release", "6.12.90-cip24-xenomai-cobalt", "--recipe", "a" * 64,
                "--build-id", "123-1-one", "--stage", "a", "--output", str(output),
            ], env={"PATH": f"{directory}:/usr/bin:/bin"}, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads((output / "identity.json").read_text()), {
                "--release": "6.12.90-cip24-xenomai-cobalt", "--recipe": "a" * 64,
                "--build-id": "123-1-one", "--stage": "a",
            })

    def test_test_command_requires_source_before_creating_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reports"
            completed = subprocess.run([
                "/bin/bash", str(ROOT / "images/xenomai-cobalt/smoke.sh"), "test",
                "--release", "6.12.90", "--recipe", "a" * 64, "--build-id", "123-1-one",
                "--stage", "a", "--output", str(output),
            ], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("test requires --source", completed.stderr)
            self.assertFalse(output.exists())


class ImageEnvironment(unittest.TestCase):
    def setUp(self):
        self.command = image_environment_module()

    def manifest(self, directory):
        path = directory / "manifest.json"
        path.write_text(json.dumps({"recipe_id": "a" * 64, "kernel_release": "6.12.90-cip24-xenomai-cobalt",
                                    "xenomai": {"core": "cobalt", "prefix": "/usr/xenomai"}}))
        return path

    def render(self, manifest, format):
        output = io.StringIO()
        with patch.object(sys, "argv", ["runner-image-env", "--manifest", str(manifest), "--format", format]), \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)), \
                patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True), contextlib.redirect_stdout(output):
            self.command.main()
        return output.getvalue()

    def test_formats_expose_the_same_manifest_values_for_local_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            structured = json.loads(self.render(manifest, "json"))
            values = structured["env"]
            self.assertEqual(values, {"AMI_EXAMPLE_RECIPE_ID": "a" * 64,
                                      "AMI_EXAMPLE_KERNEL_RELEASE": "6.12.90-cip24-xenomai-cobalt",
                                      "XENOMAI_ROOT": "/usr/xenomai"})
            self.assertEqual(structured["path"], ["/usr/xenomai/bin"])
            self.assertEqual(dict(line.split("=", 1) for line in self.render(manifest, "env").splitlines()), values)
            activated = subprocess.check_output(
                ["/bin/sh", "-c", self.render(manifest, "shell") + '\nexec /usr/bin/python3 -c "import json, os; print(json.dumps(dict(os.environ)))"'],
                env={"PATH": "/usr/bin:/bin"}, text=True)
            environment = json.loads(activated)
            self.assertEqual({key: environment[key] for key in values}, values)
            self.assertEqual(environment["PATH"], "/usr/xenomai/bin:/usr/bin:/bin")

    def test_manifest_ownership_and_permissions_remain_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.manifest(Path(temporary))
            for owner, mode in ((1000, 0o100644), (0, 0o100664), (0, 0o100646)):
                with self.subTest(owner=owner, mode=mode), \
                        patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=owner, st_mode=mode)), \
                        self.assertRaisesRegex(ValueError, "root-owned"):
                    self.command.environment(manifest)


class StandaloneToolInstaller(unittest.TestCase):
    def archive(self, directory, name, binary, content):
        archive = directory / (name + ".zip")
        member = zipfile.ZipInfo(binary)
        member.external_attr = 0o755 << 16
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(member, content)
        return {"url": archive.as_uri(), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "version": "1.0", "binary": binary}

    def test_installs_offline_archives_and_reports_directory_owned_plugin_path(self):
        installer = module("install-tools")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = directory / "tools with spaces"
            pins = {}
            for name in ("packer", "amazon_plugin", "terraform", "actionlint"):
                binary = "packer-plugin-amazon_v1.0" if name == "amazon_plugin" else name
                script = "#!/bin/sh\nexit 0\n"
                if name == "packer":
                    script = '#!/bin/sh\nmkdir -p "$PACKER_PLUGIN_PATH"\nprintf "%s\\n" "$*" > "$PACKER_PLUGIN_PATH/installed"\n'
                pins[name] = self.archive(directory, name, binary, script)
            report = directory / "output" / "tools.json"
            with patch.object(sys, "argv", ["install-tools.py", "--group", "validation", "--directory", str(destination),
                                            "--output", str(report)]), \
                    patch.object(installer, "read_json", return_value={"tools": pins}), \
                    patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "PACKER_PLUGIN_PATH": str(directory / "unrelated")}, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                installer.main()
                self.assertEqual(os.environ["PACKER_PLUGIN_PATH"], str(directory / "unrelated"))
            self.assertEqual(json.loads(report.read_text()), {"bin_directory": str(destination / "bin"),
                                                             "packer_plugin_path": str(destination / "plugins")})
            for name in ("packer", "terraform", "actionlint"):
                self.assertTrue(os.access(destination / "bin" / name, os.X_OK))
            self.assertIn("github.com/hashicorp/amazon", (destination / "plugins" / "installed").read_text())
            self.assertFalse((directory / "unrelated").exists())
