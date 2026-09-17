from contextlib import ExitStack
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


IMAGES = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("guest_report", IMAGES / "validate/guest-report.py")
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


class GuestRuntime(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        config = b"CONFIG_IKCONFIG_PROC=y\nCONFIG_DOVETAIL=y\nCONFIG_XENOMAI=y\nCONFIG_XENO_OPT_VFILE=y\n"
        self.xenomai = {"version": "3.3.3", "core": "cobalt", "prefix": "/usr/xenomai"}
        manifest = {"kernel_release": "cobalt-kernel", "recipe_id": "recipe",
                    "config_sha256": hashlib.sha256(config).hexdigest(), "xenomai": self.xenomai}
        for name, data in (("etc/ami-example.json", json.dumps(manifest).encode()),
                           ("proc/config.gz", gzip.compress(config)),
                           ("proc/cmdline", b"console=ttyS0 xenomai.allowed_group=4242"),
                           ("proc/xenomai/version", b"3.3.3"),
                           ("etc/machine-id", b"fresh-machine"),
                           ("home/runner/bin/Runner.Listener", b"runner-binary")):
            path = self.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        (self.directory / "sys/firmware/efi").mkdir(parents=True)
        self.packages = "gcc-13\t13.3.0\tamd64\tinstalled\npython3\t3.12.3\tamd64\tinstalled\n"
        self.commands = {
            ("uname", "-r"): "cobalt-kernel\n",
            ("/usr/xenomai/bin/xeno-config", "--core"): "cobalt\n",
            ("/usr/xenomai/bin/xeno-config", "--version"): "3.3.3\n",
            ("/usr/xenomai/sbin/corectl", "--status"): "running\n",
            ("/home/runner/bin/Runner.Listener", "--version"): "2.337.0\n",
            ("dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n"):
                self.packages,
        }
        real_stat = Path.stat
        real_sha = reporter.sha

        def root_owned(path, **kwargs):
            parts = list(real_stat(path, **kwargs))
            parts[4] = 0
            return os.stat_result(parts)

        stack = self.enterContext(ExitStack())
        for mocked in (
            patch.object(reporter, "Path", side_effect=lambda name: self.directory / str(name).lstrip("/")),
            patch.object(Path, "stat", root_owned),
            patch.object(reporter, "sha", side_effect=lambda name: real_sha(self.directory / str(name).lstrip("/"))),
            patch.object(reporter.glob, "glob", return_value=[]),
            patch.object(reporter.os, "getuid", return_value=1001),
            patch.object(reporter.os, "geteuid", return_value=1001),
            patch.object(reporter.os, "getgid", return_value=1001),
            patch.object(reporter.os, "getgroups", return_value=[1001, 4242]),
            patch.object(reporter.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="runner")),
            patch.object(reporter.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=4242)),
            patch.object(reporter.resource, "getrlimit", side_effect=lambda kind: (
                reporter.resource.RLIM_INFINITY, reporter.resource.RLIM_INFINITY)
                if kind == reporter.resource.RLIMIT_MEMLOCK else (99, 99)),
            patch.object(reporter.subprocess, "check_output", side_effect=lambda args, **kwargs: self.commands[tuple(args)]),
        ):
            stack.enter_context(mocked)

    def test_observes_running_cobalt_as_runner_with_inherited_limits(self):
        result = reporter.report("cobalt-kernel", "recipe")
        self.assertEqual(result["xenomai"], self.xenomai)
        self.assertEqual(result["boot_mode"], "uefi")
        self.assertEqual(result["runner_uid"], 1001)
        self.assertTrue(result["process_limits_passed"])
        self.assertEqual(result["runner_listener_sha256"], hashlib.sha256(b"runner-binary").hexdigest())
        self.assertEqual(result["packages_sha256"], hashlib.sha256(self.packages.encode()).hexdigest())
        self.assertNotIn("identity", result)

    def test_root_or_missing_process_limits_cannot_produce_passing_report(self):
        with patch.object(reporter.os, "geteuid", return_value=0), self.assertRaisesRegex(ValueError, "UID 1001"):
            reporter.report("cobalt-kernel", "recipe")
        for kind, expected in ((reporter.resource.RLIMIT_MEMLOCK, "locked memory"),
                               (reporter.resource.RLIMIT_RTPRIO, "priority limit")):
            def limits(resource):
                return (0, 0) if resource == kind else (reporter.resource.RLIM_INFINITY, reporter.resource.RLIM_INFINITY)

            with self.subTest(kind=kind), patch.object(reporter.resource, "getrlimit", side_effect=limits), \
                    self.assertRaisesRegex(ValueError, expected):
                reporter.report("cobalt-kernel", "recipe")

    def test_stopped_core_or_missing_nonroot_access_cannot_produce_passing_report(self):
        self.commands[("/usr/xenomai/sbin/corectl", "--status")] = "stopped\n"
        with self.assertRaisesRegex(ValueError, "not running"):
            reporter.report("cobalt-kernel", "recipe")
        self.commands[("/usr/xenomai/sbin/corectl", "--status")] = "running\n"
        with patch.object(reporter.os, "getgroups", return_value=[1001]), \
                self.assertRaisesRegex(ValueError, "group membership"):
            reporter.report("cobalt-kernel", "recipe")


if __name__ == "__main__":
    unittest.main()
