import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from support import ACCOUNT, ROLE, BOUNDARY, InstallerTestCase, installer


class InstallerTests(InstallerTestCase):
    def test_fresh_apply_passes_generated_role_to_deployment_and_exports_contracts(self):
        self.execute("apply", "--yes", "--profile", "source-admin")

        self.assertFalse([command for command, _ in self.external.calls if command[0] == "aws"])
        bootstrap_apply = self.external.terraform_calls("bootstrap", "apply")[0]
        deployment_apply = self.external.terraform_calls("deployment", "apply")[0]
        self.assertLess(self.external.calls.index(bootstrap_apply), self.external.calls.index(deployment_apply))
        environment = deployment_apply[1]["env"]
        self.assertEqual(environment["TF_VAR_deployment_role_arn"], ROLE)
        self.assertEqual(environment["TF_VAR_workload_boundary_arn"], BOUNDARY)
        self.assertEqual(environment["TF_VAR_aws_profile"], "source-admin")
        self.assertEqual(environment["TF_VAR_license_key"], "private-license-value")
        self.assertNotIn("TF_VAR_license_key", bootstrap_apply[1]["env"])
        self.assertEqual(json.loads(environment["TF_VAR_publisher_principal_arns"]), self.config_value["publishing"]["principal_arns"])

        for name in ("installation", "publishing"):
            path = self.root / ".local/contracts" / f"{name}.json"
            self.assertEqual(path.read_text(), self.external.outputs[name])
        for command, _ in self.external.calls:
            self.assertNotIn("private-license-value", " ".join(command))
        self.assertNotIn("private-license-value", self.output.getvalue())
        for path in (self.root / ".local").rglob("*"):
            if path.is_file():
                self.assertNotIn("private-license-value", path.read_text())

    def test_explicit_bootstrap_does_not_prepare_shared_account_resources(self):
        self.execute("bootstrap", "--yes")
        self.assertTrue(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "aws"])

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
        self.config_value["installation"]["name"] = "another-installation"
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "original state"):
            self.execute("bootstrap", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "aws"])

    def test_changed_region_does_not_reuse_existing_deployment_state(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.config_value["aws"]["region"] = "us-west-2"
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
            (destination / f"{name}.json").write_text("previous contract")
        self.execute("destroy", "--yes")
        self.assertTrue(self.external.terraform_calls("deployment", "destroy"))
        self.assertFalse(self.external.terraform_calls("bootstrap", "destroy"))
        self.assertTrue((self.root / ".local/state/bootstrap.tfstate").exists())
        self.assertFalse(list(destination.glob("*.json")))

    def test_profile_selection_preserves_native_credential_resolution(self):
        environment = {
            "AWS_ACCESS_KEY_ID": "other-account", "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "token", "AWS_PROFILE": "other-profile", "AWS_DEFAULT_PROFILE": "other-profile",
            "AWS_ROLE_ARN": "arn:aws:iam::999999999999:role/OtherRole", "AWS_WEB_IDENTITY_TOKEN_FILE": "/other/token",
        }
        for profile in (None, "chosen-profile"):
            with self.subTest(profile=profile), patch.dict(os.environ, {
                **environment, "TF_VAR_account_id": "999999999999",
                "TF_VAR_aws_profile": "inherited-profile", "TF_WORKSPACE": "other-workspace",
            }):
                self.external.calls.clear()
                self.execute("apply", "--yes", *(["--profile", profile] if profile else []))
                for _, options in self.external.calls:
                    for name, value in environment.items():
                        self.assertEqual(options["env"][name], value)
                    self.assertEqual(options["env"].get("TF_VAR_aws_profile"), profile)
                    self.assertEqual(options["env"]["TF_WORKSPACE"], "default")
                deployment_env = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
                self.assertEqual(deployment_env["TF_VAR_account_id"], ACCOUNT)

    def test_configuration_rejects_invalid_toml_before_external_commands(self):
        self.config.write_text('[aws\n')
        with self.assertRaisesRegex(installer.InstallError, "Cannot read configuration"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)

    def test_example_groups_settings_and_github_repository_tables(self):
        example = Path(installer.__file__).with_name("installation.example.toml").read_text()
        self.config.write_text(example + '''
[[publishing.github_repositories]]
repository = "example/images"

[[publishing.github_repositories]]
repository = "example/another-image"
environment = "release"
subject_prefix = "repo:example/another-image"
''')
        configuration = installer.Configuration.load(self.config)
        self.assertEqual(configuration.account_id, ACCOUNT)
        self.assertEqual(configuration.region, "us-east-1")
        self.assertEqual(configuration.name, "runs-on")
        self.assertEqual(configuration.vpc_cidr, "10.80.0.0/16")
        self.assertEqual(configuration.license_file, self.config_dir / "license.txt")
        self.assertEqual(configuration.notification_email_file, self.config_dir / "notification-email.txt")
        self.assertEqual(configuration.trusted_principal_arns, (f"arn:aws:iam::{ACCOUNT}:role/YourAdministratorRole",))
        self.assertEqual(configuration.publisher_principal_arns, (f"arn:aws:iam::{ACCOUNT}:role/YourImagePublisherLoginRole",))
        self.assertEqual(
            [(entry.repository, entry.environment, entry.subject_prefix) for entry in configuration.publisher_github_repositories],
            [("example/images", "image-publish", ""), ("example/another-image", "release", "repo:example/another-image")],
        )

    def test_configuration_rejects_unknown_fields_in_each_table(self):
        for name in ("aws", "installation", "deployment", "publishing"):
            with self.subTest(table=name):
                self.config_value[name]["typo"] = "ignored setting"
                self.write_config()
                with self.assertRaisesRegex(installer.InstallError, f"Unknown {name} fields: typo"):
                    self.execute("apply", "--yes")
                del self.config_value[name]["typo"]
        self.assertFalse(self.external.calls)

    def test_configuration_requires_tables(self):
        for name in ("aws", "installation", "deployment", "publishing"):
            original = self.config_value.pop(name)
            with self.subTest(table=name, value="missing"):
                self.write_config()
                with self.assertRaisesRegex(installer.InstallError, f"{name} must be a TOML table"):
                    self.execute("apply", "--yes")
            with self.subTest(table=name, value="string"):
                self.config_value[name] = "invalid table"
                self.write_config()
                with self.assertRaisesRegex(installer.InstallError, f"{name} must be a TOML table"):
                    self.execute("apply", "--yes")
            self.config_value[name] = original
        self.assertFalse(self.external.calls)

    def test_installation_name_limit_keeps_generated_iam_policy_within_aws_limit(self):
        self.config_value["installation"]["name"] = "a" * 24
        self.write_config()
        self.assertEqual(installer.Configuration.load(self.config).name, "a" * 24)
        self.config_value["installation"]["name"] = "a" * 25
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "3-24"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)


if __name__ == "__main__":
    unittest.main()
