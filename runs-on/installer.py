"""Connect the RunsOn bootstrap and deployment Terraform roots."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

from aws_cli import aws_environment
from configuration import Configuration, InstallError


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class BootstrapBindings:
    deployment_role_arn: str
    workload_boundary_arn: str

    @classmethod
    def parse(cls, value: object, configuration: Configuration) -> BootstrapBindings:
        if not isinstance(value, dict):
            raise InstallError("Bootstrap Terraform outputs must be a JSON object")
        fields = {}
        for name, resource in (("deployment_role_arn", f"role/{configuration.name}-deployer"), ("workload_boundary_arn", f"policy/{configuration.name}-workload-boundary")):
            output = value.get(name)
            arn = output.get("value") if isinstance(output, dict) else None
            if arn != f"arn:aws:iam::{configuration.account_id}:{resource}":
                raise InstallError(f"Bootstrap state has no valid {name} for account {configuration.account_id} and name {configuration.name}; keep an installation's account and name with its original state")
            fields[name] = arn
        return cls(**fields)


class Terraform:
    def __init__(self, root: Path, component: str, profile: str | None):
        self.directory = root / component
        self.state_path = root / ".local" / "state" / f"{component}.tfstate"
        self.data_path = root / ".local" / "terraform" / component
        self.environment = aws_environment(profile)
        # A caller's TF_VAR values must not silently override this configuration.
        self.environment = {key: value for key, value in self.environment.items() if not key.startswith("TF_VAR_")}
        self.environment.update(TF_DATA_DIR=str(self.data_path), TF_IN_AUTOMATION="true", TF_WORKSPACE="default")
        self.variables: dict[str, object] = {}
        self.initialized = False

    def command(self, *arguments: str, capture: bool = False) -> str:
        environment = dict(self.environment)
        for key, value in self.variables.items():
            environment[f"TF_VAR_{key}"] = value if isinstance(value, str) else json.dumps(value)
        try:
            completed = subprocess.run(
                ["terraform", f"-chdir={self.directory}", *arguments],
                env=environment, text=True, check=False,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except FileNotFoundError as exc:
            raise InstallError("Terraform is required; install it and make it available on PATH") from exc
        if completed.returncode:
            # Captured commands only read output; their errors contain no variable values.
            detail = f"\n{completed.stderr.strip()}" if capture and completed.stderr else ""
            raise InstallError(f"Terraform {arguments[0]} failed for {self.directory.name} (exit {completed.returncode}){detail}")
        return completed.stdout if capture else ""

    def initialize(self) -> None:
        if self.initialized:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.command("init", "-input=false", f"-backend-config=path={self.state_path}")
        self.initialized = True

    def bindings(self, configuration: Configuration) -> BootstrapBindings | None:
        outputs = self.outputs()
        if not outputs:
            return None
        return BootstrapBindings.parse(outputs, configuration)

    def outputs(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {}
        self.initialize()
        return json.loads(self.command("output", "-json", capture=True))


def require_matching_installation(configuration: Configuration, outputs: dict[str, object]) -> None:
    for name in ("installation", "publishing"):
        output = outputs.get(name)
        if not isinstance(output, dict) or not isinstance(output.get("value"), dict):
            continue
        contract = output["value"]
        for field in ("account_id", "region", "name"):
            if contract.get(field) != getattr(configuration, field):
                raise InstallError(f"Configuration {field} differs from the existing installation state; restore the original {field} or use a separate checkout and state for another installation")


def export_contracts(deployment: Terraform, destination: Path) -> None:
    contracts = {
        name: deployment.command("output", "-json", name, capture=True)
        for name in ("installation", "publishing")
    }
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, contents in contracts.items():
        path = destination / f"{name}.json"
        path.write_text(contents)
        print(f"Exported {path}")


def execute(arguments: argparse.Namespace, *, root: Path = ROOT) -> None:
    deployment = Terraform(root, "deployment", arguments.profile)
    if arguments.command == "export":
        deployment.initialize()
        export_contracts(deployment, root / ".local" / "contracts")
        return

    configuration = Configuration.load(arguments.config)
    bootstrap = Terraform(root, "bootstrap", arguments.profile)
    bootstrap.variables = configuration.bootstrap_variables()
    require_matching_installation(configuration, deployment.outputs())
    approval = ["-auto-approve", "-input=false"] if arguments.yes else []

    if arguments.command == "bootstrap":
        bootstrap.initialize()
        bootstrap.bindings(configuration)
        if arguments.plan:
            bootstrap.command("plan", "-input=false")
        else:
            bootstrap.command("apply", *approval)
            bindings = bootstrap.bindings(configuration)
            if bindings is None:
                raise InstallError("Bootstrap completed without producing deployment role outputs")
            print(f"Deployment role: {bindings.deployment_role_arn}")
        return

    # Fail on missing secret files before an apply creates any resources.
    deployment_variables = configuration.deployment_variables(resolve_github=arguments.command != "destroy")
    bootstrap.initialize()
    bindings = bootstrap.bindings(configuration)
    if bindings is None:
        if arguments.command == "destroy" or arguments.deployment_only:
            raise InstallError("Bootstrap state is missing. Run python3 installer.py bootstrap with the existing authorized AWS identity first")
        if arguments.command == "plan":
            bootstrap.command("plan", "-input=false")
            print("Bootstrap role does not exist yet. This plan covers bootstrap only; apply bootstrap before planning deployment.")
            return
        bootstrap.command("apply", *approval)
        bindings = bootstrap.bindings(configuration)
        if bindings is None:
            raise InstallError("Bootstrap completed without producing deployment role outputs")

    deployment.variables = {
        **deployment_variables,
        "deployment_role_arn": bindings.deployment_role_arn,
        "workload_boundary_arn": bindings.workload_boundary_arn,
    }
    deployment.initialize()
    if arguments.command == "plan":
        deployment.command("plan", "-input=false")
    elif arguments.command == "destroy":
        deployment.command("destroy", *approval)
        for name in ("installation", "publishing"):
            (root / ".local" / "contracts" / f"{name}.json").unlink(missing_ok=True)
        print("Deployment destroyed. Bootstrap IAM resources and local state are retained.")
    else:
        deployment.command("apply", *approval)
        export_contracts(deployment, root / ".local" / "contracts")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    descriptions = {
        "plan": "Plan deployment, or bootstrap only when its role does not yet exist",
        "apply": "Create bootstrap IAM if needed, deploy RunsOn, and export JSON contracts",
        "bootstrap": "Create or update the bootstrap IAM roles explicitly",
        "export": "Recreate both JSON contracts from deployment state",
        "destroy": "Destroy deployment resources; retain bootstrap IAM and local state",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--profile", help="AWS profile for the existing authorized identity")
        command.set_defaults(yes=False, deployment_only=False)
        if name != "export":
            command.add_argument("--config", type=Path, default=ROOT / ".local" / "config.toml", help="TOML configuration (default: .local/config.toml)")
        if name in {"apply", "bootstrap", "destroy"}:
            command.add_argument("--yes", action="store_true", help="Explicitly approve Terraform apply/destroy without a prompt")
        if name == "apply":
            command.add_argument("--deployment-only", action="store_true", help="Require existing bootstrap state; never create bootstrap IAM")
        if name == "bootstrap":
            command.add_argument("--plan", action="store_true", help="Plan bootstrap IAM without applying")
    return result


def main() -> int:
    os.umask(0o077)
    arguments = parser().parse_args()
    try:
        execute(arguments)
    except (InstallError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
