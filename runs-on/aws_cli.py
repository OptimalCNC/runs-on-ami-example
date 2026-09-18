"""Use the selected AWS identity for Terraform and account preparation."""

import os


def aws_environment(profile: str | None) -> dict[str, str]:
    environment = dict(os.environ)
    if profile:
        environment["AWS_PROFILE"] = profile
        environment["AWS_DEFAULT_PROFILE"] = profile
        for name in (
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
            "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_SESSION_NAME",
        ):
            environment.pop(name, None)
    return environment
