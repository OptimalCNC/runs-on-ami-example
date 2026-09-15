import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts import BuiltImage, sha256, write_yaml


class BuiltImageContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.disk = self.root / "disk.raw"
        with self.disk.open("wb") as stream:
            stream.truncate(1024**3)
        self.manifest = self.root / "image-manifest.json"
        self.manifest.write_text(json.dumps({
            "recipe_id": "a" * 64, "kernel_release": "6.12.90-cip24-xenomai-cobalt",
            "xenomai": {"core": "cobalt", "version": "3.3.3", "prefix": "/usr/xenomai"},
        }))
        # Hashing a GiB is covered once below; parser cases isolate metadata behavior.
        self.disk_digest = "b" * 64
        original_sha = sha256
        self.enterContext(patch("contracts.sha256", side_effect=lambda p:
            self.disk_digest if Path(p) == self.disk else original_sha(p)))
        self.record = {
            "schema_version": 1, "kind": "built-image", "recipe_id": "a" * 64,
            "disk": {"path": "disk.raw", "format": "raw", "sha256": self.disk_digest, "size_bytes": 1024**3},
            "payload": {"manifest": self.manifest.name, "sha256": original_sha(self.manifest)},
            "compatibility": {"architecture": "x86_64", "boot_mode": "uefi", "secure_boot": False,
                              "minimum_root_volume_gib": 1, "ena_support": True,
                              "runs_on_bootstrap_version": "0.1.12"},
        }

    def load(self, record=None):
        path = self.root / "build.yaml"
        write_yaml(path, self.record if record is None else record)
        return BuiltImage.load(path)

    def test_result_resolves_relative_files_and_carries_checked_identity(self):
        image = self.load()
        self.assertEqual(image.disk_path, self.disk)
        self.assertEqual(image.manifest["recipe_id"], image.recipe_id)
        self.assertEqual(image.compatibility.boot_mode, "uefi")

    def test_corrupted_content_cannot_be_validated_or_published(self):
        self.disk_digest = "c" * 64
        with self.assertRaisesRegex(ValueError, "raw disk checksum"):
            self.load()
        self.disk_digest = "b" * 64
        self.manifest.write_text("{}")
        with self.assertRaisesRegex(ValueError, "payload manifest checksum"):
            self.load()

    def test_manifest_of_another_recipe_is_rejected_even_with_updated_hash(self):
        value = json.loads(self.manifest.read_text())
        value["recipe_id"] = "d" * 64
        self.manifest.write_text(json.dumps(value))
        self.record["payload"]["sha256"] = sha256(self.manifest)
        with self.assertRaisesRegex(ValueError, "payload recipe"):
            self.load()

    def test_rejects_incompatible_and_mis_sized_disks(self):
        cases = [("architecture", "aarch64"), ("boot_mode", "legacy-bios"),
                 ("secure_boot", True), ("ena_support", False),
                 ("minimum_root_volume_gib", True), ("minimum_root_volume_gib", 2)]
        for key, value in cases:
            record = copy.deepcopy(self.record)
            record["compatibility"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(record)

    def test_paths_cannot_escape_the_relocatable_build_directory(self):
        for name in ("/etc/passwd", "../outside.raw", "missing.raw"):
            self.record["disk"]["path"] = name
            with self.subTest(path=name), self.assertRaises(ValueError):
                self.load()

    def test_raw_sha256_includes_sparse_zero_blocks(self):
        import hashlib
        path = self.root / "small.raw"
        with path.open("wb") as stream:
            stream.write(b"header")
            stream.truncate(512 * 1024)
        self.assertEqual(sha256(path), hashlib.sha256(b"header" + bytes(512 * 1024 - 6)).hexdigest())


if __name__ == "__main__":
    unittest.main()
