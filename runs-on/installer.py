"""Connect the RunsOn bootstrap and deployment Terraform roots."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Iterator

try:
    import yaml
except ImportError:
    raise SystemExit(
        "PyYAML is required. Run: python3 -m venv .venv && "
        ".venv/bin/pip install -r requirements.txt"
    ) from None

from configuration import Configuration, InstallError, string


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

    def variables(self) -> dict[str, object]:
        return dict(self.__dict__)


class Terraform:
    def __init__(self, root: Path, component: str, profile: str | None):
        self.directory = root / component
        self.state_path = root / ".local" / "state" / f"{component}.tfstate"
        self.data_path = root / ".local" / "terraform" / component
        self.environment = dict(os.environ)
        # A caller's TF_VAR values must not silently override this configuration.
        self.environment = {key: value for key, value in self.environment.items() if not key.startswith("TF_VAR_")}
        self.environment.update(TF_DATA_DIR=str(self.data_path), TF_IN_AUTOMATION="true", TF_WORKSPACE="default")
        if profile:
            self.environment["AWS_PROFILE"] = profile
            self.environment["AWS_DEFAULT_PROFILE"] = profile
            for name in (
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
                "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_SESSION_NAME",
            ):
                self.environment.pop(name, None)
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

    def apply(self, *, yes: bool) -> None:
        self.command("apply", *(["-auto-approve", "-input=false"] if yes else []))

    def plan(self) -> None:
        self.command("plan", "-input=false")

    def bindings(self, configuration: Configuration) -> BootstrapBindings | None:
        if not self.state_path.exists():
            return None
        try:
            outputs = json.loads(self.command("output", "-json", capture=True))
        except json.JSONDecodeError as exc:
            raise InstallError("Bootstrap Terraform outputs are not valid JSON") from exc
        if outputs == {}:
            return None
        return BootstrapBindings.parse(outputs, configuration)

    def state_values(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {}
        self.initialize()
        try:
            state = json.loads(self.command("show", "-json", str(self.state_path), capture=True))
            values = state.get("values", {})
            if not isinstance(values, dict):
                raise TypeError("expected state values object")
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise InstallError("Terraform returned invalid deployment state values") from exc
        return values


def require_matching_installation(configuration: Configuration, state_values: dict[str, object]) -> None:
    outputs = state_values.get("outputs", {})
    if not isinstance(outputs, dict):
        raise InstallError("Terraform deployment state has invalid outputs")
    for name in ("installation", "publishing"):
        output = outputs.get(name)
        if not isinstance(output, dict) or not isinstance(output.get("value"), dict):
            continue
        contract = output["value"]
        for field in ("account_id", "region", "name"):
            if contract.get(field) != getattr(configuration, field):
                raise InstallError(f"Configuration {field} differs from the existing installation state; restore the original {field} or use a separate checkout and state for another installation")


def aws_command(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["aws", *arguments, "--output", "json"],
            env={**environment, "AWS_PAGER": ""}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except FileNotFoundError as exc:
        raise InstallError("AWS CLI v2 is required for account bootstrap and decommission checks") from exc


def ensure_account_prerequisites(environment: dict[str, str], configuration: Configuration) -> None:
    """Create account-shared AWS resources with the original AWS identity."""
    identity = aws_command(environment, "sts", "get-caller-identity", "--region", configuration.region)
    if identity.returncode:
        raise InstallError(f"Cannot identify the bootstrap AWS account: {identity.stderr.strip()}")
    try:
        actual_account = json.loads(identity.stdout)["Account"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InstallError("AWS returned an invalid caller identity") from exc
    if actual_account != configuration.account_id:
        raise InstallError(f"The existing AWS identity belongs to account {actual_account}, expected {configuration.account_id}")

    for role, service in (("AWSServiceRoleForECS", "ecs.amazonaws.com"), ("AWSServiceRoleForEC2Spot", "spot.amazonaws.com")):
        result = aws_command(environment, "iam", "get-role", "--role-name", role)
        if result.returncode == 0:
            continue
        if "(NoSuchEntity)" not in result.stderr:
            raise InstallError(f"Cannot inspect {role}: {result.stderr.strip()}")
        created = aws_command(environment, "iam", "create-service-linked-role", "--aws-service-name", service)
        if created.returncode:
            raise InstallError(f"Cannot create {role}: {created.stderr.strip()}")
        print(f"Created account service role {role}")

    if configuration.publisher_github_repositories:
        provider_arn = f"arn:aws:iam::{configuration.account_id}:oidc-provider/token.actions.githubusercontent.com"
        result = aws_command(environment, "iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", provider_arn)
        if result.returncode == 0:
            try:
                audiences = json.loads(result.stdout)["ClientIDList"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise InstallError("AWS returned an invalid GitHub OIDC provider") from exc
            if "sts.amazonaws.com" not in audiences:
                raise InstallError("Existing GitHub OIDC provider does not include the sts.amazonaws.com audience")
        elif "(NoSuchEntity)" in result.stderr:
            created = aws_command(
                environment, "iam", "create-open-id-connect-provider",
                "--url", "https://token.actions.githubusercontent.com",
                "--client-id-list", "sts.amazonaws.com",
            )
            if created.returncode:
                raise InstallError(f"Cannot create GitHub OIDC provider: {created.stderr.strip()}")
            print(f"Created account GitHub OIDC provider {provider_arn}")
        else:
            raise InstallError(f"Cannot inspect GitHub OIDC provider: {result.stderr.strip()}")


def require_unused_legacy_key(deployment: Terraform, bindings: BootstrapBindings | None, configuration: Configuration, state_values: dict[str, object]) -> None:
    """An upgrade or removal must not strand disks encrypted by the removed key."""
    module = state_values.get("root_module", {})
    if not isinstance(module, dict):
        raise InstallError("Terraform deployment state has an invalid root module")
    resources = module.get("resources", [])
    if not isinstance(resources, list) or not all(isinstance(resource, dict) for resource in resources):
        raise InstallError("Terraform deployment state has an invalid resource inventory")
    keys = [resource for resource in resources if resource.get("address") == "aws_kms_key.images"]
    if not keys:
        return
    key_arn = keys[0].get("values", {}).get("arn")
    if not isinstance(key_arn, str) or not key_arn.startswith(f"arn:aws:kms:{configuration.region}:{configuration.account_id}:key/"):
        raise InstallError("Deployment state has no valid image KMS key for this account and region")
    if bindings is None:
        raise InstallError("Restore the bootstrap state before retiring the legacy encryption key")

    assumed = aws_command(
        deployment.environment, "sts", "assume-role", "--role-arn", bindings.deployment_role_arn,
        "--role-session-name", "runs-on-key-retirement", "--duration-seconds", "900",
        "--region", configuration.region,
    )
    if assumed.returncode:
        raise InstallError(f"Cannot assume deployment role for image retention checks: {assumed.stderr.strip()}")
    try:
        credentials = json.loads(assumed.stdout)["Credentials"]
        environment = {
            key: value for key, value in deployment.environment.items()
            if key not in {"AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SECURITY_TOKEN"}
        }
        for source, target in (("AccessKeyId", "AWS_ACCESS_KEY_ID"), ("SecretAccessKey", "AWS_SECRET_ACCESS_KEY"), ("SessionToken", "AWS_SESSION_TOKEN")):
            environment[target] = string(credentials[source], "temporary AWS credential")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InstallError("AWS returned invalid temporary deployment credentials") from exc

    retained = []
    for operation, collection, identifier, extra in (
        ("describe-snapshots", "Snapshots", "SnapshotId", ("--owner-ids", "self")),
        ("describe-volumes", "Volumes", "VolumeId", ()),
    ):
        result = aws_command(
            environment, "ec2", operation, "--region", configuration.region, *extra,
            "--filters", "Name=encrypted,Values=true",
        )
        if result.returncode:
            raise InstallError(f"Cannot check retained images with {operation}: {result.stderr.strip()}")
        try:
            resources = json.loads(result.stdout)[collection]
            if not isinstance(resources, list):
                raise TypeError("expected resource list")
            for resource in resources:
                if not isinstance(resource, dict):
                    raise TypeError("expected resource object")
                resource_key = string(resource.get("KmsKeyId"), "encrypted resource KMS key")
                if resource_key == key_arn:
                    retained.append(string(resource[identifier], identifier))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise InstallError(f"AWS returned an invalid {collection} inventory") from exc
    if retained:
        raise InstallError(
            "The legacy encryption key is still used by snapshots or volumes: "
            + ", ".join(retained)
            + ". Stop runner jobs and migrate or retire those resources before updating or removing the installation."
        )


def atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as destination:
            destination.write(contents)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_contract(deployment: Terraform, name: str) -> tuple[str, dict[str, object]]:
    contents = deployment.command("output", "-raw", f"{name}_yaml", capture=True)
    try:
        parsed = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise InstallError(f"Terraform {name}_yaml output is not valid YAML") from exc
    versions = (1, 2, 3, 4) if name == "publishing" else (1,)
    if not isinstance(parsed, dict) or type(parsed.get("schema_version")) is not int or parsed["schema_version"] not in versions:
        raise InstallError(f"Terraform {name}_yaml output must contain schema_version: {' or '.join(map(str, versions))}")
    expected_kind = {"installation": "runs-on-installation", "publishing": "ami-publishing-target"}[name]
    if parsed.get("kind") != expected_kind:
        raise InstallError(f"Terraform {name}_yaml output must have kind: {expected_kind}")
    return contents, parsed


def export_contracts(deployment: Terraform, destination: Path) -> None:
    contracts = {name: read_contract(deployment, name)[0] for name in ("installation", "publishing")}
    # Fetch and parse both before replacing either existing contract.
    for name, contents in contracts.items():
        path = destination / f"{name}.yaml"
        atomic_write(path, contents if contents.endswith("\n") else contents + "\n")
        print(f"Exported {path}")


@contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    local = root / ".local"
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (local / "install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("Another installer command is running in this directory") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def execute(arguments: argparse.Namespace, *, root: Path = ROOT) -> None:
    deployment = Terraform(root, "deployment", arguments.profile)

    with installation_lock(root):
        if arguments.command in {"export", "status"}:
            deployment.initialize()
            if arguments.command == "export":
                export_contracts(deployment, root / ".local" / "contracts")
            else:
                print(read_contract(deployment, "installation")[0], end="\n")
            return

        configuration = Configuration.load(arguments.config)
        bootstrap = Terraform(root, "bootstrap", arguments.profile)
        bootstrap.variables = configuration.bootstrap_variables()
        deployment_state = deployment.state_values()
        require_matching_installation(configuration, deployment_state)

        if arguments.command == "bootstrap" or arguments.bootstrap_only:
            bootstrap.initialize()
            bindings = bootstrap.bindings(configuration)
            if getattr(arguments, "plan", False):
                bootstrap.plan()
            else:
                require_unused_legacy_key(deployment, bindings, configuration, deployment_state)
                ensure_account_prerequisites(bootstrap.environment, configuration)
                bootstrap.apply(yes=arguments.yes)
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
                raise InstallError("Bootstrap state is missing. Run ./install bootstrap with the existing authorized AWS identity first")
            if arguments.command == "plan":
                bootstrap.plan()
                print("Bootstrap role does not exist yet. This plan covers bootstrap only; apply bootstrap before planning deployment.")
                return
            require_unused_legacy_key(deployment, bindings, configuration, deployment_state)
            ensure_account_prerequisites(bootstrap.environment, configuration)
            bootstrap.apply(yes=arguments.yes)
            bindings = bootstrap.bindings(configuration)
            if bindings is None:
                raise InstallError("Bootstrap completed without producing deployment role outputs")

        deployment.variables = {**deployment_variables, **bindings.variables()}
        deployment.initialize()
        if arguments.command == "plan":
            deployment.plan()
        elif arguments.command == "destroy":
            require_unused_legacy_key(deployment, bindings, configuration, deployment_state)
            deployment.command("destroy", *(["-auto-approve", "-input=false"] if arguments.yes else []))
            for name in ("installation", "publishing"):
                (root / ".local" / "contracts" / f"{name}.yaml").unlink(missing_ok=True)
            print("Deployment destroyed. Bootstrap IAM resources and local state are retained.")
        else:
            require_unused_legacy_key(deployment, bindings, configuration, deployment_state)
            deployment.apply(yes=arguments.yes)
            export_contracts(deployment, root / ".local" / "contracts")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="./install", description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    descriptions = {
        "plan": "Plan deployment, or bootstrap only when its role does not yet exist",
        "apply": "Create bootstrap IAM if needed, deploy RunsOn, and export YAML contracts",
        "bootstrap": "Create or update the bootstrap IAM roles explicitly",
        "export": "Recreate both YAML contracts from deployment state",
        "status": "Show the installation contract from deployment state",
        "destroy": "Destroy deployment resources; retain bootstrap IAM and local state",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--config", type=Path, default=ROOT / ".local" / "config.yaml", help="YAML configuration (default: .local/config.yaml)")
        command.add_argument("--profile", help="AWS profile for the existing authorized identity")
        command.add_argument("--yes", action="store_true", help="Explicitly approve Terraform apply/destroy without a prompt")
        command.set_defaults(bootstrap_only=False, deployment_only=False)
        if name == "apply":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--bootstrap-only", action="store_true", help="Only provision bootstrap IAM")
            mode.add_argument("--deployment-only", action="store_true", help="Require existing bootstrap state; never create bootstrap IAM")
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
