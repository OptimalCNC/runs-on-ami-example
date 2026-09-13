#!/usr/bin/env python3
"""Plan, apply, or inventory the dedicated RunsOn stack with administrative access."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import quote

from example import ROOT, require, timestamp, utcnow, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("plan", "apply", "inventory"))
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    config = json.loads((ROOT / "infra/runs-on/deployment.json").read_text())
    work = ROOT / ".work/runs-on"
    private = ROOT / ".aws-local"
    redactions = []

    def aws(service, operation, payload=None):
        command = ["aws", "--profile", args.profile, "--region", config["region"],
                   service, operation, "--output", "json", "--no-cli-pager"]
        if service == "s3api" and operation == "put-object":
            payload = dict(payload)
            command += ["--body", payload.pop("Body")]
        # CLI request files can contain the license. Keep them out of shell
        # arguments, normal work files, Terraform state, and error messages.
        with tempfile.TemporaryDirectory(prefix="cfn-request-", dir=private) as temporary:
            if payload is not None:
                request = Path(temporary) / "request.json"
                descriptor = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(payload, stream)
                command += ["--cli-input-json", "file://" + str(request)]
            completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completed.returncode:
            message = completed.stderr
            for secret in redactions:
                message = message.replace(secret, "[REDACTED]")
            raise RuntimeError(f"{service} {operation}: {message.strip()}")
        return json.loads(completed.stdout) if completed.stdout else {}

    identity = aws("sts", "get-caller-identity")
    require(identity["Account"] == config["account_id"], "administrative account differs")
    inventory_file = ROOT / "infra/resources.json"
    inventory = json.loads(inventory_file.read_text())
    state_file = work / "change-set.json"

    if args.task == "plan":
        template = work / "template.json"
        digest = hashlib.sha256(template.read_bytes()).hexdigest()
        require(digest == inventory["runs_on"]["template_sha256"], "reviewed template differs")
        license_path = ROOT / config["license_file"]
        require(license_path.stat().st_mode & 0o777 == 0o600, "license file must have mode0600")
        license_value = license_path.read_text().strip()
        require(bool(license_value), "license file is empty")
        redactions.append(license_value)
        email = (ROOT / config["notification_email_file"]).read_text().strip()
        require("@" in email and "\n" not in email, "notification email is invalid")
        values = {name: ",".join(value) if isinstance(value, list) else str(value)
                  for name, value in config["parameters"].items()}
        values.update(LicenseKey=license_value, EmailAddress=email)
        key = config["template_prefix"] + digest + ".json"
        uploaded = aws("s3api", "put-object", {"Bucket": config["template_bucket"],
                       "Key": key, "Body": str(template), "ServerSideEncryption": "AES256",
                       "ContentType": "application/json", "ExpectedBucketOwner": config["account_id"]})
        version = uploaded.get("VersionId")
        require(bool(version) and version != "null", "template must have an immutable S3 version")
        url = (f"https://{config['template_bucket']}.s3.{config['region']}.amazonaws.com/"
               f"{key}?versionId={quote(version, safe='')}")
        existing = aws("cloudformation", "list-stacks", {"StackStatusFilter": [
            "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "REVIEW_IN_PROGRESS"]})
        matches = [item for item in existing["StackSummaries"] if item["StackName"] == config["stack_name"]]
        change_type = "UPDATE" if matches and matches[0]["StackStatus"] != "REVIEW_IN_PROGRESS" else "CREATE"
        result = aws("cloudformation", "create-change-set", {
            "StackName": config["stack_name"], "ChangeSetName": "review-" + digest[:12] + "-" + utcnow().strftime("%H%M%S"),
            "ChangeSetType": change_type, "TemplateURL": url,
            "Capabilities": ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()],
            "Tags": [{"Key": "ami-example:owner", "Value": inventory["repository"]},
                     {"Key": "ami-example:purpose", "Value": "runs-on-service"}]})
        record = {"stack_id": result["StackId"], "change_set_id": result["Id"],
                  "change_set_type": change_type, "template_sha256": digest,
                  "template_bucket": config["template_bucket"], "template_key": key,
                  "template_version_id": version, "prepared_at": timestamp()}
        write_json(state_file, record)
        print(json.dumps(record))
    elif args.task == "apply":
        record = json.loads(state_file.read_text())
        require(record["template_sha256"] == inventory["runs_on"]["template_sha256"], "reviewed change set differs")
        changes = aws("cloudformation", "describe-change-set", {"ChangeSetName": record["change_set_id"]})
        require(changes["Status"] == "CREATE_COMPLETE" and changes["ExecutionStatus"] == "AVAILABLE",
                "change set is not ready for execution")
        resources = [item["ResourceChange"] for item in changes.get("Changes", [])]
        if record["change_set_type"] == "CREATE":
            expected = {(item["logical_id"], item["type"]) for item in inventory["runs_on"]["resources"]}
            actual = {(item["LogicalResourceId"], item["ResourceType"]) for item in resources}
            require(actual == expected and all(item["Action"] == "Add" for item in resources),
                    "CloudFormation resources differ from the reviewed plan")
        write_json(work / "reviewed-changes.json", resources)
        try:
            linked = aws("iam", "get-role", {"RoleName": "AWSServiceRoleForECS"})["Role"]
        except RuntimeError as error:
            if "NoSuchEntity" not in str(error):
                raise
            linked = aws("iam", "create-service-linked-role", {"AWSServiceName": "ecs.amazonaws.com"})["Role"]
        # ECS can fail immediately after its automatic service-role creation.
        # Establish the role before CloudFormation starts dependent resources.
        created = dt.datetime.fromisoformat(linked["CreateDate"].replace("Z", "+00:00"))
        propagation = 15 - (utcnow() - created).total_seconds()
        if propagation > 0:
            time.sleep(propagation)
        inventory["runs_on"]["ecs_service_linked_role_arn"] = linked["Arn"]
        aws("cloudformation", "execute-change-set", {"ChangeSetName": record["change_set_id"]})
        inventory["runs_on"].update(status="creation_started", stack_id=record["stack_id"],
            created_at=timestamp(), review_or_teardown_due=timestamp(utcnow() + dt.timedelta(hours=config["trial_hours"])))
        write_json(inventory_file, inventory)
        print(json.dumps({"stack_id": record["stack_id"], "status": "execution_started",
                          "planned_resources": len(resources)}))
    else:
        stack = aws("cloudformation", "describe-stacks", {"StackName": config["stack_name"]})["Stacks"][0]
        resources = aws("cloudformation", "list-stack-resources", {"StackName": stack["StackId"]})["StackResourceSummaries"]
        observed = {item["LogicalResourceId"]: item for item in resources}
        for item in inventory["runs_on"]["resources"]:
            actual = observed.get(item["logical_id"], {})
            item.update(physical_id=actual.get("PhysicalResourceId"), status=actual.get("ResourceStatus", "not_created"))
        outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
        inventory["runs_on"].update(status=stack["StackStatus"], stack_id=stack["StackId"], outputs=outputs)
        inventory["runs_on"]["service_role_arn"] = outputs.get("RunsOnServiceRoleArn")
        role = outputs.get("RunsOnInstanceRoleName")
        if role:
            inventory["runs_on"]["runner_role_arn"] = aws("iam", "get-role", {"RoleName": role})["Role"]["Arn"]
        profile = observed.get("EC2InstanceProfile", {}).get("PhysicalResourceId")
        profile_status = observed.get("EC2InstanceProfile", {}).get("ResourceStatus", "")
        if profile and profile_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"):
            inventory["runs_on"]["runner_profile_arn"] = aws("iam", "get-instance-profile", {"InstanceProfileName": profile})["InstanceProfile"]["Arn"]
        inventory["updated_at"] = timestamp()
        write_json(inventory_file, inventory)
        write_json(work / "stack-resources.json", resources)
        print(json.dumps({"stack_status": stack["StackStatus"], "observed_resources": len(resources),
                          "inventory": str(inventory_file)}))


if __name__ == "__main__":
    main()
