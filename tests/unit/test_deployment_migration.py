import dataclasses
import subprocess
import tempfile
from pathlib import Path
import unittest

from support import ROOT, deployment, module
from example import InvalidInput, file_sha, load_deployment, read_json, write_json

migration = module("migrate-deployment")


class DeploymentMigration(unittest.TestCase):
    def legacy(self, source):
        current = deployment()
        value = dataclasses.asdict(current)
        for role in ("source", "controller"):
            relative = f"infra/{role}-inventory.json"
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(getattr(current, role + "_ami").inventory_path.read_bytes())
            value[role + "_ami"]["inventory_file"] = relative
        write_json(source / "infra/deployment.json", value)
        write_json(source / "infra/runs-on/deployment.json", {
            "stack_name": "example-runs-on", "parameters": {"VpcCidrBlock": "10.60.0.0/16", "Private": "false"}})
        write_json(source / "infra/example.auto.tfvars.json", {"name_prefix": "example-images"})
        write_json(source / "archive.json", {"source_commit": "2" * 40})
        # Historical evidence is deliberately copied as bytes, without rewriting its schema or date.
        (source / "accepted-image.json").write_bytes(b'{ "schema_version": 1, "image": null }\n')
        (source / "infra/terraform.tfstate").write_bytes(b'{ "version": 4, "serial": 9, "resources": [] }\n')
        return value

    def test_external_migration_preserves_effective_inputs_and_original_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "legacy", root / "private"
            original = self.legacy(source)
            subprocess.run(["python3", str(ROOT / "scripts/migrate-deployment.py"),
                            "--source-dir", str(source), "--output", str(destination)],
                           cwd=root, check=True, stdout=subprocess.PIPE, text=True)
            parsed = dataclasses.asdict(load_deployment(destination / "manifest.json"))
            for role in ("source", "controller"):
                parsed[role + "_ami"]["inventory_file"] = original[role + "_ami"]["inventory_file"]
            self.assertEqual(parsed, original)
            self.assertEqual(read_json(source / "infra/deployment.json"), original)
            self.assertEqual((destination / "state/accepted-image.json").read_bytes(), (source / "accepted-image.json").read_bytes())
            self.assertEqual((destination / "archive/legacy/infra/terraform.tfstate").read_bytes(), (source / "infra/terraform.tfstate").read_bytes())
            spec = read_json(destination / "spec.json")
            self.assertTrue(spec["private"])
            self.assertFalse(spec["runs_on"]["private"])
            self.assertEqual(spec["source_ami"], {"id": original["source_ami"]["id"], "owner": original["source_ami"]["owner"]})
            self.assertEqual(read_json(destination / "bindings.json")["parent_selections"]["source"], spec["source_ami"])
            report = read_json(destination / "migration.json")
            self.assertTrue(report["effective_configuration_unchanged"])
            self.assertFalse(report["image_rebuilt_or_requalified"])
            self.assertFalse(report["configuration_published"])
            archive = destination / "archive/legacy"
            for item in read_json(archive / "archive.json")["files"]:
                self.assertEqual(file_sha(archive / item["path"]), item["sha256"])

    def test_archive_refuses_to_replace_existing_different_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.legacy(root / "legacy")
            migration.archive_legacy(root / "legacy", root / "private")
            (root / "legacy/accepted-image.json").write_text("changed\n")
            with self.assertRaisesRegex(InvalidInput, "overwrite different archived data"):
                migration.archive_legacy(root / "legacy", root / "private")
            self.assertNotEqual((root / "private/archive/legacy/accepted-image.json").read_text(), "changed\n")

    def test_corrupt_parent_is_archived_but_cannot_become_a_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.legacy(root / "legacy")
            (root / "legacy/infra/source-inventory.json").write_text("{}\n")
            archive = migration.archive_legacy(root / "legacy", root / "private")
            with self.assertRaisesRegex(InvalidInput, "legacy inventory digest differs"):
                migration.convert(archive, root / "private")
            self.assertFalse((root / "private/manifest.json").exists())
            self.assertEqual((root / "legacy/infra/source-inventory.json").read_text(), "{}\n")


if __name__ == "__main__":
    unittest.main()
