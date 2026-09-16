"""Exercise the local and hosted preparation commands through their real CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contract import ExecutionPlan
from fixtures import image_value, installation_value


class PrepareCommand(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.installation = self.directory / "installation.yaml"
        self.image = self.directory / "published-image.yaml"
        self.output = self.directory / "dispatch.json"
        self.github_output = self.directory / "github-output"
        self.installation.write_text(yaml.safe_dump(installation_value()))
        self.image.write_text(yaml.safe_dump(image_value()))

    def command(self, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
        return subprocess.run([
            sys.executable, str(Path(__file__).resolve().parents[1] / "prepare.py"),
            "--output", str(self.output), *map(str, arguments),
        ], env=environment, text=True, capture_output=True, timeout=10)

    def test_local_files_produce_usable_dispatch_inputs_without_private_fields(self):
        original = installation_value()
        original["license_key"] = "never-forward-this-value"
        self.installation.write_text(yaml.safe_dump(original))
        result = self.command("--installation", self.installation, "--image", self.image)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = ExecutionPlan.from_inputs(json.loads(self.output.read_text()))
        self.assertEqual(plan.image.ami_id, image_value()["ami_id"])
        self.assertNotIn("never-forward", self.output.read_text())

    def test_hosted_preparation_reparses_inputs_and_emits_safe_scheduling_values(self):
        event = self.directory / "event.json"
        event.write_text(json.dumps({"inputs": {
            "installation": self.installation.read_text(), "published_image": self.image.read_text(),
        }}))
        result = self.command("--event", event, "--repository", "ExampleOrg/project",
                              "--github-output", self.github_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dict(line.split("=", 1) for line in self.github_output.read_text().splitlines()), {
            "ami_id": image_value()["ami_id"], "environment": "production", "region": "us-east-1",
        })

    def test_mismatched_publication_cannot_emit_dispatch_or_job_outputs(self):
        wrong = image_value()
        wrong["target"]["name"] = "different-installation"
        self.image.write_text(yaml.safe_dump(wrong))
        result = self.command("--installation", self.installation, "--image", self.image,
                              "--github-output", self.github_output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.github_output.exists())

    def test_event_requires_repository_and_cannot_mix_local_contracts(self):
        event = self.directory / "event.json"
        event.write_text("{}")
        for arguments in (("--event", event), ("--event", event, "--repository", "ExampleOrg/project",
                                               "--installation", self.installation, "--image", self.image)):
            with self.subTest(arguments=arguments):
                result = self.command(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_direct_dispatch_from_the_wrong_github_organization_is_rejected(self):
        event = self.directory / "event.json"
        event.write_text(json.dumps({"inputs": {
            "installation": self.installation.read_text(), "published_image": self.image.read_text(),
        }}))
        result = self.command("--event", event, "--repository", "AnotherOrg/project")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
