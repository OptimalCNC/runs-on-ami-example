"""Explicit file and AWS boundaries for RunsOn stack operations."""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from example import digest, match, read_json, require


class CloudFormationLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    if tag == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {tag if tag in ("Ref", "Condition") else "Fn::" + tag: value}


CloudFormationLoader.add_multi_constructor("!", intrinsic)


def parse_template(source):
    return yaml.load(source, Loader=CloudFormationLoader) if isinstance(source, (str, bytes)) else source


SERVICE_PARAMETERS = {
    "AppSize", "AppCapacityProvider", "AppGithubApiStrategy", "LoggerLevel",
    "SpotCircuitBreaker", "MaintenanceMode", "OtelExporterEndpoint", "OtelExporterTemporality",
    "CostReportsEnabled", "AppBudgetDailyUsd", "CostAllocationTag", "S3CacheExpirationInDays",
    "RunnerMaxRuntime", "EnableWAF", "EnableAdminRoutes",
}


@dataclasses.dataclass(frozen=True)
class StackConfig:
    account_id: str
    region: str
    repository: str
    stack_name: str
    environment: str
    source_ami_id: str
    controller_ami_id: str
    instance_type: str
    root_volume_gib: int
    private: bool
    vpc_cidr: str
    overrides: dict
    license_file: Path | None
    notification_email_file: Path | None
    sha256: str

    @classmethod
    def read(cls, path: Path) -> StackConfig:
        from deployment_config import load_spec

        spec = load_spec(path)
        value = spec.values
        service = value["runs_on"]
        require(isinstance(service, dict), "runs_on must be an object")
        require(not (set(service) - {"stack_name", "environment", "vpc_cidr", "parameters", "mode", "private",
                                    "license_file", "notification_email_file"}),
                "unsupported RunsOn configuration field")
        require(service.get("mode", "existing") in ("existing", "create"), "RunsOn mode must be existing or create")
        private = service.get("private", False)
        require(isinstance(private, bool), "RunsOn private must be a boolean")
        require(not private, "this RunsOn policy supports public service networking; runs_on.private must be false")
        name = match(service["stack_name"], r"[A-Za-z][A-Za-z0-9-]{0,127}", "RunsOn stack name")
        environment = match(service.get("environment", value["environment"]),
                            r"[A-Za-z0-9_-]+", "RunsOn environment")
        cidr = str(ipaddress.IPv4Network(service.get("vpc_cidr", "10.60.0.0/16")))
        overrides = service.get("parameters", {})
        require(isinstance(overrides, dict) and not (set(overrides) - SERVICE_PARAMETERS),
                "RunsOn parameters may override only documented service settings")
        require(all(isinstance(item, (str, int, float)) and not isinstance(item, bool)
                    for item in overrides.values()), "RunsOn parameter values must be strings or numbers")
        def private_file(key):
            reference = service.get(key)
            return relative_file(Path(spec.directory) / "deployment.json", reference) if reference else None
        choices = {key: value[key] for key in ("account_id", "region", "repository", "source_ami",
                                               "controller_ami", "instance_type", "root_volume_gib")}
        choices["runs_on"] = {**service, "environment": environment, "private": private, "vpc_cidr": cidr}
        return cls(value["account_id"], value["region"], value["repository"], name, environment,
                   value["source_ami"]["id"], value["controller_ami"]["id"], value["instance_type"],
                   value["root_volume_gib"], private, cidr, overrides,
                   private_file("license_file"), private_file("notification_email_file"), digest(choices))

    def parameters(self, defaults: dict) -> dict:
        return {**defaults, **self.overrides, "GithubOrganization": self.repository.split("/")[0],
                "Environment": self.environment, "VpcCidrBlock": self.vpc_cidr,
                "Private": "true" if self.private else "false"}


def terraform_outputs(path: Path) -> dict:
    value = read_json(path)
    require(isinstance(value, dict) and all(isinstance(item, dict) and "value" in item
                                          for item in value.values()),
            "foundation must be the JSON produced by terraform output -json")
    return {name: item["value"] for name, item in value.items()}


def relative_file(owner: Path, reference: str) -> Path:
    require(isinstance(reference, str) and bool(reference), "file reference is empty")
    path = Path(reference)
    return (owner.parent / path).resolve() if not path.is_absolute() else path.resolve()


def stack_arn(value: str, account_id: str, region: str, stack_name: str) -> str:
    return match(value, rf"arn:aws:cloudformation:{re.escape(region)}:{account_id}:stack/"
                 rf"{re.escape(stack_name)}/[A-Za-z0-9-]+", "stack ARN")


def change_set_arn(value: str, account_id: str, region: str) -> str:
    return match(value, rf"arn:aws:cloudformation:{re.escape(region)}:{account_id}:changeSet/"
                 r"[A-Za-z0-9-]+/[A-Za-z0-9-]+", "change-set ARN")


def public_change_set(response: dict) -> dict:
    """Keep evaluated changes and non-secret parameters, never license/email values."""
    return {"stack_id": response["StackId"], "change_set_id": response["ChangeSetId"],
            "changes": response.get("Changes", []),
            "parameters": sorted((item for item in response.get("Parameters", [])
                                  if item["ParameterKey"] not in ("LicenseKey", "EmailAddress")),
                                 key=lambda item: item["ParameterKey"]),
            "capabilities": sorted(response.get("Capabilities", [])),
            "description": response.get("Description", "")}


@dataclasses.dataclass(frozen=True)
class ReviewedChangeSet:
    account_id: str
    region: str
    stack_name: str
    stack_id: str
    change_set_id: str
    template_content_sha256: str
    evaluated: dict

    @classmethod
    def read(cls, path: Path) -> ReviewedChangeSet:
        value = read_json(path)
        require(value.get("schema_version") == 1 and value.get("kind") == "runs-on-change-set",
                "expected a reviewed RunsOn change-set record")
        account_id = match(value["account_id"], r"[0-9]{12}", "account ID")
        region = match(value["region"], r"[a-z]{2}-[a-z]+-[0-9]+", "region")
        name = match(value["stack_name"], r"[A-Za-z][A-Za-z0-9-]{0,127}", "stack name")
        stack = stack_arn(value["stack_id"], account_id, region, name)
        change_set = change_set_arn(value["change_set_id"], account_id, region)
        content_sha = match(value["template_content_sha256"], r"[a-f0-9]{64}", "template content SHA256")
        evaluated = value["evaluated"]
        require(evaluated["stack_id"] == stack and evaluated["change_set_id"] == change_set,
                "evaluated changes belong to another stack or change set")
        require(value["evaluated_sha256"] == digest(evaluated), "evaluated change-set record differs")
        template_sha = match(value["template_sha256"], r"[a-f0-9]{64}", "template SHA256")
        require(evaluated["description"] == "template-sha256:" + template_sha,
                "change set is not bound to its prepared template")
        return cls(account_id, region, name, stack, change_set, content_sha, evaluated)


class StackAws:
    def __init__(self, account_id: str, region: str, profile: str | None = None):
        self.account_id = account_id
        self.region = region
        self.profile = profile
        self.redactions: list[str] = []

    def call(self, service: str, operation: str, payload: dict | None = None) -> dict:
        command = ["aws", "--region", self.region, "--output", "json", "--no-cli-pager"]
        if self.profile:
            command += ["--profile", self.profile]
        command += [service, operation]
        payload = dict(payload) if payload is not None else None
        if service == "s3api" and operation == "put-object":
            command += ["--body", payload.pop("Body")]
        # License values enter the CLI through a private temporary request file.
        with tempfile.TemporaryDirectory(prefix="runs-on-request-") as temporary:
            if payload is not None:
                request = Path(temporary) / "request.json"
                descriptor = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(payload, stream)
                command += ["--cli-input-json", "file://" + str(request)]
            completed = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=120)
        if completed.returncode:
            message = completed.stderr
            for secret in self.redactions:
                message = message.replace(secret, "[REDACTED]")
            raise RuntimeError(f"{service} {operation}: {message.strip()}")
        return json.loads(completed.stdout) if completed.stdout else {}

    def identify(self) -> None:
        require(self.call("sts", "get-caller-identity")["Account"] == self.account_id,
                "AWS identity belongs to another account")

    def change_set(self, arn: str) -> dict:
        response = self.call("cloudformation", "describe-change-set", {"ChangeSetName": arn})
        token = response.pop("NextToken", None)
        while token:
            page = self.call("cloudformation", "describe-change-set",
                             {"ChangeSetName": arn, "NextToken": token})
            response.setdefault("Changes", []).extend(page.get("Changes", []))
            token = page.get("NextToken")
        return response
