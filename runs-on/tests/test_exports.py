import unittest

from support import InstallerTestCase, installer


class ExportsTests(InstallerTestCase):
    def test_export_needs_state_but_not_configuration_or_secret_files(self):
        self.config.unlink()
        (self.config_dir / "license.txt").unlink()
        self.execute("export")
        self.assertTrue((self.root / ".local/contracts/publishing.json").exists())
        self.assertEqual(
            [command[2:] for command, _ in self.external.terraform_calls("deployment", "output")],
            [["output", "-json", "installation"], ["output", "-json", "publishing"]],
        )

    def test_export_preserves_previous_contracts_if_second_output_is_missing(self):
        destination = self.root / ".local/contracts"
        destination.mkdir(parents=True)
        for name in ("installation", "publishing"):
            (destination / f"{name}.json").write_text(f"previous {name}\n")
        del self.external.outputs["publishing"]
        with self.assertRaisesRegex(installer.InstallError, "Missing Terraform output"):
            self.execute("export")
        for name in ("installation", "publishing"):
            self.assertEqual((destination / f"{name}.json").read_text(), f"previous {name}\n")


if __name__ == "__main__":
    unittest.main()
