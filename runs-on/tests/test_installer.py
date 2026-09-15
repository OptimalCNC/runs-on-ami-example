"""Exercise installer handoffs and failures at the Terraform/AWS boundaries."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer
import yaml


ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/runs-on-deployer"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/runs-on-workload-boundary"
KEY = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789012"


class ExternalCommands:
    """Model externally produced state and outputs, without running cloud tools."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.fail = None
        self.outputs = {
            "installation_yaml": "schema_version: 1\nkind: runs-on-installation\n",
            "publishing_yaml": f"schema_version: 1\nkind: ami-publishing-target\ndestination:\n  kms_key_arn: {KEY}\n",
        }
        self.bindings = {
            "deployment_role_arn": {"value": ROLE},
            "workload_boundary_arn": {"value": BOUNDARY},
        }
        self.account = ACCOUNT
        self.role_error = None
        self.oidc_error = None
        self.snapshots = []
        self.volumes = []
        self.github_settings = {"use_default": True}
        self.state_values = {
            "outputs": {
                "installation": {"value": {"account_id": ACCOUNT, "region": "us-east-1", "name": "runs-on"}},
            },
            "root_module": {"resources": [{"address": "aws_kms_key.images", "values": {"arn": KEY}}]},
        }

    def bootstrap_exists(self):
        path = self.root / ".local/state/bootstrap.tfstate"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("external Terraform state")

    def deployment_exists(self):
        path = self.root / ".local/state/deployment.tfstate"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("external Terraform state")

    def __call__(self, command, **options):
        self.calls.append((command, options))
        stdout = ""
        stderr = ""
        returncode = 0
        if command[0] == "gh":
            stdout = json.dumps(self.github_settings)
        elif command[0] == "aws":
            if command[1:3] == ["sts", "get-caller-identity"]:
                stdout = json.dumps({"Account": self.account})
            elif command[1:3] == ["iam", "get-role"] and self.role_error:
                returncode, stderr = 254, self.role_error
            elif command[1:3] == ["iam", "get-open-id-connect-provider"]:
                if self.oidc_error:
                    returncode, stderr = 254, self.oidc_error
                else:
                    stdout = json.dumps({"ClientIDList": ["sts.amazonaws.com"]})
            elif command[1:3] == ["sts", "assume-role"]:
                stdout = json.dumps({"Credentials": {"AccessKeyId": "temporary-key", "SecretAccessKey": "temporary-secret", "SessionToken": "temporary-token"}})
            elif command[1:3] == ["ec2", "describe-snapshots"]:
                stdout = json.dumps({"Snapshots": self.snapshots})
            elif command[1:3] == ["ec2", "describe-volumes"]:
                stdout = json.dumps({"Volumes": self.volumes})
            else:
                stdout = "{}"
        else:
            component = Path(command[1].removeprefix("-chdir=")).name
            operation = command[2]
            if self.fail == (component, operation):
                returncode, stderr = 1, "external operation failed"
            elif component == "bootstrap" and operation == "apply":
                self.bootstrap_exists()
            elif component == "bootstrap" and operation == "output":
                stdout = json.dumps(self.bindings)
            elif operation == "show":
                stdout = json.dumps({"values": self.state_values})
            elif operation == "output":
                stdout = self.outputs[command[-1]]
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def terraform_calls(self, component, operation):
        return [
            (command, options)
            for command, options in self.calls
            if command[0] == "terraform"
            and Path(command[1].removeprefix("-chdir=")).name == component
            and command[2] == operation
        ]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_dir = self.root / "configuration"
        self.config_dir.mkdir()
        self.config = self.config_dir / "config.yaml"
        self.config_value = {
            "schema_version": 1,
            "account_id": ACCOUNT,
            "region": "us-east-1",
            "name": "runs-on",
            "environment": "production",
            "github_organization": "example",
            "license_file": "license.txt",
            "notification_email_file": "email.txt",
            "trusted_principal_arns": [f"arn:aws:iam::{ACCOUNT}:role/Admin"],
            "publisher_principal_arns": [f"arn:aws:iam::{ACCOUNT}:role/Publisher"],
        }
        self.write_config()
        (self.config_dir / "license.txt").write_text("private-license-value\n")
        (self.config_dir / "email.txt").write_text("private@example.invalid\n")
        self.external = ExternalCommands(self.root)
        self.output = io.StringIO()

    def write_config(self):
        self.config.write_text(yaml.safe_dump(self.config_value))

    def execute(self, *arguments):
        options = installer.parser().parse_args([*arguments, "--config", str(self.config)])
        with patch.object(installer.subprocess, "run", self.external), redirect_stdout(self.output):
            installer.execute(options, root=self.root)

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
            self.assertEqual(yaml.safe_load(path.read_text())["schema_version"], 1)
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

    def test_destroy_does_not_need_access_to_a_retired_github_repository(self):
        self.external.bootstrap_exists()
        self.external.deployment_exists()
        self.config_value["publisher_github_repository"] = "example/images"
        self.write_config()
        self.execute("destroy", "--yes")
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "gh"])
        self.assertTrue(self.external.terraform_calls("deployment", "destroy"))

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

    def test_github_bootstrap_creates_missing_account_oidc_provider(self):
        self.config_value["publisher_github_repository"] = "example/images"
        self.write_config()
        self.external.oidc_error = "An error occurred (NoSuchEntity) when calling GetOpenIDConnectProvider"
        self.execute("apply", "--yes")
        creations = [command for command, _ in self.external.calls if "create-open-id-connect-provider" in command]
        self.assertEqual(len(creations), 1)
        self.assertIn("https://token.actions.githubusercontent.com", creations[0])
        variables = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertEqual(variables["TF_VAR_existing_github_oidc_provider_arn"], f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com")

    def test_github_oidc_permission_failure_does_not_create_provider(self):
        self.config_value["publisher_github_repository"] = "example/images"
        self.write_config()
        self.external.oidc_error = "An error occurred (AccessDenied) when calling GetOpenIDConnectProvider"
        with self.assertRaisesRegex(installer.InstallError, "Cannot inspect GitHub OIDC provider"):
            self.execute("bootstrap", "--yes")
        self.assertFalse([command for command, _ in self.external.calls if "create-open-id-connect-provider" in command])

    def test_immutable_github_subject_passes_through_to_publisher_trust(self):
        self.external.bootstrap_exists()
        self.config_value["publisher_github_repository"] = "example/images"
        self.write_config()
        self.external.github_settings = {"use_default": True, "use_immutable_subject": True, "sub_claim_prefix": "repo:example@123/images@456"}
        self.execute("apply", "--yes")
        variables = self.external.terraform_calls("deployment", "apply")[0][1]["env"]
        self.assertEqual(variables["TF_VAR_publisher_github_subject_prefix"], "repo:example@123/images@456")

    def test_explicit_github_subject_does_not_require_github_api_access(self):
        self.external.bootstrap_exists()
        self.config_value.update(publisher_github_repository="example/images", publisher_github_subject_prefix="repo:example@123/images@456")
        self.write_config()
        self.execute("plan")
        self.assertFalse([command for command, _ in self.external.calls if command[0] == "gh"])

    def test_subject_for_another_repository_stops_before_bootstrap(self):
        self.config_value.update(publisher_github_repository="example/images", publisher_github_subject_prefix="repo:other@123/images@456")
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "must identify publisher_github_repository"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.calls)

    def test_unknown_custom_github_subject_stops_before_provisioning(self):
        self.config_value["publisher_github_repository"] = "example/images"
        self.write_config()
        self.external.github_settings = {"use_default": False, "include_claim_keys": ["job_workflow_ref"]}
        with self.assertRaisesRegex(installer.InstallError, "custom OIDC subject"):
            self.execute("apply", "--yes")
        self.assertFalse(self.external.terraform_calls("bootstrap", "init"))

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

    def test_configuration_rejects_no_publishing_identity(self):
        self.config_value["publisher_principal_arns"] = []
        self.write_config()
        with self.assertRaisesRegex(installer.InstallError, "Configure at least one"):
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
