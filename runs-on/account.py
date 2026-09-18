"""Prepare shared AWS account prerequisites without changing installation state."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from aws_cli import aws_environment
from configuration import Configuration, InstallError


def aws_command(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["aws", *arguments, "--output", "json"],
            env={**environment, "AWS_PAGER": ""}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except FileNotFoundError as exc:
        raise InstallError("AWS CLI is required for account preparation") from exc


def ensure_account_prerequisites(environment: dict[str, str], configuration: Configuration) -> None:
    """Create account-shared AWS resources with the original AWS identity."""
    identity = aws_command(environment, "sts", "get-caller-identity", "--region", configuration.region)
    if identity.returncode:
        raise InstallError(f"Cannot identify the AWS account: {identity.stderr.strip()}")
    actual_account = json.loads(identity.stdout)["Account"]
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
            audiences = json.loads(result.stdout)["ClientIDList"]
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / ".local/config.toml", help="TOML configuration (default: .local/config.toml)")
    result.add_argument("--profile", help="AWS profile authorized to prepare shared account resources")
    return result


def execute(arguments: argparse.Namespace) -> None:
    configuration = Configuration.load(arguments.config)
    ensure_account_prerequisites(aws_environment(arguments.profile), configuration)
    print(f"Account prerequisites ready in {configuration.account_id}")


def main() -> int:
    try:
        execute(parser().parse_args())
    except (InstallError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
