import copy
import functools
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from support import ROOT, FakeCloud, deployment_dict, module, result
import example
from example import InvalidInput, read_json, write_json


class BuildCloud(FakeCloud):
    def __init__(self, deployment):
        super().__init__()
        self.deployment = deployment
        self.images[0].update(State="available", BootMode="uefi", RootDeviceName="/dev/sda1")
        self.images[0]["BlockDeviceMappings"][0]["DeviceName"] = "/dev/sda1"
        self.retained_contents = {}

    def call(self, service, operation, payload=None):
        if operation == "create-key-pair":
            key = {"KeyName": payload["KeyName"], "KeyPairId": "key-0123456789abcdef0",
                   "KeyMaterial": "temporary private key"}
            self.keys.append(key)
            self.mutations.append((operation, payload))
            return key
        return super().call(service, operation, payload)

    def retain(self, path, key):
        self.retained_contents[key] = Path(path).read_bytes()
        return super().retain(path, key)


class ImageCommands(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "recipe-checkout"
        self.root.mkdir()
        self.inputs = self.directory / "external-inputs"
        self.inputs.mkdir()
        self.caller = self.directory / "caller"
        self.caller.mkdir()
        previous_cwd = Path.cwd()
        os.chdir(self.caller)
        self.addCleanup(os.chdir, previous_cwd)
        self.build = module("build-image")
        self.config = module("image-config")
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
        self.environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))
        self.enterContext(patch.object(example, "ROOT", self.root))
        self.enterContext(patch.object(self.build, "ROOT", self.root))
        self.enterContext(patch.object(self.build, "recipe", functools.partial(example.recipe, root=self.root)))

        inventory = copy.deepcopy(result()["payload"]["parent_inventory"])
        inventory["packages_sha256"] = hashlib.sha256(inventory["package_inventory"].encode()).hexdigest()
        deployment = deployment_dict()
        for name in ("source_ami", "controller_ami"):
            path = self.inputs / "inventories" / f"{name}.json"
            deployment[name]["inventory_file"] = str(path.relative_to(self.inputs))
            write_json(path, inventory)
            deployment[name]["inventory_sha256"] = example.file_sha(path)
        self.manifest = self.inputs / "manifest.json"
        write_json(self.manifest, deployment)
        self.deployment = example.load_deployment(self.manifest)
        self.lock = read_json(ROOT / "images/xenomai-cobalt/inputs.lock.json")
        self.lock["recipe_files"] = ["images/xenomai-cobalt/image.pkr.hcl"]
        self.template = self.root / self.lock["recipe_files"][0]
        self.template.parent.mkdir(parents=True, exist_ok=True)
        self.template.write_text("fixture recipe\n")
        write_json(self.root / "images/xenomai-cobalt/inputs.lock.json", self.lock)
        for args in (["init", "--quiet"], ["add", "."],
                     ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "commit", "--quiet", "-m", "fixture"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        self.recipe_id, self.recipe_files = example.recipe(self.lock, self.deployment, self.root)
        self.cloud = BuildCloud(self.deployment)
        self.cloud_factory = self.enterContext(patch.object(self.build, "Cloud", return_value=self.cloud))
        self.enterContext(patch.object(self.build, "inspect_deployment", return_value={
            "source_ami": {"RootDeviceName": "/dev/sda1"}}))
        self.enterContext(patch.object(self.build, "controller_identity", return_value={
            "instance_id": "i-00000000000000000", "ami_id": self.deployment.controller_ami.id,
            "inventory": inventory}))
        self.enterContext(patch("preflight.inspect_ami"))
        self.packer_commands = []
        self.packer_failure = False
        original_run = subprocess.run

        def run(command, **kwargs):
            if command[0] != "packer":
                return original_run(command, **kwargs)
            self.packer_commands.append(command[1])
            variables = read_json(next(value.split("=", 1)[1] for value in command if value.startswith("-var-file=")))
            key = Path(variables["ssh_private_key_file"])
            self.assertEqual(key.read_text(), "temporary private key")
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            if command[1] == "build":
                kwargs["stdout"].write("packer diagnostics\n")
                if self.packer_failure:
                    raise subprocess.CalledProcessError(1, command)
                destination = Path(variables["output_directory"])
                if not variables["stock_only"]:
                    image = result()["payload"]
                    image["recipe_id"] = self.recipe_id
                    write_json(destination / "image-manifest.json", image)
                    write_json(destination / "packer-manifest.json", {"builds": [{
                        "artifact_id": "us-east-1:ami-11111111111111111"}]})
                    (destination / "inputs.tar").write_bytes(b"retained build inputs")
            return subprocess.CompletedProcess(command, 0)

        self.enterContext(patch.object(self.build.subprocess, "run", side_effect=run))

    def arguments(self, build_id):
        return ["--deployment", str(self.manifest), "--build-id", build_id, "--output", str(self.directory / "output" / build_id),
                "--config-output", str(self.directory / "config" / f"{build_id}.json"), "--execute"]

    def test_config_writes_explicit_destination_without_github_context(self):
        for build_id in (None, "789-2-stock"):
            with self.subTest(build_id=build_id):
                path = self.directory / "config" / "settings.json"
                args = ["--deployment", str(self.manifest), "--output", str(path)]
                if build_id:
                    args += ["--build-id", build_id]
                self.config.main(args)
                value = read_json(path)
                self.assertEqual(value["region"], "us-east-1")
                self.assertEqual(value["role_arn"], "arn:aws:iam::123456789012:role/example")
                self.assertEqual(value["environment"], "ami-build")
                if build_id:
                    self.assertEqual(value["build_id"], build_id)
                    self.assertEqual(value["controller_ami_id"], "ami-0123456789abcdef0")
                else:
                    self.assertEqual(set(value), {"region", "role_arn", "environment"})
        self.cloud_factory.assert_not_called()

    def test_standalone_builds_preserve_outputs_provenance_and_cleanup(self):
        for variant in ("stock", "one"):
            build_id = "789-2-" + variant
            with self.subTest(variant=variant):
                self.build.main(self.arguments(build_id))
                config = read_json(self.directory / "config" / f"{build_id}.json")
                self.assertEqual(config["build_id"], build_id)
                self.assertEqual(config["recipe_id"], self.recipe_id)
                output = self.directory / "output" / build_id
                if variant == "stock":
                    value = read_json(output / "stock-result.json")
                    self.assertEqual(value["status"], "passed")
                    self.assertEqual(value["controller"], "i-00000000000000000")
                else:
                    value = read_json(output / "image-result.json")
                    self.assertEqual(value["source"]["recipe_commit"], self.commit)
                    self.assertEqual(value["source"]["recipe_files"], self.recipe_files)
                    self.assertEqual(value["execution"]["build_id"], build_id)
                    self.assertEqual(config["ami_id"], "ami-11111111111111111")
                    self.assertEqual(self.cloud.retained_contents[f"{build_id}/inputs.tar"], b"retained build inputs")
                self.assertEqual(self.cloud.keys, [])
                self.assertFalse((output / "work/builder-key.pem").exists())
                index = read_json(output / "artifact-index.json")
                self.assertIn("packer.log", index)
                self.assertIn("deployment/manifest.json", index)
                self.assertTrue(any(name.startswith("deployment/parents/") for name in index))
                self.assertIn(f"{build_id}/artifact-index.json", self.cloud.retained)
                self.assertEqual(self.cloud.retained_contents[f"{build_id}/packer.log"], b"packer diagnostics\n")
        self.assertEqual(self.packer_commands, ["validate", "build", "validate", "build"])
        shutil.rmtree(self.inputs)
        for variant in ("stock", "one"):
            snapshot = example.load_deployment(self.directory / "output" / f"789-2-{variant}" / "deployment/manifest.json")
            self.assertEqual(example.recipe(self.lock, snapshot, self.root), (self.recipe_id, self.recipe_files))

    def test_dirty_inputs_stop_before_cloud_or_packer(self):
        self.template.write_text("uncommitted recipe\n")
        with self.assertRaisesRegex(InvalidInput, "commit all locked build inputs"):
            self.build.main(self.arguments("789-2-stock"))
        self.cloud_factory.assert_not_called()
        self.assertEqual(self.packer_commands, [])
        self.assertFalse((self.directory / "output").exists())

    def test_failed_build_cleans_key_and_retains_diagnostics(self):
        self.packer_failure = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.build.main(self.arguments("789-2-stock"))
        self.assertEqual(self.cloud.keys, [])
        self.assertFalse((self.directory / "output/789-2-stock/work/builder-key.pem").exists())
        self.assertEqual(self.cloud.retained_contents["789-2-stock/packer.log"], b"packer diagnostics\n")
        self.assertIn("789-2-stock/artifact-index.json", self.cloud.retained)
        self.assertFalse((self.directory / "config/789-2-stock.json").exists())

    def test_candidate_with_disposable_build_disk_is_rejected_with_diagnostics_and_key_cleanup(self):
        self.cloud.images[0]["BlockDeviceMappings"].append({
            "DeviceName": "/dev/sdf", "Ebs": {"SnapshotId": "snap-22222222222222222", "VolumeSize": 16}})
        with self.assertRaisesRegex(InvalidInput, "only its root snapshot, without the disposable build disk"):
            self.build.main(self.arguments("789-2-one"))
        output = self.directory / "output/789-2-one"
        self.assertEqual(self.packer_commands, ["validate", "build"])
        self.assertEqual(self.cloud.keys, [])
        self.assertFalse((output / "work/builder-key.pem").exists())
        self.assertFalse((output / "image-result.json").exists())
        self.assertFalse((self.directory / "config/789-2-one.json").exists())
        self.assertEqual(self.cloud.retained_contents["789-2-one/packer.log"], b"packer diagnostics\n")
        self.assertEqual(self.cloud.retained_contents["789-2-one/inputs.tar"], b"retained build inputs")
        index = read_json(output / "artifact-index.json")
        self.assertTrue({"controller.json", "preflight.json", "packer-manifest.json", "image-manifest.json",
                         "packer.log", "inputs.tar", "deployment/manifest.json"} <= set(index))
        self.assertIn("789-2-one/artifact-index.json", self.cloud.retained)

    def test_malformed_build_id_stops_before_cloud(self):
        for build_id in ("../789-2-stock", "789-0-stock", "789-2-unknown"):
            with self.subTest(build_id=build_id), self.assertRaisesRegex(InvalidInput, "build ID"):
                self.build.main(self.arguments(build_id))
        self.cloud_factory.assert_not_called()
        self.assertEqual(self.packer_commands, [])


if __name__ == "__main__":
    unittest.main()
