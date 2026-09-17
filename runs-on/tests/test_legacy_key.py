import unittest

from support import KEY, InstallerTestCase, installer


class LegacyKeyTests(InstallerTestCase):
    def test_destroy_refuses_key_deletion_when_published_snapshot_remains(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.snapshots = [{"SnapshotId": "snap-retained", "KmsKeyId": KEY}]
        with self.assertRaisesRegex(installer.InstallError, "snap-retained"):
            self.execute("destroy", "--yes", "--profile", "source-admin")
        self.assertFalse(self.external.terraform_calls("deployment", "destroy"))
        inventories = [(command, options) for command, options in self.external.calls if command[0:2] == ["aws", "ec2"]]
        for _, options in inventories:
            self.assertEqual(options["env"]["AWS_ACCESS_KEY_ID"], "temporary-key")
            self.assertNotIn("AWS_PROFILE", options["env"])

    def test_upgrade_refuses_to_retire_a_key_with_retained_snapshots_or_volumes(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        for collection, retained in (
            ("snapshots", {"SnapshotId": "snap-retained", "KmsKeyId": KEY}),
            ("volumes", {"VolumeId": "vol-running", "KmsKeyId": KEY}),
        ):
            for arguments in (("apply", "--deployment-only", "--yes"), ("bootstrap", "--yes"), ("apply", "--bootstrap-only", "--yes")):
                with self.subTest(collection=collection, arguments=arguments):
                    self.external.snapshots, self.external.volumes = [], []
                    setattr(self.external, collection, [retained])
                    self.external.calls.clear()
                    with self.assertRaisesRegex(installer.InstallError, "legacy encryption key is still used"):
                        self.execute(*arguments)
                    self.assertFalse(self.external.terraform_calls("deployment", "apply"))
                    self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
                    self.assertFalse([cmd for cmd, _ in self.external.calls if cmd[:2] == ["aws", "iam"]])

    def test_upgrade_checks_the_old_key_before_applying_and_exporting(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.snapshots = [{"SnapshotId": "snap-unrelated", "KmsKeyId": KEY + "other"}]
        self.external.volumes = [{"VolumeId": "vol-unrelated", "KmsKeyId": KEY + "other"}]
        self.execute("apply", "--deployment-only", "--yes", "--profile", "source-admin")
        apply = self.external.terraform_calls("deployment", "apply")[0]
        reads = [entry for entry in self.external.calls if entry[0][:2] == ["aws", "ec2"]]
        self.assertEqual(len(reads), 2)
        for read in reads:
            self.assertLess(self.external.calls.index(read), self.external.calls.index(apply))
            self.assertEqual(read[1]["env"]["AWS_ACCESS_KEY_ID"], "temporary-key")
            self.assertNotIn("AWS_PROFILE", read[1]["env"])
        self.assertTrue((self.root / ".local/contracts/publishing.yaml").is_file())

    def test_upgrade_after_key_retirement_needs_no_aws_inventory(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.state_values["root_module"] = {"resources": []}
        self.execute("apply", "--deployment-only", "--yes")
        self.assertFalse([cmd for cmd, _ in self.external.calls if cmd[0] == "aws"])
        self.assertTrue(self.external.terraform_calls("deployment", "apply"))

    def test_key_inventory_access_denied_blocks_upgrade(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.inventory_error = "AccessDenied"
        with self.assertRaisesRegex(installer.InstallError, "Cannot check retained images"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("deployment", "apply"))

    def test_legacy_key_with_missing_bootstrap_state_blocks_automatic_bootstrap(self):
        self.external.deployment_exists()
        with self.assertRaisesRegex(installer.InstallError, "Restore the bootstrap state"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse([cmd for cmd, _ in self.external.calls if cmd[0] == "aws"])

    def test_destroy_refuses_key_deletion_when_volume_remains(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.volumes = [{"VolumeId": "vol-retained", "KmsKeyId": KEY}]
        with self.assertRaisesRegex(installer.InstallError, "vol-retained"):
            self.execute("destroy", "--yes")
        self.assertFalse(self.external.terraform_calls("deployment", "destroy"))

    def test_destroy_ignores_disks_encrypted_by_other_keys(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.snapshots = [{"SnapshotId": "snap-unrelated", "KmsKeyId": KEY + "other"}]
        self.external.volumes = [{"VolumeId": "vol-unrelated", "KmsKeyId": KEY + "other"}]
        self.execute("destroy", "--yes")
        self.assertTrue(self.external.terraform_calls("deployment", "destroy"))

    def test_partial_deployment_without_outputs_still_protects_retained_images(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.state_values["outputs"] = {}
        self.external.outputs = {}
        self.external.snapshots = [{"SnapshotId": "snap-retained", "KmsKeyId": KEY}]
        with self.assertRaisesRegex(installer.InstallError, "snap-retained"):
            self.execute("destroy", "--yes")
        self.assertFalse(self.external.terraform_calls("deployment", "destroy"))

    def test_destroy_before_image_key_creation_does_not_require_complete_outputs(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.state_values = {"root_module": {"resources": []}}
        self.external.outputs = {}
        self.execute("destroy", "--yes")
        self.assertTrue(self.external.terraform_calls("deployment", "destroy"))
        self.assertFalse([command for command, _ in self.external.calls if "assume-role" in command])


if __name__ == "__main__":
    unittest.main()
