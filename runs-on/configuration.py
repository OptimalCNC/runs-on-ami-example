"""Parse the shared installation settings and publisher access configuration."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tomllib


class InstallError(Exception):
    """A configuration or external-command failure the user can act on."""


def string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstallError(f"{label} must be a nonempty string")
    return value


def table(value: object, label: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InstallError(f"{label} must be a TOML table")
    unknown = set(value) - fields
    if unknown:
        raise InstallError(f"Unknown {label} fields: {', '.join(sorted(unknown))}")
    return value


def principal_arns(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise InstallError(f"{label} must be {'a nonempty' if required else 'a'} list")
    result = []
    for item in value:
        arn = string(item, label)
        if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:(?:role|user)/[A-Za-z0-9_+=,.@/-]+", arn):
            raise InstallError(f"{label} must contain IAM role or user ARNs")
        result.append(arn)
    return tuple(result)


@dataclass(frozen=True)
class GitHubPublisher:
    repository: str
    environment: str
    subject_prefix: str

    @classmethod
    def parse(cls, value: object) -> GitHubPublisher:
        value = table(value, "GitHub publisher", {"repository", "environment", "subject_prefix"})
        repository = string(value.get("repository"), "GitHub publisher repository")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise InstallError("GitHub publisher repository must have the form owner/repository")
        environment = string(value.get("environment", "image-publish"), "GitHub publisher environment")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", environment):
            raise InstallError("GitHub publisher environment must contain letters, digits, underscores, dots or hyphens")
        subject_prefix = value.get("subject_prefix", "")
        if subject_prefix != "":
            subject_prefix = parse_subject_prefix(subject_prefix, repository)
        return cls(repository, environment, subject_prefix)


@dataclass(frozen=True)
class Configuration:
    account_id: str
    region: str
    name: str
    environment: str
    github_organization: str
    license_file: Path
    notification_email_file: Path
    trusted_principal_arns: tuple[str, ...]
    publisher_principal_arns: tuple[str, ...]
    publisher_github_repositories: tuple[GitHubPublisher, ...]
    vpc_cidr: str

    @classmethod
    def load(cls, path: Path) -> Configuration:
        path = path.expanduser().resolve()
        try:
            with path.open("rb") as source:
                value = tomllib.load(source)
        except (OSError, ValueError) as exc:
            raise InstallError(f"Cannot read configuration {path}: {exc}") from exc
        value = table(value, "configuration", {"aws", "installation", "deployment", "publishing"})
        aws = table(value.get("aws"), "aws", {"account_id", "region"})
        installation = table(value.get("installation"), "installation", {
            "name", "environment", "github_organization", "vpc_cidr", "license_file", "notification_email_file",
        })
        deployment = table(value.get("deployment"), "deployment", {"trusted_principal_arns"})
        publishing = table(value.get("publishing"), "publishing", {"principal_arns", "github_repositories"})

        account_id = string(aws.get("account_id"), "aws.account_id")
        if not re.fullmatch(r"[0-9]{12}", account_id):
            raise InstallError("account_id must be a quoted 12-digit AWS account ID")
        region = string(aws.get("region"), "aws.region")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", region):
            raise InstallError("region must be an AWS commercial region name")
        name = string(installation.get("name"), "installation.name")
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,23}", name):
            raise InstallError("name must be 3-24 lowercase letters, digits or hyphens, starting with a letter")
        environment = string(installation.get("environment"), "installation.environment")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", environment):
            raise InstallError("environment must begin with a lowercase letter and contain lowercase letters, digits or hyphens")
        organization = string(installation.get("github_organization"), "installation.github_organization")
        repositories = publishing.get("github_repositories", [])
        if not isinstance(repositories, list):
            raise InstallError("publishing.github_repositories must be an array of tables")
        github_publishers = tuple(GitHubPublisher.parse(entry) for entry in repositories)
        if len({(entry.repository, entry.environment) for entry in github_publishers}) != len(github_publishers):
            raise InstallError("GitHub publisher repository/environment pairs must be unique")
        publisher_principals = principal_arns(publishing.get("principal_arns", []), "publishing.principal_arns")
        if not publisher_principals and not github_publishers:
            raise InstallError("Configure at least one publishing.principal_arns entry or publishing.github_repositories entry")
        vpc_cidr = string(installation.get("vpc_cidr", "10.80.0.0/16"), "installation.vpc_cidr")
        try:
            network = ipaddress.IPv4Network(vpc_cidr)
        except ValueError as exc:
            raise InstallError("vpc_cidr must be a canonical IPv4 network") from exc
        if not 16 <= network.prefixlen <= 27:
            raise InstallError("vpc_cidr must have prefix length 16 through 27")
        trusted_principals = principal_arns(deployment.get("trusted_principal_arns"), "deployment.trusted_principal_arns", required=True)
        if any(not arn.startswith(f"arn:aws:iam::{account_id}:") for arn in trusted_principals):
            raise InstallError("trusted_principal_arns must belong to account_id")

        return cls(
            account_id, region, name, environment, organization,
            (path.parent / string(installation.get("license_file"), "installation.license_file")).resolve(),
            (path.parent / string(installation.get("notification_email_file"), "installation.notification_email_file")).resolve(),
            trusted_principals,
            publisher_principals,
            github_publishers, str(network),
        )

    def bootstrap_variables(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "region": self.region,
            "name": self.name,
            "trusted_principal_arns": self.trusted_principal_arns,
        }

    def deployment_variables(self, *, resolve_github: bool = True) -> dict[str, object]:
        result = {
            "account_id": self.account_id,
            "region": self.region,
            "name": self.name,
            "environment": self.environment,
            "github_organization": self.github_organization,
            "publisher_principal_arns": self.publisher_principal_arns,
            "vpc_cidr": self.vpc_cidr,
        }
        for name, path in (("license_key", self.license_file), ("notification_email", self.notification_email_file)):
            try:
                secret = path.read_text().strip()
            except OSError as exc:
                raise InstallError(f"Cannot read {name} from {path}: {exc}") from exc
            if not secret:
                raise InstallError(f"{name} file is empty: {path}")
            result[name] = secret
        publishers = []
        for publisher in self.publisher_github_repositories:
            prefix = publisher.subject_prefix
            if not prefix:
                prefix = github_subject_prefix(publisher.repository) if resolve_github else f"repo:{publisher.repository}"
            publishers.append({"repository": publisher.repository, "environment": publisher.environment, "subject_prefix": prefix})
        result["publisher_github_repositories"] = publishers
        return result


def parse_subject_prefix(value: object, repository: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"repo:[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?", value):
        raise InstallError("GitHub publisher subject_prefix must have the form repo:owner/repository, optionally with immutable numeric IDs")
    if re.sub(r"@[0-9]+", "", value) != f"repo:{repository}":
        raise InstallError(f"GitHub publisher subject_prefix must identify repository {repository}")
    return value


def github_subject_prefix(repository: str) -> str:
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repository}/actions/oidc/customization/sub"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except FileNotFoundError as exc:
        raise InstallError("GitHub CLI is required to resolve publishing OIDC settings; install gh and authenticate, or configure each GitHub publisher's subject_prefix explicitly") from exc
    if result.returncode:
        raise InstallError(f"Cannot resolve publishing OIDC subject for {repository}: {result.stderr.strip()}. Authenticate gh or configure its subject_prefix explicitly")
    settings = json.loads(result.stdout)
    if settings.get("use_default") is not True:
        raise InstallError(f"Repository {repository} uses a custom OIDC subject; configure its subject_prefix explicitly after checking its environment subject format")
    prefix = settings.get("sub_claim_prefix", f"repo:{repository}")
    return parse_subject_prefix(prefix, repository)
