import json
import os
import unittest
from unittest.mock import patch

import yaml

from support import ACCOUNT, KEY, ROLE, BOUNDARY, InstallerTestCase, installer


class InstallerTests(InstallerTestCase):
    def test_fresh_apply_passes_generated_role_to_deployment_and_exports_contracts(self):
        self.execute("apply", "--yes", "--profile", "source-admin")

        bootstrap_apply = self.external.terraform_calls("bootstrap", "apply")[0]
        deployment_apply = self.external.terraform_calls("deployment", "apply")[0]
        self.assertLess(self.external.calls.index(bootstrap_apply), self.external.calls.index(deployment_apply))
        environment = deployment_apply[1]["env"]
        self.assertEqual(environment["TF_VAR_deployment_role_arn"], ROLE)
        self.assertEqual(environment["TF_VAR_workload_boundary_arn"], BOUNDARY)
        self.assertEqual(environment["AWS_PROFILE"], "source-admin")
        self.assertEqual(environment["TF_VAR_license_key"], "private-license-value")
        self.assertNotIn("TF_VAR_license_key", bootstrap_apply[1]["env"])
        self.assertEqual(json.loads(environment["TF_VAR_publisher_principal_arns"]), self.config_value["publisher_principal_arns"])

        for name in ("installation", "publishing"):
            path = self.root / ".local/contracts" / f"{name}.yaml"
            self.assertEqual(yaml.safe_load(path.read_text())["schema_version"], 4 if name == "publishing" else 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for command, _ in self.external.calls:
            self.assertNotIn("private-license-value", " ".join(command))
        self.assertNotIn("private-license-value", self.output.getvalue())
        for path in (self.root / ".local").rglob("*"):
            if path.is_file():
                self.assertNotIn("private-license-value", path.read_text())

    def test_bootstrap_failure_stops_before_deployment(self):
        self.external.fail = ("bootstrap", "apply")
        with self.assertRaisesRegex(installer.InstallError, "apply failed for bootstrap"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("deployment", "init"))
        self.assertFalse((self.root / ".local/contracts").exists())

    def test_normal_apply_reuses_bootstrap_without_account_level_iam_calls(self):
        self.external.bootstrap_exists()
        self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "aws"])
        self.assertTrue(self.external.terraform_calls("deployment", "apply"))

    def test_fresh_plan_does_not_mutate_aws_or_initialize_deployment(self):
        self.execute("plan")
        self.assertTrue(self.external.terraform_calls("bootstrap", "plan"))
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse(self.external.terraform_calls("deployment", "init"))
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "aws"])
        self.assertIn("covers bootstrap only", self.output.getvalue())

    def test_explicit_bootstrap_plan_works_before_secret_files_exist(self):
        (self.config_dir / "license.txt").unlink()
        (self.config_dir / "email.txt").unlink()
        self.execute("bootstrap", "--plan")
        self.assertTrue(self.external.terraform_calls("bootstrap", "plan"))
        self.assertFalse(self.external.terraform_calls("deployment", "init"))

    def test_missing_secret_stops_apply_before_any_external_command(self):
        (self.config_dir / "license.txt").unlink()
        with self.assertRaisesRegex(installer.InstallError, "Cannot read license_key"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)

    def test_deployment_only_requires_existing_bootstrap(self):
        with self.assertRaisesRegex(installer.InstallError, "Bootstrap state is missing"):
            self.execute("apply", "--deployment-only", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse(self.external.terraform_calls("deployment", "init"))

    def test_wrong_account_bootstrap_outputs_never_reach_deployment(self):
        self.external.bootstrap_exists()
        self.external.bindings["deployment_role_arn"]["value"] = "arn:aws:iam::999999999999:role/deployer"
        with self.assertRaisesRegex(installer.InstallError, "no valid deployment_role_arn"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("deployment", "init"))

    def test_changed_installation_name_never_replaces_existing_bootstrap_roles(self):
        self.external.bootstrap_exists()
        self.config_value["name"] = "another-installation"
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "original state"):
            self.execute("bootstrap", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "aws"])

    def test_changed_region_does_not_reuse_existing_deployment_state(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.config_value["region"] = "us-west-2"
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "region differs"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse(self.external.terraform_calls("deployment", "apply"))

    def test_destroy_retains_bootstrap_state_and_removes_stale_contracts(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        destination = self.root / ".local/contracts"
        destination.mkdir(parents=True)
        for name in ("installation", "publishing"):
            (destination / f"{name}.yaml").write_text("previous contract")
        self.execute("destroy", "--yes")
        self.assertTrue(self.external.terraform_calls("deployment", "destroy"))
        self.assertFalse(self.external.terraform_calls("bootstrap", "destroy"))
        self.assertTrue((self.root / ".local/state/bootstrap.tfstate").exists())
        self.assertFalse(list(destination.glob("*.yaml")))

    def test_partial_deployment_still_blocks_bootstrap_boundary_update(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.external.state_values["outputs"] = {}
        self.external.snapshots = [{"SnapshotId": "snap-retained", "KmsKeyId": KEY}]
        with self.assertRaisesRegex(installer.InstallError, "snap-retained"):
            self.execute("bootstrap", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))

    def test_access_denied_is_not_treated_as_a_missing_service_role(self):
        self.external.role_error = "An error occurred (AccessDenied) when calling GetRole"
        with self.assertRaisesRegex(installer.InstallError, "Cannot inspect AWSServiceRoleForECS"):
            self.execute("bootstrap", "--yes")
        self.assertFalse([command for command, _ in self.external.calls if "create-service-linked-role" in command])
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))

    def test_missing_account_roles_are_created_only_for_required_services(self):
        self.external.role_error = "An error occurred (NoSuchEntity) when calling GetRole"
        self.execute("bootstrap", "--yes")
        creations = [command for command, _ in self.external.calls if "create-service-linked-role" in command]
        self.assertEqual({command[command.index("--aws-service-name") + 1] for command in creations}, {"ecs.amazonaws.com", "spot.amazonaws.com"})

    def test_wrong_source_account_stops_before_iam_mutation(self):
        self.external.account = "999999999999"
        with self.assertRaisesRegex(installer.InstallError, "expected 123456789012"):
            self.execute("bootstrap", "--yes")
        self.assertFalse([command for command, _ in self.external.calls if command[0:2] == ["aws", "iam"]])
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))

    def test_explicit_profile_does_not_use_inherited_static_credentials(self):
        with patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "other-account", "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_ROLE_ARN": "arn:aws:iam::999999999999:role/OtherRole", "AWS_WEB_IDENTITY_TOKEN_FILE": "/other/token",
            "AWS_DEFAULT_PROFILE": "other-profile", "TF_VAR_account_id": "999999999999", "TF_WORKSPACE": "other-workspace",
        }):
            self.execute("apply", "--yes", "--profile", "chosen-profile")
        for _, options in self.external.calls:
            self.assertNotIn("AWS_ACCESS_KEY_ID", options["env"])
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", options["env"])
            self.assertNotIn("AWS_ROLE_ARN", options["env"])
            self.assertNotIn("AWS_WEB_IDENTITY_TOKEN_FILE", options["env"])
            self.assertEqual(options["env"]["AWS_PROFILE"], "chosen-profile")
            self.assertEqual(options["env"]["AWS_DEFAULT_PROFILE"], "chosen-profile")
            self.assertEqual(options["env"]["TF_WORKSPACE"], "default")
        deployment_env = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertEqual(deployment_env["TF_VAR_account_id"], ACCOUNT)

    def test_configuration_rejects_executable_yaml_before_external_commands(self):
        self.config.write_text("!!python/object/apply:os.system ['false']")
        with self.assertRaisesRegex(installer.InstallError, "Cannot read configuration"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)

    def test_installation_name_limit_keeps_generated_iam_policy_within_aws_limit(self):
        self.config_value["name"] = "a" * 24
        self.write_config()
        self.assertEqual(installer.Configuration.load(self.config).name, "a" * 24)
        self.config_value["name"] = "a" * 25
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "3-24"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)


if __name__ == "__main__":
    unittest.main()
