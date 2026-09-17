"""Parse the shared installation settings and publisher access configuration."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import subprocess

import yaml


class InstallError(Exception):
    """A configuration or external-command failure the user can act on."""


def string(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise InstallError(f"{label} must be {'a' if empty else 'a nonempty'} string")
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
        if not isinstance(value, dict):
            raise InstallError("Each publisher_github_repositories entry must be a mapping")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise InstallError(f"Unknown GitHub publisher fields: {', '.join(sorted(map(str, unknown)))}")
        repository = string(value.get("repository"), "GitHub publisher repository")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise InstallError("GitHub publisher repository must have the form owner/repository")
        environment = string(value.get("environment", "image-publish"), "GitHub publisher environment")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", environment):
            raise InstallError("GitHub publisher environment must contain letters, digits, underscores, dots or hyphens")
        subject_prefix = string(value.get("subject_prefix", ""), "GitHub publisher subject_prefix", empty=True)
        if subject_prefix:
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
            value = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise InstallError(f"Cannot read configuration {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise InstallError("Configuration must be a YAML mapping")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise InstallError("schema_version must be 1")
        unknown = set(value) - {"schema_version", *cls.__dataclass_fields__}
        if unknown:
            raise InstallError(f"Unknown configuration fields: {', '.join(sorted(map(str, unknown)))}")

        account_id = string(value.get("account_id"), "account_id")
        if not re.fullmatch(r"[0-9]{12}", account_id):
            raise InstallError("account_id must be a quoted 12-digit AWS account ID")
        region = string(value.get("region"), "region")
        if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", region):
            raise InstallError("region must be an AWS commercial region name")
        name = string(value.get("name"), "name")
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,23}", name):
            raise InstallError("name must be 3-24 lowercase letters, digits or hyphens, starting with a letter")
        environment = string(value.get("environment"), "environment")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", environment):
            raise InstallError("environment must begin with a lowercase letter and contain lowercase letters, digits or hyphens")
        organization = string(value.get("github_organization"), "github_organization")
        repositories = value.get("publisher_github_repositories", [])
        if not isinstance(repositories, list):
            raise InstallError("publisher_github_repositories must be a list")
        github_publishers = tuple(GitHubPublisher.parse(entry) for entry in repositories)
        if len({(entry.repository, entry.environment) for entry in github_publishers}) != len(github_publishers):
            raise InstallError("GitHub publisher repository/environment pairs must be unique")
        publisher_principals = principal_arns(value.get("publisher_principal_arns", []), "publisher_principal_arns")
        if not publisher_principals and not github_publishers:
            raise InstallError("Configure at least one publisher_principal_arns entry or publisher_github_repositories entry")
        vpc_cidr = string(value.get("vpc_cidr", "10.80.0.0/16"), "vpc_cidr")
        try:
            network = ipaddress.IPv4Network(vpc_cidr)
        except ValueError as exc:
            raise InstallError("vpc_cidr must be a canonical IPv4 network") from exc
        if not 16 <= network.prefixlen <= 27:
            raise InstallError("vpc_cidr must have prefix length 16 through 27")
        trusted_principals = principal_arns(value.get("trusted_principal_arns"), "trusted_principal_arns", required=True)
        if any(not arn.startswith(f"arn:aws:iam::{account_id}:") for arn in trusted_principals):
            raise InstallError("trusted_principal_arns must belong to account_id")

        return cls(
            account_id, region, name, environment, organization,
            (path.parent / string(value.get("license_file"), "license_file")).resolve(),
            (path.parent / string(value.get("notification_email_file"), "notification_email_file")).resolve(),
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
            key: getattr(self, key)
            for key in self.__dataclass_fields__
            if key not in {"license_file", "notification_email_file", "trusted_principal_arns", "publisher_github_repositories"}
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
        discovered_prefixes = {}
        for publisher in self.publisher_github_repositories:
            prefix = publisher.subject_prefix
            if not prefix:
                if publisher.repository not in discovered_prefixes:
                    discovered_prefixes[publisher.repository] = (
                        github_subject_prefix(publisher.repository)
                        if resolve_github else f"repo:{publisher.repository}"
                    )
                prefix = discovered_prefixes[publisher.repository]
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
    try:
        settings = json.loads(result.stdout)
        if not isinstance(settings, dict):
            raise TypeError("expected settings object")
    except (json.JSONDecodeError, TypeError) as exc:
        raise InstallError("GitHub returned invalid repository OIDC settings") from exc
    if settings.get("use_default") is not True:
        raise InstallError(f"Repository {repository} uses a custom OIDC subject; configure its subject_prefix explicitly after checking its environment subject format")
    prefix = settings.get("sub_claim_prefix", f"repo:{repository}")
    return parse_subject_prefix(prefix, repository)
