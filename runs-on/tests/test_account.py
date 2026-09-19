import os
import unittest
from unittest.mock import patch

from support import InstallerTestCase, installer


class AccountTests(InstallerTestCase):
    def test_access_denied_is_not_treated_as_a_missing_service_role(self):
        self.external.role_error = "An error occurred (AccessDenied) when calling GetRole"
        with self.assertRaisesRegex(installer.InstallError, "Cannot inspect AWSServiceRoleForECS"):
            self.prepare_account()
        self.assertFalse([command for command, _ in self.external.calls if "create-service-linked-role" in command])
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))

    def test_missing_account_roles_are_created_only_for_required_services(self):
        self.external.role_error = "An error occurred (NoSuchEntity) when calling GetRole"
        self.prepare_account()
        creations = [command for command, _ in self.external.calls if "create-service-linked-role" in command]
        self.assertEqual({command[command.index("--aws-service-name") + 1] for command in creations}, {"ecs.amazonaws.com", "spot.amazonaws.com"})

    def test_wrong_source_account_stops_before_iam_mutation(self):
        self.external.account = "999999999999"
        with self.assertRaisesRegex(installer.InstallError, "expected 123456789012"):
            self.prepare_account()
        self.assertFalse([command for command, _ in self.external.calls if command[0:2] == ["aws", "iam"]])
        self.assertFalse(self.external.terraform_calls("bootstrap", "apply"))

    def test_account_preparation_creates_missing_account_oidc_provider(self):
        self.config_value["publishing"]["github_repositories"] = [
            {"repository": "example/images"}, {"repository": "example/another-image"},
        ]
        self.write_config()
        self.external.oidc_error = "An error occurred (NoSuchEntity) when calling GetOpenIDConnectProvider"
        self.prepare_account()
        creations = [command for command, _ in self.external.calls if "create-open-id-connect-provider" in command]
        self.assertEqual(len(creations), 1)
        self.assertIn("https://token.actions.githubusercontent.com", creations[0])

    def test_github_oidc_permission_failure_does_not_create_provider(self):
        self.config_value["publishing"]["github_repositories"] = [{"repository": "example/images"}]
        self.write_config()
        self.external.oidc_error = "An error occurred (AccessDenied) when calling GetOpenIDConnectProvider"
        with self.assertRaisesRegex(installer.InstallError, "Cannot inspect GitHub OIDC provider"):
            self.prepare_account()
        self.assertFalse([command for command, _ in self.external.calls if "create-open-id-connect-provider" in command])

    def test_account_preparation_reuses_existing_account_oidc_provider(self):
        self.config_value["publishing"]["github_repositories"] = [{"repository": "example/images"}]
        self.write_config()
        self.prepare_account()
        reads = [command for command, _ in self.external.calls if "get-open-id-connect-provider" in command]
        self.assertEqual(len(reads), 1)
        self.assertIn(f"arn:aws:iam::{self.config_value['aws']['account_id']}:oidc-provider/token.actions.githubusercontent.com", reads[0])
        self.assertFalse([command for command, _ in self.external.calls if "create-open-id-connect-provider" in command])

    def test_preparation_does_not_run_terraform_or_need_secret_files(self):
        (self.config_dir / "license.txt").unlink()
        (self.config_dir / "email.txt").unlink()
        self.prepare_account()
        self.assertTrue(self.external.calls)
        self.assertTrue(all(command[0] == "aws" for command, _ in self.external.calls))
        self.assertFalse((self.root / ".local").exists())

    def test_profile_selection_preserves_native_credential_resolution(self):
        environment = {
            "AWS_ACCESS_KEY_ID": "other-account", "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "token", "AWS_PROFILE": "other-profile", "AWS_DEFAULT_PROFILE": "other-profile",
            "AWS_ROLE_ARN": "arn:aws:iam::999999999999:role/OtherRole", "AWS_WEB_IDENTITY_TOKEN_FILE": "/other/token",
        }
        for profile in (None, "account-admin"):
            with self.subTest(profile=profile), patch.dict(os.environ, environment):
                self.external.calls.clear()
                self.prepare_account(*(["--profile", profile] if profile else []))
                for command, options in self.external.calls:
                    self.assertIsNone(options.get("env"))
                    if profile:
                        self.assertEqual(command[command.index("--profile") + 1], profile)
                    else:
                        self.assertNotIn("--profile", command)
                for name, value in environment.items():
                    self.assertEqual(os.environ[name], value)


if __name__ == "__main__":
    unittest.main()
