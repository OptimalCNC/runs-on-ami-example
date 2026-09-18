"""Exercise installer handoffs and failures at the Terraform/AWS boundaries."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import account
import installer


ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/runs-on-deployer"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/runs-on-workload-boundary"


class ExternalCommands:
    """Model externally produced state and outputs, without running cloud tools."""

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.fail = None
        self.outputs = {
            "installation": json.dumps({"account_id": ACCOUNT, "region": "us-east-1", "name": "runs-on"}),
            "publishing": json.dumps({"account_id": ACCOUNT, "region": "us-east-1", "name": "runs-on", "required_tags": {"runs-on-installation": "runs-on"}}),
        }
        self.bindings = {
            "deployment_role_arn": {"value": ROLE},
            "workload_boundary_arn": {"value": BOUNDARY},
        }
        self.account = ACCOUNT
        self.role_error = None
        self.oidc_error = None
        self.github_settings = {"use_default": True}
        self.github_repository_settings = {}

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
            repository = command[2].removeprefix("repos/").removesuffix("/actions/oidc/customization/sub")
            stdout = json.dumps(self.github_repository_settings.get(repository, self.github_settings))
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
            elif operation == "output":
                if command[-1] == "-json":
                    stdout = json.dumps({name: {"value": json.loads(value)} for name, value in self.outputs.items()})
                elif command[-1] in self.outputs:
                    stdout = self.outputs[command[-1]]
                else:
                    returncode, stderr = 1, "Missing Terraform output"
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def terraform_calls(self, component, operation):
        return [
            (command, options)
            for command, options in self.calls
            if command[0] == "terraform"
            and Path(command[1].removeprefix("-chdir=")).name == component
            and command[2] == operation
        ]


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_dir = self.root / "configuration"
        self.config_dir.mkdir()
        self.config = self.config_dir / "config.toml"
        self.config_value = {
            "aws": {"account_id": ACCOUNT, "region": "us-east-1"},
            "installation": {
                "name": "runs-on",
                "environment": "production",
                "github_organization": "example",
                "license_file": "license.txt",
                "notification_email_file": "email.txt",
            },
            "deployment": {"trusted_principal_arns": [f"arn:aws:iam::{ACCOUNT}:role/Admin"]},
            "publishing": {"principal_arns": [f"arn:aws:iam::{ACCOUNT}:role/Publisher"]},
        }
        self.write_config()
        (self.config_dir / "license.txt").write_text("private-license-value\n")
        (self.config_dir / "email.txt").write_text("private@example.invalid\n")
        self.external = ExternalCommands(self.root)
        self.output = io.StringIO()

    def write_config(self):
        def literal(value):
            if isinstance(value, dict):
                return "{" + ", ".join(f"{key} = {literal(item)}" for key, item in value.items()) + "}"
            if isinstance(value, list):
                return "[" + ", ".join(literal(item) for item in value) + "]"
            return json.dumps(value)

        self.config.write_text("\n".join(f"{key} = {literal(value)}" for key, value in self.config_value.items()) + "\n")

    def execute(self, *arguments):
        config = [] if arguments[0] == "export" else ["--config", str(self.config)]
        options = installer.parser().parse_args([*arguments, *config])
        with patch.object(installer.subprocess, "run", self.external), redirect_stdout(self.output):
            installer.execute(options, root=self.root)

    def prepare_account(self, *arguments):
        options = account.parser().parse_args([*arguments, "--config", str(self.config)])
        with patch("subprocess.run", self.external), redirect_stdout(self.output):
            account.execute(options)
