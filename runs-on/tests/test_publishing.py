import json
import unittest

from support import InstallerTestCase, installer


class PublishingTests(InstallerTestCase):
    def test_destroy_does_not_need_access_to_a_retired_github_repository(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images"}, {"repository": "example/another-image"},
        ]
        self.write_config()
        self.execute("destroy", "--yes")
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "gh"])
        self.assertTrue(self.external.terraform_calls("deployment", "destroy"))

    def test_github_bootstrap_creates_missing_account_oidc_provider(self):
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images"}, {"repository": "example/another-image"},
        ]
        self.write_config()
        self.external.oidc_error = "An error occurred (NoSuchEntity) when calling GetOpenIDConnectProvider"
        self.execute("apply", "--yes")
        creations = [command for command, _ in self.external.calls if "create-open-id-connect-provider" in command]
        self.assertEqual(len(creations), 1)
        self.assertIn("https://token.actions.githubusercontent.com", creations[0])
        variables = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertNotIn("TF_VAR_existing_github_oidc_provider_arn", variables)

    def test_github_oidc_permission_failure_does_not_create_provider(self):
        self.config_value["publisher_github_repositories"] = [{"repository": "example/images"}]
        self.write_config()
        self.external.oidc_error = "An error occurred (AccessDenied) when calling GetOpenIDConnectProvider"
        with self.assertRaisesRegex(installer.InstallError, "Cannot inspect GitHub OIDC provider"):
            self.execute("bootstrap", "--yes")
        self.assertFalse([command for command, _ in self.external.calls if "create-open-id-connect-provider" in command])

    def test_github_bootstrap_reuses_existing_account_oidc_provider(self):
        self.config_value["publisher_github_repositories"] = [{"repository": "example/images"}]
        self.write_config()
        self.execute("bootstrap", "--yes")
        reads = [command for command, _ in self.external.calls if "get-open-id-connect-provider" in command]
        self.assertEqual(len(reads), 1)
        self.assertIn(f"arn:aws:iam::{self.config_value['account_id']}:oidc-provider/token.actions.githubusercontent.com", reads[0])
        self.assertFalse([command for command, _ in self.external.calls if "create-open-id-connect-provider" in command])
        self.assertTrue(self.external.terraform_calls("bootstrap", "apply"))

    def test_immutable_github_subject_passes_through_to_publisher_trust(self):
        self.external.bootstrap_exists()
        self.config_value["publisher_github_repositories"] = [{"repository": "example/images"}]
        self.write_config()
        self.external.github_settings = {"use_default": True, "use_immutable_subject": True, "sub_claim_prefix": "repo:example@123/images@456"}
        self.execute("apply", "--yes")
        variables = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertEqual(json.loads(variables["TF_VAR_publisher_github_repositories"]), [{
            "repository": "example/images", "environment": "image-publish", "subject_prefix": "repo:example@123/images@456",
        }])

    def test_explicit_github_subject_does_not_require_github_api_access(self):
        self.external.bootstrap_exists()
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images", "subject_prefix": "repo:example@123/images@456"},
            {"repository": "example/another-image", "environment": "release", "subject_prefix": "repo:example/another-image"},
        ]
        self.write_config()
        self.execute("plan")
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "gh"])

    def test_subject_for_another_repository_stops_before_bootstrap(self):
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images", "subject_prefix": "repo:other@123/images@456"},
        ]
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "must identify repository example/images"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)

    def test_unknown_custom_github_subject_stops_before_provisioning(self):
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images", "subject_prefix": "repo:example/images"},
            {"repository": "example/another-image"},
        ]
        self.write_config()
        self.external.github_settings = {"use_default": False, "include_claim_keys": ["job_workflow_ref"]}
        with self.assertRaisesRegex(installer.InstallError, "custom OIDC subject"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "init"))

    def test_multiple_repositories_discover_their_own_subjects_and_keep_environment_pairs(self):
        self.config_value["publisher_principal_arns"] = []
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images"},
            {"repository": "other/another-image", "environment": "release"},
            {"repository": "example/images", "environment": "staging"},
        ]
        self.external.github_repository_settings = {
            "example/images": {"use_default": True, "sub_claim_prefix": "repo:example@123/images@456"},
            "other/another-image": {"use_default": True, "sub_claim_prefix": "repo:other@789/another-image@987"},
        }
        self.write_config()
        self.execute("apply", "--yes")
        variables = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertEqual(json.loads(variables["TF_VAR_publisher_github_repositories"]), [
            {"repository": "example/images", "environment": "image-publish", "subject_prefix": "repo:example@123/images@456"},
            {"repository": "other/another-image", "environment": "release", "subject_prefix": "repo:other@789/another-image@987"},
            {"repository": "example/images", "environment": "staging", "subject_prefix": "repo:example@123/images@456"},
        ])
        github_calls = [command for command, _ in self.external.calls if command[0] == "gh"]
        self.assertEqual(github_calls, [
            ["gh", "api", "repos/example/images/actions/oidc/customization/sub"],
            ["gh", "api", "repos/other/another-image/actions/oidc/customization/sub"],
        ])

    def test_mixed_explicit_and_discovered_subjects_only_query_the_missing_prefix(self):
        self.config_value["publisher_github_repositories"] = [
            {"repository": "example/images", "subject_prefix": "repo:example@123/images@456"},
            {"repository": "other/another-image", "environment": "release"},
        ]
        self.write_config()
        self.execute("apply", "--yes")
        github_calls = [command for command, _ in self.external.calls if command[0] == "gh"]
        self.assertEqual(github_calls, [["gh", "api", "repos/other/another-image/actions/oidc/customization/sub"]])
        variables = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertEqual(json.loads(variables["TF_VAR_publisher_github_repositories"]), [
            {"repository": "example/images", "environment": "image-publish", "subject_prefix": "repo:example@123/images@456"},
            {"repository": "other/another-image", "environment": "release", "subject_prefix": "repo:other/another-image"},
        ])

    def test_invalid_github_publishers_stop_before_external_commands(self):
        for entries in (
            "example/images", None, ["example/images"], [{}],
            [{"repository": "example/*"}],
            [{"repository": "example/images", "environment": "*"}],
            [{"repository": "example/images", "subject_prefix": "repo:example/*"}],
            [{"repository": "example/images", "subject_prefix": "repo:other/images"}],
            [{"repository": "example/images", "environmnt": "release"}],
            [{"repository": "example/images"}, {"repository": "example/images", "environment": "image-publish"}],
        ):
            with self.subTest(entries=entries):
                self.config_value["publisher_github_repositories"] = entries
                self.write_config()
                with self.assertRaises(installer.InstallError):
                    self.execute("apply", "--yes")
                self.assertFalse(self.external.calls)

    def test_unknown_publishing_fields_are_rejected(self):
        for field in ("publisher_github_repository", "publisher_github_environment", "publisher_github_subject_prefix", "existing_github_oidc_provider_arn"):
            with self.subTest(field=field):
                self.config_value[field] = ""
                self.config_value["publisher_github_repositories"] = [{"repository": "example/images"}]
                self.write_config()
                with self.assertRaisesRegex(installer.InstallError, "Unknown configuration fields"):
                    self.execute("apply", "--yes")
                self.assertFalse(self.external.calls)
                del self.config_value[field]

    def test_configuration_rejects_no_publishing_identity(self):
        self.config_value["publisher_principal_arns"] = []
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "Configure at least one"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)


if __name__ == "__main__":
    unittest.main()
