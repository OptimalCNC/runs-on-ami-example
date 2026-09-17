import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "images" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class SourceInventory(unittest.TestCase):
    def test_plain_ubuntu_inventory_records_absent_runner_without_executing_it(self):
        spec = importlib.util.spec_from_file_location("source_inventory", ROOT / "images/build/common/inventory.py")
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


class StandaloneToolInstaller(unittest.TestCase):
    def archive(self, directory, name, binary, content):
        archive = directory / (name + ".zip")
        member = zipfile.ZipInfo(binary)
        member.external_attr = 0o755 << 16
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(member, content)
        return {"url": archive.as_uri(), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "version": "1.0", "binary": binary}

    def test_installs_offline_archives_with_directory_owned_plugin_path(self):
        installer = module("install-tools")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = directory / "tools with spaces"
            pins = {}
            for name in ("packer", "qemu_plugin"):
                binary = "packer-plugin-qemu_v1.0" if name == "qemu_plugin" else name
                script = "#!/bin/sh\nexit 0\n"
                if name == "packer":
                    script = '#!/bin/sh\nmkdir -p "$PACKER_PLUGIN_PATH"\nprintf "%s\\n" "$*" > "$PACKER_PLUGIN_PATH/installed"\n'
                pins[name] = self.archive(directory, name, binary, script)
            with patch.object(sys, "argv", ["install-tools.py", "--group", "build", "--directory", str(destination)]), \
                    patch.object(installer, "read_json", return_value={"tools": pins}), \
                    patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "PACKER_PLUGIN_PATH": str(directory / "unrelated")}, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                installer.main()
                self.assertEqual(os.environ["PACKER_PLUGIN_PATH"], str(directory / "unrelated"))
            self.assertTrue(os.access(destination / "bin/packer", os.X_OK))
            self.assertIn("github.com/hashicorp/qemu", (destination / "plugins" / "installed").read_text())
            self.assertFalse((directory / "unrelated").exists())
