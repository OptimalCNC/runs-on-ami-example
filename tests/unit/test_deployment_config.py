import dataclasses
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from support import ROOT, deployment, deployment_dict, parent_inventory, source_inventory
from example import (BuildInputs, CleanupContext, InfrastructureBindings, InvalidInput,
                     file_sha, load_bindings, load_cleanup_context, load_deployment, recipe, write_json)
from deployment_config import (OperatorSpec, assemble_bindings, assemble_manifest,
                               bootstrap_values, foundation_inputs, load_spec, management_inputs)


class DeploymentBoundaryTests(unittest.TestCase):
    def test_manifest_is_independent_of_repository_and_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            snapshot = deployment().snapshot(directory / "outside-checkout")
            with patch("pathlib.Path.cwd", return_value=Path("/unrelated")):
                loaded = load_deployment(snapshot)
            self.assertEqual(loaded.source_ami.inventory, parent_inventory())
            self.assertEqual(loaded.source_ami.inventory_path, snapshot.parent / "parents/source.json")
            self.assertEqual(load_cleanup_context(snapshot), deployment().cleanup())
            self.assertEqual(load_bindings(snapshot).instance_type, "t3.small")

    def test_recipe_changes_for_parent_bytes_and_not_evidence_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "recipe.sh").write_text("build payload\n")
            lock = {"recipe_files": ["recipe.sh"]}
            first = load_deployment(deployment().snapshot(directory / "first"))
            second = load_deployment(first.snapshot(directory / "moved"))
            identity = recipe(lock, first, directory)[0]
            self.assertEqual(identity, recipe(lock, second, directory)[0])
            other_owner = dataclasses.replace(second, repository="other/repository", account_id="234567890123",
                                               artifact_bucket="different-bucket", retain_hours=72)
            self.assertEqual(identity, recipe(lock, other_owner, directory)[0])
            changed_parent = dataclasses.replace(second.source_ami, inventory_sha256="2" * 64)
            self.assertNotEqual(identity, recipe(lock, dataclasses.replace(second, source_ami=changed_parent), directory)[0])
            changed_ami = dataclasses.replace(second.source_ami, id="ami-22222222222222222")
            self.assertNotEqual(identity, recipe(lock, dataclasses.replace(second, source_ami=changed_ami), directory)[0])

    def test_complete_manifest_requires_verified_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = deployment().snapshot(Path(temporary) / "runtime")
            source = manifest.parent / "parents/source.json"
            source.write_text(source.read_text() + " ")
            with self.assertRaisesRegex(InvalidInput, "inventory hash differs"):
                load_deployment(manifest)
            self.assertEqual(load_cleanup_context(manifest), deployment().cleanup())
            self.assertEqual(load_bindings(manifest).probe_profile_name, "example-probe")

    def test_plain_source_is_accepted_but_controller_requires_runson(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "source.json"
            write_json(path, source_inventory())
            value = deployment_dict()
            value["root_volume_gib"] = 16
            value["source_ami"].update(inventory_file=str(path), inventory_sha256=file_sha(path))
            parsed = BuildInputs.parse(value)
            self.assertIsNone(parsed.source_ami.inventory["runner_version"])
            self.assertEqual(parsed.controller_ami.inventory, parent_inventory())
            self.assertEqual((parsed.root_volume_gib, parsed.parent_root_volume_gib), (16, 30))
            value["controller_ami"] = dict(value["source_ami"])
            with self.assertRaisesRegex(InvalidInput, "controller parent must contain the GitHub Actions runner"):
                BuildInputs.parse(value)

    def test_plain_source_still_requires_clean_ubuntu_2404_and_consistent_runner_evidence(self):
        for change, error in (({"os_version": "22.04"}, "Ubuntu 24.04"),
                              ({"registered": True}, "not clean"),
                              ({"workspaces": ["/home/runner/_work/repo"]}, "not clean"),
                              ({"secure_boot": True}, "Secure Boot"),
                              ({"runner_listener_sha256": "1" * 64}, "absent runner")):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "source.json"
                write_json(path, {**source_inventory(), **change})
                value = deployment_dict()
                value["source_ami"].update(inventory_file=str(path), inventory_sha256=file_sha(path))
                with self.assertRaisesRegex(InvalidInput, error):
                    BuildInputs.parse(value)

    def test_snapshot_detects_evidence_changed_after_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            loaded = load_deployment(deployment().snapshot(directory / "original"))
            loaded.source_ami.inventory_path.write_text("{}")
            with self.assertRaisesRegex(InvalidInput, "changed after loading"):
                loaded.snapshot(directory / "snapshot")

    def test_serialized_parent_has_only_evidence_contract(self):
        parent = deployment().source_ami
        self.assertEqual(set(dataclasses.asdict(parent)),
                         {"id", "owner", "architecture", "boot_mode", "inventory_file", "inventory_sha256"})
        self.assertNotIn("inventory_file", parent.semantic_identity("us-east-1"))

    def test_bindings_exist_without_parent_evidence(self):
        values = deployment_dict()
        for key in ("source_ami", "controller_ami"):
            del values[key]
        parsed = InfrastructureBindings.parse(values)
        self.assertEqual(parsed.cleanup(), CleanupContext("123456789012", "us-east-1", "example/repo", "example-artifacts"))
        with self.assertRaises(InvalidInput):
            BuildInputs.parse(values)

    def test_manifest_assembly_uses_each_parent_records_own_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = deployment()
            bindings = dataclasses.asdict(load_bindings(original.snapshot(root / "base")))
            bindings["parent_selections"] = {role: {"id": original.source_ami.id, "owner": original.source_ami.owner}
                                               for role in ("source", "controller")}
            write_json(root / "bindings.json", bindings)
            records = []
            for name, inventory in (("first", source_inventory()), ("second", parent_inventory())):
                directory = root / name
                directory.mkdir()
                write_json(directory / "inventory.json", inventory)
                write_json(directory / "parent.json", {**original.source_ami.record(), "inventory_file": "inventory.json",
                                                       "inventory_sha256": file_sha(directory / "inventory.json")})
                records.append(directory / "parent.json")
            result = assemble_manifest(root / "bindings.json", *records, root / "assembled")
            self.assertEqual(load_deployment(result).source_ami.inventory, source_inventory())
            captured = json.loads(records[0].read_text())
            captured["id"] = "ami-22222222222222222"
            write_json(records[0], captured)
            with self.assertRaisesRegex(InvalidInput, "captured source parent differs"):
                assemble_manifest(root / "bindings.json", *records, root / "wrong-manifest.json")
            self.assertFalse((root / "wrong-manifest.json").exists())


class OperatorAssemblyTests(unittest.TestCase):
    def make_spec(self, account="123456789012", repository="example/repo", region="us-east-1"):
        return OperatorSpec.parse({"schema_version": 1, "account_id": account, "region": region, "repository": repository,
                                   "source_ami": {"id": "ami-0123456789abcdef0", "owner": "345678901234"},
                                   "controller_ami": {"id": "ami-0123456789abcdef0", "owner": "345678901234"},
                                   "runs_on": {"stack_name": "example-runs-on", "environment": "ami-example"}})

    def fixtures(self, spec):
        scope = spec.scope()
        foundation = {**scope, "controller_role_arn": f"arn:aws:iam::{scope['account_id']}:role/controller",
                      "builder_profile_name": "builder", "probe_profile_name": "probe", "artifact_bucket": "example-artifacts",
                      "environment": "ami-build", "name_prefix": "ami-example", "controller_role_managed": True,
                      "management_role_arns": {name: f"arn:aws:iam::{scope['account_id']}:role/{name}" for name in ("builder", "probe")},
                      "management_profile_arns": {name: f"arn:aws:iam::{scope['account_id']}:instance-profile/{name}" for name in ("builder", "probe")},
                      "oidc_provider_arn": f"arn:aws:iam::{scope['account_id']}:oidc-provider/token.actions.githubusercontent.com",
                      "ebs_key_arn": f"arn:aws:kms:{scope['region']}:{scope['account_id']}:key/12345678-1234-1234-1234-123456789012"}
        management = {**scope, "security_group_id": "sg-0123456789abcdef0", "vpc_id": "vpc-0123456789abcdef0",
                      "subnet_id": "subnet-0123456789abcdef0", "source_ami_id": "ami-0123456789abcdef0", "controller_ami_id": "ami-0123456789abcdef0"}
        parent = {"id": "ami-0123456789abcdef0", "owner": "345678901234", "architecture": "x86_64", "boot_mode": "uefi", "root_volume_gib": 30}
        observed = {"schema_version": 1, **scope, "network": {"subnet_id": "subnet-0123456789abcdef0", "vpc_id": "vpc-0123456789abcdef0", "vpc_cidr": "10.60.0.0/16"},
                    "instance_type": {"name": "t3.small", "vcpus": 2}, "parents": {"source": {**parent, "root_volume_gib": 8}, "controller": dict(parent)}}
        management.update(observed["network"])
        management.update({key: spec.values[key] for key in ("instance_type", "builder_instance_type", "root_volume_gib", "parent_root_volume_gib")})
        management["foundation"] = dict(foundation)
        service = {**scope, "stack_name": "example-runs-on", "runs_on": {"environment": "ami-example", "version": "3.3.1", "bootstrap_version": "0.1.12"}}
        return foundation, management, observed, service

    def test_two_operator_specs_assemble_without_source_edits(self):
        for account, repository, region in (("123456789012", "first/repo", "us-east-1"), ("234567890123", "second/repo", "eu-west-1")):
            with self.subTest(repository=repository):
                spec = self.make_spec(account, repository, region)
                foundation, management, observed, service = self.fixtures(spec)
                terraform = management_inputs(spec, foundation, observed)
                bindings = assemble_bindings(spec, {key: {"value": value} for key, value in foundation.items()}, management, observed, service)
                self.assertEqual(bindings.account_id, account)
                self.assertEqual(bindings.repository, repository)
                self.assertEqual(bindings.region, region)
                self.assertEqual(bindings.controller_role_arn, terraform["foundation"]["controller_role_arn"])
                self.assertEqual(bindings.subnet_id, terraform["subnet_id"])

    def test_mixed_infrastructure_outputs_are_rejected(self):
        spec = self.make_spec()
        for index in range(4):
            values = list(self.fixtures(spec))
            values[index]["account_id"] = "999999999999"
            with self.subTest(index=index), self.assertRaises(InvalidInput):
                assemble_bindings(spec, *values)

    def test_observed_parent_must_be_the_selected_publisher_and_image(self):
        spec = self.make_spec()
        values = list(self.fixtures(spec))
        values[2]["parents"]["source"]["owner"] = "999999999999"
        with self.assertRaisesRegex(InvalidInput, "parent selection differs"):
            assemble_bindings(spec, *values)

    def test_source_volume_must_fit_candidate_independently_of_controller_probe_volume(self):
        spec = self.make_spec()
        self.assertEqual((spec.values["root_volume_gib"], spec.values["parent_root_volume_gib"]), (16, 30))
        values = list(self.fixtures(spec))
        self.assertEqual(assemble_bindings(spec, *values).root_volume_gib, 16)
        values[2]["parents"]["source"]["root_volume_gib"] = 17
        with self.assertRaisesRegex(InvalidInput, "source parent root exceeds candidate"):
            assemble_bindings(spec, *values)
        values[2]["parents"]["source"]["root_volume_gib"] = 8
        values[2]["parents"]["controller"]["root_volume_gib"] = 31
        with self.assertRaisesRegex(InvalidInput, "controller parent root exceeds"):
            assemble_bindings(spec, *values)

    def test_foundation_environment_must_match_requested_oidc_environment(self):
        spec = self.make_spec()
        values = list(self.fixtures(spec))
        values[0]["environment"] = "different-environment"
        with self.assertRaisesRegex(InvalidInput, "foundation environment"):
            assemble_bindings(spec, *values)

    def test_management_policy_must_use_selected_foundation_roles(self):
        spec = self.make_spec()
        values = list(self.fixtures(spec))
        values[1]["foundation"]["controller_role_arn"] = "arn:aws:iam::123456789012:role/different"
        with self.assertRaisesRegex(InvalidInput, "foundation bindings differ"):
            assemble_bindings(spec, *values)

    def test_observed_network_must_match_provisioned_policy(self):
        spec = self.make_spec()
        values = list(self.fixtures(spec))
        values[1]["subnet_id"] = "subnet-99999999999999999"
        with self.assertRaisesRegex(InvalidInput, "management policy image or network"):
            assemble_bindings(spec, *values)

    def test_foundation_requires_no_network_or_inventory(self):
        values = foundation_inputs(self.make_spec())
        self.assertEqual(values["environment"], "ami-build")
        self.assertNotIn("subnet_id", values)
        self.assertNotIn("source_ami", values)

    def test_bootstrap_values_use_the_resolved_resources(self):
        values = bootstrap_values(deployment())
        self.assertEqual(values["repository_variables"], {"AMI_DEPLOYMENT_ENVIRONMENT": "ami-build"})
        self.assertEqual(values["environment_variables"]["AMI_STATE_URI"], "s3://example-artifacts/example/repo/state")
        self.assertEqual(values["cleanup_context"], dataclasses.asdict(deployment().cleanup()))

    def test_operator_references_are_relative_to_spec(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "operator/spec.json"
            write_json(path, self.make_spec().values)
            self.assertEqual(load_spec(path).directory, path.parent)

    def test_neutral_example_and_cli_generate_selection_without_aws(self):
        spec = load_spec(ROOT / "examples/deployment.spec.json")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "source.json"
            subprocess.run([sys.executable, str(ROOT / "scripts/deployment-config.py"), "selection", "--spec",
                            str(ROOT / "examples/deployment.spec.json"), "--parent", "source", "--output", str(output)],
                           check=True, cwd=temporary)
            self.assertEqual(json.loads(output.read_text()), spec.values["source_ami"])


if __name__ == "__main__":
    unittest.main()
