import unittest
from unittest.mock import patch

import yaml

from support import InstallerTestCase, installer


class ExportsTests(InstallerTestCase):
    def test_export_needs_state_but_not_configuration_or_secret_files(self):
        self.config.unlink()
        (self.config_dir / "license.txt").unlink()
        self.execute("export")
        self.assertTrue((self.root / ".local/contracts/publishing.yaml").exists())

    def test_export_rejects_boolean_schema_version(self):
        self.external.outputs["installation_yaml"] = "schema_version: true\nkind: runs-on-installation\n"
        with self.assertRaisesRegex(installer.InstallError, "schema_version: 1"):
            self.execute("export")
        self.assertFalse((self.root / ".local/contracts").exists())

    def test_export_reads_retained_publishing_state(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.external.outputs["publishing_yaml"] = f"schema_version: {version}\nkind: ami-publishing-target\n"
                self.execute("export")
                exported = self.root / ".local/contracts/publishing.yaml"
                self.assertEqual(yaml.safe_load(exported.read_text())["schema_version"], version)

    def test_export_rejects_unknown_or_boolean_publishing_version(self):
        for version in ("5", "true"):
            with self.subTest(version=version):
                self.external.outputs["publishing_yaml"] = f"schema_version: {version}\nkind: ami-publishing-target\n"
                with self.assertRaisesRegex(installer.InstallError, "schema_version: 1 or 2 or 3 or 4"):
                    self.execute("export")
                self.assertFalse((self.root / ".local/contracts").exists())

    def test_export_rejects_swapped_contract_kinds(self):
        self.external.outputs["publishing_yaml"] = self.external.outputs["installation_yaml"]
        with self.assertRaisesRegex(installer.InstallError, "kind: ami-publishing-target"):
            self.execute("export")
        self.assertFalse((self.root / ".local/contracts").exists())

    def test_export_preserves_previous_contracts_if_second_output_is_invalid(self):
        destination = self.root / ".local/contracts"
        destination.mkdir(parents=True)
        for name in ("installation", "publishing"):
            (destination / f"{name}.yaml").write_text(f"previous {name}\n")
        self.external.outputs["publishing_yaml"] = "wrong-shape"
        with self.assertRaisesRegex(installer.InstallError, "publishing_yaml"):
            self.execute("export")
        for name in ("installation", "publishing"):
            self.assertEqual((destination / f"{name}.yaml").read_text(), f"previous {name}\n")

    def test_atomic_write_keeps_previous_file_on_replace_failure(self):
        destination = self.root / "contract.yaml"
        destination.write_text("previous contract\n")
        with patch.object(installer.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                installer.atomic_write(destination, "new contract\n")
        self.assertEqual(destination.read_text(), "previous contract\n")
        self.assertFalse(list(self.root.glob(".contract.yaml.*")))


if __name__ == "__main__":
    unittest.main()
