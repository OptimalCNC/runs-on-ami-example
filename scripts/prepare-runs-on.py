#!/usr/bin/env python3
"""Prepare the pinned RunsOn template and a non-secret resource plan; never deploy."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import urllib.request

import yaml

from example import ROOT, require, write_json


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


def resource_plan(template, config):
    parameters = {name: value.get("Default") for name, value in template["Parameters"].items()}
    parameters.update(config["parameters"])
    parameters.update({"AWS::Region": config["region"], "AWS::AccountId": config["account_id"],
                       "AWS::Partition": "aws", "AWS::StackName": config["stack_name"]})

    def evaluate(value):
        if isinstance(value, list):
            return [evaluate(item) for item in value]
        if not isinstance(value, dict):
            return value
        require(len(value) == 1, "unexpected CloudFormation condition expression")
        operation, item = next(iter(value.items()))
        if operation == "Ref":
            return parameters[item]
        if operation == "Condition":
            return evaluate(template["Conditions"][item])
        if operation == "Fn::Equals":
            left, right = evaluate(item)
            return left == right
        if operation == "Fn::Not":
            return not evaluate(item)[0]
        if operation == "Fn::And":
            return all(evaluate(item))
        if operation == "Fn::Or":
            return any(evaluate(item))
        if operation == "Fn::FindInMap":
            first, second, third = evaluate(item)
            return template["Mappings"][first][second][third]
        raise ValueError(f"unsupported CloudFormation condition: {operation}")

    resources = [{"logical_id": name, "type": value["Type"], "physical_id": None,
                  "deletion_policy": value.get("DeletionPolicy", "Delete"),
                  "log_retention_days": value.get("Properties", {}).get("RetentionInDays")}
                 for name, value in template["Resources"].items()
                 if not value.get("Condition") or evaluate({"Condition": value["Condition"]})]
    return {"account_id": config["account_id"], "region": config["region"],
            "stack_name": config["stack_name"], "resource_count": len(resources),
            "resource_types": dict(sorted(Counter(r["type"] for r in resources).items())),
            "resources": resources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-template", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".work/runs-on")
    args = parser.parse_args()
    directory = ROOT / "infra/runs-on"
    lock = json.loads((directory / "template-lock.json").read_text())
    config = json.loads((directory / "deployment.json").read_text())
    if args.vendor_template:
        source = args.vendor_template.read_bytes()
    else:
        with urllib.request.urlopen(lock["url"], timeout=60) as response:
            source = response.read()
    require(hashlib.sha256(source).hexdigest() == lock["sha256"], "vendor template digest differs")
    template = yaml.load(source, Loader=CloudFormationLoader)
    patch(template, json.loads((directory / "policy-overlay.json").read_text()))
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "template.json"
    write_json(target, template)
    plan = resource_plan(template, config)
    plan["template_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    write_json(args.output / "resource-plan.json", plan)
    print(json.dumps({"template": str(target), "resource_plan": str(args.output / "resource-plan.json"),
                      "resource_count": plan["resource_count"], "cloud_changes": 0}))


if __name__ == "__main__":
    main()
