"""Verify build input identity and complete-artifact handoff without a VM."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build


class RecipeTests(unittest.TestCase):
    def test_staged_recipe_is_stable_after_checkout_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            source = root / "provision.sh"
            source.write_text("original guest setup\n")
            lock = {"recipe_files": ["provision.sh"], "source_image": {"sha256": "a" * 64}}
            stage = Path(directory) / "stage"
            original_id, original_files = build.stage_recipe(lock, stage, root)
            source.write_text("changed guest setup\n")
            changed_id, _ = build.stage_recipe(lock, Path(directory) / "changed", root)
            self.assertNotEqual(original_id, changed_id)
            self.assertEqual((stage / "provision.sh").read_text(), "original guest setup\n")
            self.assertEqual(original_files["provision.sh"], build.sha256(stage / "provision.sh"))
            same_id, _ = build.stage_recipe(lock, Path(directory) / "same", stage)
            self.assertEqual(original_id, same_id)

    def test_locked_source_changes_recipe_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "provision.sh").write_text("guest setup\n")
            lock = {"recipe_files": ["provision.sh"], "source_image": {"sha256": "a" * 64}}
            first, _ = build.stage_recipe(lock, root / "first", root)
            changed = copy.deepcopy(lock)
            changed["source_image"]["sha256"] = "b" * 64
            second, _ = build.stage_recipe(changed, root / "second", root)
            self.assertNotEqual(first, second)

    def test_parent_traversal_cannot_escape_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            (root / "provision.sh").write_text("guest setup\n")
            lock = {"recipe_files": ["../checkout/provision.sh"]}
            with self.assertRaises(ValueError):
                build.stage_recipe(lock, Path(directory) / "staging", root)


class ExportTests(unittest.TestCase):
    def test_wrong_payload_cannot_be_published_as_complete_build(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            packer = output / "packer"
            packer.mkdir()
            disk = packer / "disk.raw"
            with disk.open("wb") as stream:
                stream.truncate(16 * 1024**3)
            (output / "image-manifest.json").write_text(json.dumps({
                "recipe_id": "wrong recipe", "kernel_release": "cobalt",
            }))
            info = json.dumps({"format": "raw", "virtual-size": 16 * 1024**3})
            with patch.object(build.subprocess, "check_output", return_value=info):
                with self.assertRaisesRegex(ValueError, "different image recipe"):
                    build.export_build(output, packer, {"kernel": {"release": "cobalt"}}, "a" * 64, {}, {})
            self.assertFalse((output / "build.yaml").exists())
            self.assertFalse((output / "disk.raw").exists())
            self.assertTrue(disk.exists())

    def test_backing_image_cannot_be_exported_as_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            packer = output / "packer"
            packer.mkdir()
            (packer / "disk.raw").write_bytes(b"qcow2 header")
            info = json.dumps({"format": "qcow2", "virtual-size": 16 * 1024**3})
            with patch.object(build.subprocess, "check_output", return_value=info):
                with self.assertRaisesRegex(ValueError, "16 GiB raw"):
                    build.export_build(output, packer, {}, "a" * 64, {}, {})
            self.assertFalse((output / "build.yaml").exists())


if __name__ == "__main__":
    unittest.main()
