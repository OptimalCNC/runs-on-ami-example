#!/usr/bin/env python3
"""Render the pinned RunsOn template from explicit deployment inputs; never deploy."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

from example import ROOT, digest, file_sha, match, read_json, require, write_json
from runs_on import StackConfig, parse_template, terraform_outputs


def patch(template, operations):
    for operation in operations:
        parts = operation["path"].lstrip("/").split("/")
        parent = template
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = parts[-1]
        if operation["op"] == "add" and isinstance(parent, list) and key == "-":
            parent.append(operation["value"])
        elif operation["op"] == "replace":
            if isinstance(parent, list):
                parent[int(key)] = operation["value"]
            else:
                require(key in parent, f"missing template patch target: {operation['path']}")
                parent[key] = operation["value"]
        else:
            require(operation["op"] == "add" and isinstance(parent, dict) and key not in parent,
                    f"unsupported or conflicting template patch: {operation['path']}")
            parent[key] = operation["value"]


PARAMETERS = {
    "DeploymentRepository": "String",
    "DeploymentSourceAmiId": "AWS::EC2::Image::Id",
    "DeploymentControllerAmiId": "AWS::EC2::Image::Id",
    "DeploymentInstanceType": "String",
    "DeploymentRootVolumeGiB": "Number",
    "DeploymentEbsKeyArn": "String",
    "DeploymentManagementRoleArns": "CommaDelimitedList",
}


def prepare(config, foundation, source, lock, directory, output):
    for name in ("account_id", "region", "repository"):
        require(foundation[name] == getattr(config, name), f"foundation {name} differs from configuration")
    require(hashlib.sha256(source).hexdigest() == lock["sha256"], "vendor template digest differs")
    template = parse_template(source)
    require(isinstance(template, dict), "vendor template must be an object")
    for name, kind in PARAMETERS.items():
        require(name not in template["Parameters"], f"vendor parameter conflicts with {name}")
        template["Parameters"][name] = {"Type": kind}
    patch(template, read_json(directory / "policy-overlay.json"))
    if not config.instance_type.startswith(("t2.", "t3.", "t3a.")):
        template["Resources"]["EC2FleetLaunchTemplateLinuxDefault"]["Properties"]["LaunchTemplateData"].pop("CreditSpecification")
    values = config.parameters(read_json(directory / "defaults.json"))
    roles = [foundation["controller_role_arn"], *foundation["management_role_arns"].values()]
    for role in roles:
        match(role, rf"arn:aws:iam::{config.account_id}:role/[A-Za-z0-9+=,.@_/-]+", "management role ARN")
    match(foundation["ebs_key_arn"], rf"arn:aws:kms:{config.region}:{config.account_id}:key/[A-Za-z0-9-]+",
          "EBS key ARN")
    match(foundation["artifact_bucket"], r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", "artifact bucket")
    values.update(DeploymentRepository=config.repository,
                  DeploymentSourceAmiId=config.source_ami_id,
                  DeploymentControllerAmiId=config.controller_ami_id,
                  DeploymentInstanceType=config.instance_type,
                  DeploymentRootVolumeGiB=max(config.root_volume_gib, config.parent_root_volume_gib),
                  DeploymentEbsKeyArn=foundation["ebs_key_arn"],
                  DeploymentManagementRoleArns=roles)
    require(not (set(values) - set(template["Parameters"])), "unsupported vendor template parameter")
    target = output / "template.json"
    write_json(target, template)
    prepared = {"schema_version": 1, "kind": "runs-on-prepared-template",
                "account_id": config.account_id, "region": config.region,
                "repository": config.repository, "stack_name": config.stack_name,
                "config_sha256": config.sha256, "template_file": "template.json",
                "template_sha256": file_sha(target), "template_content_sha256": digest(template),
                "template_bucket": foundation["artifact_bucket"],
                "template_prefix": config.repository + "/infrastructure/runs-on/",
                "parameters": values, "vendor_template_sha256": lock["sha256"]}
    write_json(output / "prepared.json", prepared)
    return {"template": str(target), "prepared": str(output / "prepared.json"), "cloud_changes": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--vendor-template", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = ROOT / "infra/runs-on"
    lock = read_json(directory / "template-lock.json")
    config = StackConfig.read(args.config)
    if args.vendor_template:
        source = args.vendor_template.read_bytes()
    else:
        with urllib.request.urlopen(lock["url"], timeout=60) as response:
            source = response.read()
    print(json.dumps(prepare(config, terraform_outputs(args.foundation), source, lock,
                             directory, args.output)))


if __name__ == "__main__":
    main()
