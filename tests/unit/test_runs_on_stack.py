import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from support import ROOT, module
from example import InvalidInput, digest, read_json, write_json
from runs_on import ReviewedChangeSet, StackAws, StackConfig, public_change_set, terraform_outputs


def config(directory, account="111111111111", region="us-west-2", repository="first/project"):
    return StackConfig(account, region, repository, "example-stack", "example", "ami-" + "1" * 17,
                       "ami-" + "2" * 17, "t3.small", 80, False, "10.70.0.0/16", {},
                       directory / "license", directory / "email", "a" * 64)


def foundation(value):
    arn = f"arn:aws:iam::{value.account_id}:role/"
    return {"account_id": value.account_id, "region": value.region, "repository": value.repository,
            "controller_role_arn": arn + "custom/controller",
            "management_role_arns": {"builder": arn + "builder", "probe": arn + "probe"},
            "ebs_key_arn": f"arn:aws:kms:{value.region}:{value.account_id}:key/example-key",
            "artifact_bucket": f"example-{value.account_id}-{value.region}"}


def vendor_template():
    defaults = read_json(ROOT / "infra/runs-on/defaults.json")
    return {"Parameters": {name: {"Type": "String"} for name in
                            [*defaults, "GithubOrganization", "Environment", "VpcCidrBlock"]},
            "Resources": {
                "EC2FleetLaunchTemplateLinuxDefault": {"Properties": {"LaunchTemplateData": {
                    "TagSpecifications": [{"Tags": []}]}}},
                "RunsOnServiceRole": {"Properties": {"Policies": [{"PolicyDocument": {}}]}},
                "EC2InstanceRole": {"Properties": {"Policies": []}},
                "StackConfigMaterializer": {"Properties": {"StackConfigBase": {"EbsEncryptionKey": "old"}}}}}


class FakeStackAws:
    def __init__(self, value, template):
        self.config = value
        self.template = template
        self.calls = []
        self.redactions = []
        self.account = value.account_id
        self.stack_id = f"arn:aws:cloudformation:{value.region}:{value.account_id}:stack/{value.stack_name}/123"
        self.change_set_id = f"arn:aws:cloudformation:{value.region}:{value.account_id}:changeSet/review/456"
        self.change = None

    def identify(self):
        if self.account != self.config.account_id:
            raise InvalidInput("AWS identity belongs to another account")

    def call(self, service, operation, payload=None):
        self.calls.append((service, operation, copy.deepcopy(payload)))
        if operation == "get-role":
            return {"Role": {"Arn": f"arn:aws:iam::{self.account}:role/" + payload["RoleName"]}}
        if operation == "put-object":
            return {"VersionId": "version/with+symbols="}
        if operation == "list-stacks":
            return {"StackSummaries": []}
        if operation == "create-change-set":
            self.change = {"StackId": self.stack_id, "ChangeSetId": self.change_set_id,
                           "Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE",
                           "Description": payload["Description"], "Parameters": payload["Parameters"],
                           "Capabilities": payload["Capabilities"],
                           "Changes": [{"Type": "Resource", "ResourceChange": {
                               "LogicalResourceId": "VendorGeneratedResource", "ResourceType": "AWS::S3::Bucket",
                               "Action": "Add"}}]}
            return {"StackId": self.stack_id, "Id": self.change_set_id}
        if operation == "get-template":
            return {"TemplateBody": json.dumps(self.template)}
        if operation == "execute-change-set":
            return {}
        raise AssertionError((service, operation, payload))

    def change_set(self, arn):
        self.calls.append(("cloudformation", "describe-change-set", {"ChangeSetName": arn}))
        return copy.deepcopy(self.change)


class InventoryAws(FakeStackAws):
    def call(self, service, operation, payload=None):
        value = self.config
        if operation == "describe-stacks":
            outputs = {"RunsOnAwsAccountId": value.account_id, "RunsOnRegion": value.region,
                       "RunsOnAppTag": "v3.3.1", "RunsOnLaunchTemplateLinuxDefault": "lt-example:7",
                       "RunsOnInstanceRoleName": "runner", "RunsOnServiceRoleArn": "service-role"}
            return {"Stacks": [{"StackId": self.stack_id, "StackStatus": "CREATE_COMPLETE",
                                "Outputs": [{"OutputKey": key, "OutputValue": item} for key, item in outputs.items()],
                                "Parameters": [{"ParameterKey": "GithubOrganization", "ParameterValue": value.repository.split("/")[0]},
                                               {"ParameterKey": "Environment", "ParameterValue": value.environment},
                                               {"ParameterKey": "EmailAddress", "ParameterValue": "private@example.invalid"}]}]}
        if operation == "list-stack-resources":
            return {"StackResourceSummaries": [{"LogicalResourceId": "CurrentVendorResource",
                                                "PhysicalResourceId": "current-resource", "ResourceStatus": "CREATE_COMPLETE"}]}
        if operation == "get-template":
            return {"TemplateBody": {"Mappings": {"App": {"Tags": {"BootstrapTag": "v0.1.12"}}}}}
        if operation == "describe-launch-template-versions":
            return {"LaunchTemplateVersions": [{"LaunchTemplateData": {"TagSpecifications": [{
                "ResourceType": "instance", "Tags": [{"Key": "ami-example:runs-on-repository", "Value": value.repository}]}]}}]}
        return super().call(service, operation, payload)


class RunsOnStack(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.prepare = module("prepare-runs-on")
        self.stack = module("runs-on-stack")
        self.config = config(self.directory)
        self.config.license_file.write_text("private-license")
        self.config.license_file.chmod(0o600)
        self.config.notification_email_file.write_text("operator@example.invalid")

    def prepared(self, value=None):
        value = value or self.config
        source = json.dumps(vendor_template()).encode()
        lock = {"sha256": hashlib.sha256(source).hexdigest()}
        output = self.directory / value.account_id
        self.prepare.prepare(value, foundation(value), source, lock, ROOT / "infra/runs-on", output)
        return output / "prepared.json"

    def reviewed(self):
        path = self.prepared()
        aws = FakeStackAws(self.config, read_json(path.parent / "template.json"))
        record = self.stack.plan(self.config, path, aws)
        record_path = self.directory / "change-set.json"
        write_json(record_path, record)
        return ReviewedChangeSet.read(record_path), aws, record

    def test_two_deployments_bind_their_own_roles_images_and_policy_limits(self):
        second = dataclasses.replace(config(self.directory, "222222222222", "eu-west-1", "second/image"),
                                     instance_type="m7i.large", root_volume_gib=96)
        for value in (self.config, second):
            with self.subTest(account=value.account_id):
                record = read_json(self.prepared(value))
                parameters = record["parameters"]
                self.assertEqual(parameters["DeploymentRepository"], value.repository)
                self.assertEqual(parameters["DeploymentManagementRoleArns"], [
                    f"arn:aws:iam::{value.account_id}:role/custom/controller",
                    f"arn:aws:iam::{value.account_id}:role/builder",
                    f"arn:aws:iam::{value.account_id}:role/probe"])
                self.assertEqual(parameters["DeploymentInstanceType"], value.instance_type)
                self.assertEqual(parameters["DeploymentRootVolumeGiB"], value.root_volume_gib)
                self.assertEqual(parameters["DeploymentSourceAmiId"], value.source_ami_id)
                self.assertEqual(parameters["DeploymentControllerAmiId"], value.controller_ami_id)
                template = read_json(self.directory / value.account_id / "template.json")
                launch_data = template["Resources"]["EC2FleetLaunchTemplateLinuxDefault"]["Properties"]["LaunchTemplateData"]
                self.assertEqual("CreditSpecification" in launch_data, value.instance_type == "t3.small")
                policy = template["Resources"]["RunsOnServiceRole"]["Properties"]["Policies"][0]["PolicyDocument"]
                statements = {item.get("Sid"): item for item in policy["Statement"]}
                self.assertEqual(statements["OwnedRepositoryImages"]["Condition"]["StringEquals"], {
                    "ec2:Owner": {"Ref": "AWS::AccountId"},
                    "ec2:ResourceTag/ami-example:owner": {"Ref": "DeploymentRepository"}})
                launch = statements["TaggedRunnerInstances"]["Condition"]["StringEquals"]
                self.assertEqual(launch["ec2:InstanceMarketType"], "on-demand")
                self.assertEqual(launch["ec2:MetadataHttpTokens"], "required")
                self.assertEqual(launch["ec2:InstanceType"], {"Ref": "DeploymentInstanceType"})
                self.assertEqual(statements["DenyOtherEbsEncryptionKeys"]["NotResource"],
                                 {"Ref": "DeploymentEbsKeyArn"})
                runner_policy = template["Resources"]["EC2InstanceRole"]["Properties"]["Policies"][0]["PolicyDocument"]
                runner_statements = {item["Sid"]: item for item in runner_policy["Statement"]}
                self.assertEqual(runner_statements["NoPublisherAssumption"]["Resource"],
                                 {"Ref": "DeploymentManagementRoleArns"})
                self.assertEqual(runner_statements["NoImagePublishing"]["Effect"], "Deny")
                self.assertIn("ec2:CreateImage", runner_statements["NoImagePublishing"]["Action"])

    def test_foundation_from_another_deployment_is_rejected(self):
        source = json.dumps(vendor_template()).encode()
        bindings = foundation(self.config)
        bindings["repository"] = "unrelated/repo"
        with self.assertRaisesRegex(InvalidInput, "foundation repository"):
            self.prepare.prepare(self.config, bindings, source, {"sha256": hashlib.sha256(source).hexdigest()},
                                 ROOT / "infra/runs-on", self.directory / "output")
        self.assertFalse((self.directory / "output").exists())

    def test_plan_records_cloudformation_evaluated_changes_without_secrets(self):
        _, aws, record = self.reviewed()
        self.assertEqual(record["evaluated"]["changes"][0]["ResourceChange"]["LogicalResourceId"],
                         "VendorGeneratedResource")
        self.assertNotIn("private-license", json.dumps(record))
        self.assertNotIn("operator@example.invalid", json.dumps(record))
        operations = [operation for _, operation, _ in aws.calls]
        self.assertNotIn("execute-change-set", operations)
        self.assertNotIn("create-service-linked-role", operations)
        request = next(payload for _, operation, payload in aws.calls if operation == "create-change-set")
        self.assertIn("versionId=version%2Fwith%2Bsymbols%3D", request["TemplateURL"])

    def test_apply_executes_exact_record_without_configuration_or_prepared_files(self):
        record, aws, _ = self.reviewed()
        (self.directory / self.config.account_id / "prepared.json").unlink()
        result = self.stack.apply(record, aws)
        self.assertEqual(result["status"], "execution_started")
        self.assertEqual(aws.calls[-1], ("cloudformation", "execute-change-set", {
            "StackName": record.stack_id, "ChangeSetName": record.change_set_id}))

    def test_apply_rejects_changed_resources_template_or_identity(self):
        for mutation in ("resources", "template", "identity"):
            with self.subTest(mutation=mutation):
                record, aws, _ = self.reviewed()
                if mutation == "resources":
                    aws.change["Changes"][0]["ResourceChange"]["Action"] = "Remove"
                elif mutation == "template":
                    aws.template["Resources"] = {}
                else:
                    aws.account = "999999999999"
                with self.assertRaises(InvalidInput):
                    self.stack.apply(record, aws)
                self.assertNotIn("execute-change-set", [operation for _, operation, _ in aws.calls])

    def test_record_rejects_arn_in_another_region(self):
        _, _, record = self.reviewed()
        record["change_set_id"] = record["change_set_id"].replace("us-west-2", "eu-west-1")
        path = self.directory / "invalid.json"
        write_json(path, record)
        with self.assertRaisesRegex(InvalidInput, "change-set ARN"):
            ReviewedChangeSet.read(path)

    def test_plan_rejects_modified_prepared_template_before_any_cloud_calls(self):
        path = self.prepared()
        (path.parent / "template.json").write_text("{}")
        aws = FakeStackAws(self.config, {})
        with self.assertRaisesRegex(InvalidInput, "template bytes"):
            self.stack.plan(self.config, path, aws)
        self.assertEqual(aws.calls, [])

    def test_standard_terraform_output_is_unwrapped(self):
        path = self.directory / "foundation.json"
        write_json(path, {key: {"value": value, "type": "string", "sensitive": False}
                          for key, value in foundation(self.config).items()})
        self.assertEqual(terraform_outputs(path), foundation(self.config))

    def test_inventory_uses_fresh_resources_and_installed_service_versions(self):
        aws = InventoryAws(self.config, {})
        observed = self.stack.inventory(self.config, aws)
        self.assertEqual(observed["runs_on"], {"environment": "example", "version": "3.3.1", "bootstrap_version": "0.1.12"})
        self.assertEqual(observed["resources"][0]["PhysicalResourceId"], "current-resource")
        self.assertNotIn("private@example.invalid", json.dumps(observed))

    def test_inventory_rejects_another_repository_marker(self):
        aws = InventoryAws(self.config, {})
        original = aws.call
        def call(service, operation, payload=None):
            response = original(service, operation, payload)
            if operation == "describe-launch-template-versions":
                response["LaunchTemplateVersions"][0]["LaunchTemplateData"]["TagSpecifications"][0]["Tags"][0]["Value"] = "another/repo"
            return response
        aws.call = call
        with self.assertRaisesRegex(InvalidInput, "ownership marker"):
            self.stack.inventory(self.config, aws)

    def test_config_paths_are_relative_to_spec_and_privacy_is_independent(self):
        value = read_json(ROOT / "examples/deployment.spec.json")
        value["private"] = True
        value["runs_on"]["private"] = False
        path = self.directory / "configuration" / "operator.json"
        write_json(path, value)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.directory)
        loaded = StackConfig.read(path)
        self.assertEqual(loaded.license_file, path.parent / value["runs_on"]["license_file"])
        self.assertFalse(loaded.private)
        self.assertEqual(loaded.parameters({})["Private"], "false")

    def test_config_rejects_raw_policy_override(self):
        value = read_json(ROOT / "examples/deployment.spec.json")
        value["runs_on"]["parameters"] = {"RunnerCustomPolicy": "arn:aws:iam::aws:policy/AdministratorAccess"}
        path = self.directory / "operator.json"
        write_json(path, value)
        with self.assertRaisesRegex(InvalidInput, "documented service settings"):
            StackConfig.read(path)

    def test_config_rejects_unsupported_private_service_before_aws_is_created(self):
        value = read_json(ROOT / "examples/deployment.spec.json")
        value["runs_on"]["private"] = True
        path = self.directory / "operator.json"
        write_json(path, value)
        with patch.object(self.stack, "StackAws") as aws:
            with patch("sys.argv", ["runs-on-stack.py", "inventory", "--config", str(path),
                                    "--output", str(self.directory / "inventory.json")]):
                with self.assertRaisesRegex(InvalidInput, "public service networking"):
                    self.stack.main()
        aws.assert_not_called()

    def test_aws_request_file_is_private_temporary_and_secret_not_in_arguments(self):
        aws = StackAws(self.config.account_id, self.config.region)
        observed = []
        def run(command, **kwargs):
            self.assertNotIn("private-license", " ".join(command))
            path = Path(command[command.index("--cli-input-json") + 1][7:])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(read_json(path), {"LicenseKey": "private-license"})
            observed.append(path)
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")
        with patch("runs_on.subprocess.run", side_effect=run):
            aws.call("cloudformation", "create-change-set", {"LicenseKey": "private-license"})
        self.assertFalse(observed[0].exists())

    def test_paginated_change_set_contains_all_evaluated_resources(self):
        aws = StackAws(self.config.account_id, self.config.region)
        pages = [{"StackId": "stack", "ChangeSetId": "changes", "Changes": ["first"], "NextToken": "next"},
                 {"Changes": ["second"]}]
        with patch.object(aws, "call", side_effect=pages) as call:
            response = aws.change_set("selected-arn")
        self.assertEqual(response["Changes"], ["first", "second"])
        self.assertEqual(call.call_args.args[-1], {"ChangeSetName": "selected-arn", "NextToken": "next"})


if __name__ == "__main__":
    unittest.main()
