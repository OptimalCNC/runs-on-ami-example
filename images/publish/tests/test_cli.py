"""Exercise the three command interfaces against an in-memory AWS inventory."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from publish.core import PublishingConfig, read_json
from publish.delete import main as delete
from publish.prune import main as prune
from publish.publish import main as publish
from test_retention import MemoryCloud


class CommandTests(unittest.TestCase):
    def test_publish_prune_and_delete_without_caller_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "publishing.json"
            config_path.write_text(json.dumps({
                "name": "minimal-worker", "account_id": "123456789012", "region": "us-east-1",
            }))
            config = PublishingConfig.load(config_path)
            cloud = MemoryCloud(target=config.target)
            disk, output = root / "disk.raw", root / "publication"
            disk.write_bytes(b"raw disk fixture")
            with (patch("publish.publish.Cloud", return_value=cloud),
                  patch("publish.prune.Cloud", return_value=cloud),
                  patch("publish.core.Cloud", return_value=cloud) as delete_cloud,
                  patch("publish.core.signal.signal"), patch("publish.core.os.umask"),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(publish(["--config", str(config_path), "--image", str(disk),
                                          "--output", str(output)]), 0)
                record_path = output / "published-image.json"
                record = read_json(record_path)
                self.assertEqual(record["image"], "minimal-worker")
                self.assertEqual(record["target"]["required_tags"], {})
                disk.unlink()
                self.assertEqual(prune(["--config", str(config_path)]), 0)
                self.assertIn(record["ami_id"], cloud.images)
                config_path.unlink()
                for _ in range(2):
                    self.assertEqual(delete(["--record", str(record_path)]), 0)
                self.assertEqual(delete_cloud.call_args.args[0].identity(), record["target"])
                self.assertEqual(read_json(record_path)["status"], "deleted")
            self.assertEqual(cloud.images, {})
            self.assertEqual(cloud.snapshots, {})

    def test_each_command_accepts_only_its_own_inputs(self):
        cases = (
            (publish, ["--config", "publishing.json"]),
            (publish, ["--config", "publishing.json", "--image", "disk.raw", "--output", "out",
                       "--name", "override"]),
            (prune, ["--config", "publishing.json", "--image", "disk.raw"]),
            (delete, ["--record", "record.json", "--config", "publishing.json"]),
        )
        for command, arguments in cases:
            with self.subTest(command=command.__module__), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    command(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
