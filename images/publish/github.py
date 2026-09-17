"""Prepare the installation's publishing target for a GitHub OIDC job."""
import argparse
import os
from pathlib import Path

from .image import PublishingTarget, load_yaml


def load_github_target(path: Path, repository: str, environment: str) -> PublishingTarget:
    target = PublishingTarget.load(path)
    if target.legacy_kms_key_arn is not None:
        raise ValueError("GitHub publication requires a version 3 publishing target")
    authentication = load_yaml(path).get("authentication")
    github = authentication.get("github") if isinstance(authentication, dict) else None
    if (not isinstance(github, dict) or github.get("method") != "github-oidc"
            or github.get("audience") != "sts.amazonaws.com"):
        raise ValueError("Publishing target must authorize GitHub OIDC")
    repositories = github.get("repositories")
    if not isinstance(repositories, list) or not any(
        isinstance(entry, dict) and entry.get("repository") == repository
        and entry.get("environment") == environment for entry in repositories
    ):
        raise ValueError("Publishing target does not authorize this repository and GitHub environment")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    value = os.environ.get("PUBLISHING_TARGET", "")
    if not value.strip():
        parser.error("set the GitHub environment variable PUBLISHING_TARGET to the exported publishing.yaml")
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(value)
    target = load_github_target(args.target, os.environ["GITHUB_REPOSITORY"],
                                os.environ["PUBLISHING_ENVIRONMENT"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"role_arn={target.publisher_role_arn}\nregion={target.region}\n")


if __name__ == "__main__":
    main()
