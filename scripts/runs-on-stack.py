#!/usr/bin/env python3
"""Prepare a RunsOn change set, execute its exact record, or inspect an installed stack."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import quote

from example import ROOT, digest, file_sha, read_json, require, timestamp, utcnow, write_json
from runs_on import (ReviewedChangeSet, StackAws, StackConfig, change_set_arn,
                     parse_template, public_change_set, relative_file, stack_arn)


def prepared_template(config, path):
    prepared = read_json(path)
    require(prepared.get("schema_version") == 1 and prepared.get("kind") == "runs-on-prepared-template",
            "expected a prepared RunsOn template record")
    for name in ("account_id", "region", "repository", "stack_name"):
        require(prepared[name] == getattr(config, name), f"prepared template {name} differs")
    require(prepared["config_sha256"] == config.sha256, "configuration differs from prepared template")
    template = relative_file(path, prepared["template_file"])
    require(file_sha(template) == prepared["template_sha256"], "prepared template bytes differ")
    require(digest(read_json(template)) == prepared["template_content_sha256"], "prepared template content differs")
    return prepared, template


def plan(config, prepared_path, aws, timeout_seconds=600):
    prepared, template = prepared_template(config, prepared_path)
    aws.identify()
    # This account prerequisite is established explicitly before planning.
    aws.call("iam", "get-role", {"RoleName": "AWSServiceRoleForECS"})
    license_path = config.license_file
    require(license_path is not None and license_path.stat().st_mode & 0o777 == 0o600,
            "license file must have mode 0600")
    license_value = license_path.read_text().strip()
    require(bool(license_value), "license file is empty")
    require(config.notification_email_file is not None, "notification email file is required")
    email = config.notification_email_file.read_text().strip()
    require("@" in email and "\n" not in email, "notification email is invalid")
    aws.redactions.extend([license_value, email])
    values = {name: ",".join(str(item) for item in value) if isinstance(value, list) else str(value)
              for name, value in prepared["parameters"].items()}
    values.update(LicenseKey=license_value, EmailAddress=email)
    template_sha = prepared["template_sha256"]
    key = prepared["template_prefix"] + template_sha + ".json"
    uploaded = aws.call("s3api", "put-object", {
        "Bucket": prepared["template_bucket"], "Key": key, "Body": str(template),
        "ServerSideEncryption": "AES256", "ContentType": "application/json",
        "ExpectedBucketOwner": config.account_id})
    version = uploaded.get("VersionId")
    require(bool(version) and version != "null", "template must have an immutable S3 version")
    url = (f"https://{prepared['template_bucket']}.s3.{config.region}.amazonaws.com/"
           f"{key}?versionId={quote(version, safe='')}")
    existing = aws.call("cloudformation", "list-stacks", {"StackStatusFilter": [
        "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "REVIEW_IN_PROGRESS"]})
    matches = [item for item in existing["StackSummaries"] if item["StackName"] == config.stack_name]
    change_type = "UPDATE" if matches and matches[0]["StackStatus"] != "REVIEW_IN_PROGRESS" else "CREATE"
    description = "template-sha256:" + template_sha
    response = aws.call("cloudformation", "create-change-set", {
        "StackName": config.stack_name,
        "ChangeSetName": "review-" + template_sha[:12] + "-" + utcnow().strftime("%Y%m%d%H%M%S%f"),
        "ChangeSetType": change_type, "TemplateURL": url, "Description": description,
        "Capabilities": ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
        "Parameters": [{"ParameterKey": name, "ParameterValue": value} for name, value in values.items()],
        "Tags": [{"Key": "ami-example:owner", "Value": config.repository},
                 {"Key": "ami-example:purpose", "Value": "runs-on-service"}]})
    stack = stack_arn(response["StackId"], config.account_id, config.region, config.stack_name)
    change_set = change_set_arn(response["Id"], config.account_id, config.region)
    deadline = time.monotonic() + timeout_seconds
    while True:
        changes = aws.change_set(change_set)
        if changes["Status"] not in ("CREATE_PENDING", "CREATE_IN_PROGRESS"):
            break
        require(time.monotonic() < deadline, f"change-set preparation timed out: {change_set}")
        time.sleep(2)
    require(changes["Status"] == "CREATE_COMPLETE" and changes["ExecutionStatus"] == "AVAILABLE",
            "change-set preparation failed: " + changes.get("StatusReason", changes["Status"]))
    evaluated = public_change_set(changes)
    require(evaluated["stack_id"] == stack and evaluated["change_set_id"] == change_set,
            "AWS returned a different change set")
    return {"schema_version": 1, "kind": "runs-on-change-set", "account_id": config.account_id,
            "region": config.region, "stack_name": config.stack_name,
            "stack_id": stack, "change_set_id": change_set, "change_set_type": change_type,
            "template_sha256": template_sha,
            "template_content_sha256": prepared["template_content_sha256"],
            "template_bucket": prepared["template_bucket"], "template_key": key,
            "template_version_id": version, "evaluated": evaluated,
            "evaluated_sha256": digest(evaluated), "prepared_at": timestamp()}


def apply(record, aws):
    aws.identify()
    changes = aws.change_set(record.change_set_id)
    require(changes["Status"] == "CREATE_COMPLETE" and changes["ExecutionStatus"] == "AVAILABLE",
            "change set is not ready for execution")
    require(public_change_set(changes) == record.evaluated, "live change set differs from reviewed record")
    template = aws.call("cloudformation", "get-template", {
        "StackName": record.stack_id, "ChangeSetName": record.change_set_id, "TemplateStage": "Original"})["TemplateBody"]
    template = parse_template(template)
    require(digest(template) == record.template_content_sha256, "change-set template differs from reviewed record")
    aws.call("cloudformation", "execute-change-set", {
        "StackName": record.stack_id, "ChangeSetName": record.change_set_id})
    return {"schema_version": 1, "kind": "runs-on-execution", "account_id": record.account_id,
            "region": record.region, "stack_id": record.stack_id,
            "change_set_id": record.change_set_id, "status": "execution_started",
            "started_at": timestamp()}


def inventory(config, aws):
    aws.identify()
    stack = aws.call("cloudformation", "describe-stacks", {"StackName": config.stack_name})["Stacks"][0]
    stack_arn(stack["StackId"], config.account_id, config.region, config.stack_name)
    resources = aws.call("cloudformation", "list-stack-resources", {"StackName": stack["StackId"]})["StackResourceSummaries"]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    parameters = {item["ParameterKey"]: item.get("ParameterValue") for item in stack.get("Parameters", [])
                  if item["ParameterKey"] not in ("LicenseKey", "EmailAddress")}
    require(parameters.get("GithubOrganization") == config.repository.split("/")[0],
            "RunsOn stack belongs to another GitHub organization")
    require(parameters.get("Environment") == config.environment, "RunsOn environment differs")
    require(outputs.get("RunsOnAwsAccountId") == config.account_id
            and outputs.get("RunsOnRegion") == config.region, "RunsOn output account or region differs")
    lock = read_json(ROOT / "infra/runs-on/template-lock.json")
    version = outputs.get("RunsOnAppTag", "").removeprefix("v")
    require(version == lock["version"], "installed RunsOn version is unsupported")
    template = parse_template(aws.call("cloudformation", "get-template", {
        "StackName": stack["StackId"], "TemplateStage": "Original"})["TemplateBody"])
    bootstrap = template["Mappings"]["App"]["Tags"]["BootstrapTag"].removeprefix("v")
    require(bootstrap == lock["bootstrap_version"], "installed RunsOn bootstrap version is unsupported")
    launch_id, launch_version = outputs["RunsOnLaunchTemplateLinuxDefault"].split(":", 1)
    launch = aws.call("ec2", "describe-launch-template-versions", {
        "LaunchTemplateId": launch_id, "Versions": [launch_version]})["LaunchTemplateVersions"][0]["LaunchTemplateData"]
    tags = {tag["Key"]: tag["Value"] for item in launch.get("TagSpecifications", [])
            if item["ResourceType"] == "instance" for tag in item.get("Tags", [])}
    require(tags.get("ami-example:runs-on-repository") == config.repository,
            "RunsOn runner launch template lacks this repository's ownership marker")
    observed = {item["LogicalResourceId"]: item for item in resources}
    role_name = outputs.get("RunsOnInstanceRoleName")
    role_arn = aws.call("iam", "get-role", {"RoleName": role_name})["Role"]["Arn"] if role_name else None
    profile = observed.get("EC2InstanceProfile", {})
    profile_arn = None
    if profile.get("ResourceStatus") in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"):
        profile_arn = aws.call("iam", "get-instance-profile", {
            "InstanceProfileName": profile["PhysicalResourceId"]})["InstanceProfile"]["Arn"]
    return {"schema_version": 1, "kind": "runs-on-inventory", "account_id": config.account_id,
            "region": config.region, "repository": config.repository,
            "stack_name": config.stack_name, "stack_id": stack["StackId"], "status": stack["StackStatus"],
            "runs_on": {"environment": config.environment, "version": version, "bootstrap_version": bootstrap},
            "outputs": outputs, "parameters": parameters, "resources": resources,
            "service_role_arn": outputs.get("RunsOnServiceRoleArn"),
            "runner_role_arn": role_arn, "runner_profile_arn": profile_arn, "observed_at": timestamp()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="task", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--prepared", type=Path, required=True)
    plan_parser.add_argument("--timeout-seconds", type=int, default=600)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--change-set", type=Path, required=True)
    inventory_parser = commands.add_parser("inventory")
    inventory_parser.add_argument("--config", type=Path, required=True)
    for command in (plan_parser, apply_parser, inventory_parser):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--profile")
    args = parser.parse_args()
    if args.task == "apply":
        record = ReviewedChangeSet.read(args.change_set)
        result = apply(record, StackAws(record.account_id, record.region, args.profile))
    else:
        config = StackConfig.read(args.config)
        aws = StackAws(config.account_id, config.region, args.profile)
        result = (plan(config, args.prepared, aws, args.timeout_seconds) if args.task == "plan"
                  else inventory(config, aws))
    write_json(args.output, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
