import copy
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import ExecutionPlan
from fixtures import image_value, installation_value


class ExecutionContractTests(unittest.TestCase):
    def setUp(self):
        self.installation, self.image = installation_value(), image_value()

    def parse(self, **kwargs):
        return ExecutionPlan.parse(self.installation, self.image, **kwargs)

    def test_producer_shapes_become_frozen_execution_values(self):
        plan = self.parse(repository="exampleorg/image-repo")
        self.assertEqual(plan.installation.bootstrap_version, "0.1.12")
        self.assertEqual(plan.image.ami_id, "ami-0123456789abcdef0")
        self.assertEqual(plan.image.manifest_sha256, "d" * 64)
        self.assertEqual(plan.image.compatibility.minimum_root_volume_gib, 16)
        with self.assertRaises(FrozenInstanceError):
            plan.image.ami_id = "ami-fffffffffffffffff"
        with self.assertRaises(FrozenInstanceError):
            plan.image.compatibility.secure_boot = True

    def test_dispatch_roundtrip_preserves_plan_and_strips_private_data(self):
        for value in (self.installation, self.installation["runtime"], self.installation["versions"],
                      self.image, self.image["target"], self.image["artifact"], self.image["compatibility"]):
            value["private_extension"] = {"token": "never-send-this-token"}
        plan = self.parse()
        inputs = plan.as_inputs()
        self.assertEqual(set(inputs), {"installation", "published_image"})
        self.assertEqual(ExecutionPlan.from_inputs(inputs, repository="EXAMPLEORG/repo"), plan)
        self.assertNotIn("never-send-this-token", "".join(inputs.values()))
        self.assertNotIn("publisher_role_arn", "".join(inputs.values()))
        self.assertNotIn("setup_url", inputs["installation"])
        self.assertEqual(yaml.safe_load(inputs["published_image"])["artifact"]["manifest_sha256"], "d" * 64)
        self.installation["environment"] = "changed"
        self.assertEqual(plan.installation.environment, "production")

    def test_foreign_installation_or_internally_conflicting_target_is_rejected(self):
        for field, foreign in (("name", "another-platform"), ("account_id", "999999999999"), ("region", "eu-west-1")):
            for change_top_level in (False, True):
                image = copy.deepcopy(self.image)
                image["target"][field] = foreign
                if change_top_level and field != "name":
                    image[field] = foreign
                with self.subTest(field=field, top_level=change_top_level), self.assertRaises(ValueError):
                    ExecutionPlan.parse(self.installation, image)

    def test_exact_artifact_identity_and_availability_are_required(self):
        changes = [
            (("status",), "deleted"), (("status",), "checking-image"),
            (("kind",), "image-publication"), (("schema_version",), True),
            (("ami_id",), None), (("ami_id",), "ami-0123456789abcdef0/spot=true"),
            (("snapshot_id",), "snap-not-an-id"), (("publication_id",), "a" * 31),
            (("artifact", "sha256"), "b" * 63), (("artifact", "recipe_id"), "other-recipe"),
            (("artifact", "manifest_sha256"), None), (("artifact", "size_bytes"), True),
            (("artifact", "size_bytes"), 8 * 1024**3),
        ]
        for path, value in changes:
            image = copy.deepcopy(self.image)
            target = image
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                ExecutionPlan.parse(self.installation, image)

    def test_fixed_runner_shape_rejects_incompatible_images(self):
        changes = [
            ("architecture", "arm64"), ("boot_mode", "legacy-bios"),
            ("secure_boot", True), ("secure_boot", 0), ("ena_support", False), ("ena_support", 1),
            ("minimum_root_volume_gib", 17), ("minimum_root_volume_gib", 0),
            ("minimum_root_volume_gib", True), ("minimum_root_volume_gib", 16.0),
            ("runs_on_bootstrap_version", "0.1.13"),
        ]
        for field, value in changes:
            image = copy.deepcopy(self.image)
            image["compatibility"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                ExecutionPlan.parse(self.installation, image)

    def test_dispatch_labels_and_profile_cannot_escape_the_installation(self):
        changes = [
            (("schema_version",), True), (("account_id",), 123456789012),
            (("environment",), "production/ami=ami-fffffffffffffffff"),
            (("environment",), "production\nother=value"), (("region",), "us-east-1,spot=true"),
            (("name",), "../other"), (("github_organization",), "ExampleOrg/other"),
            (("versions", "bootstrap"), "v0.1.12/extra"),
            (("runtime", "runner_profile_arn"), "arn:aws:iam::999999999999:instance-profile/other"),
            (("runtime", "runner_max_runtime_minutes"), 14),
            (("runtime", "runner_max_runtime_minutes"), True),
            (("runtime", "runner_max_runtime_minutes"), 60.0),
        ]
        for path, value in changes:
            installation = copy.deepcopy(self.installation)
            target = installation
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                ExecutionPlan.parse(installation, self.image)

    def test_repository_must_belong_to_the_github_app_account(self):
        for repository in ("other-org/repo", "ExampleOrg", "ExampleOrg/repo/extra", "ExampleOrg/repo\n"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                self.parse(repository=repository)

    def test_yaml_input_errors_are_value_errors(self):
        inputs = self.parse().as_inputs()
        for key in inputs:
            for invalid in (None, 1, "", "[]", "null", "scalar", "bad: [", "---\na: 1\n---\nb: 2", "!!python/object:bad {}"):
                with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                    ExecutionPlan.from_inputs({**inputs, key: invalid})
        for invalid in (None, [], {}):
            with self.subTest(inputs=invalid), self.assertRaises(ValueError):
                ExecutionPlan.from_inputs(invalid)


if __name__ == "__main__":
    unittest.main()
