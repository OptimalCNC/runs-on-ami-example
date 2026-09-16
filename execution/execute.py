#!/usr/bin/env python3
"""Prove a published Cobalt image executes on its selected RunsOn installation."""
import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import stat
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import yaml

from contract import ExecutionPlan


ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path("/etc/ami-example.json")
REPORTER = Path("/usr/local/bin/ami-example-guest-report")
SUCCESS_MARKER = "Cobalt task completed in primary mode at priority 50"
INSTANCE_TYPE = "t3.small"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value, label):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), f"invalid {label} digest")
    return value


def write_result(path: Path, result: dict):
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        temporary.write_text(yaml.safe_dump(result, sort_keys=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class GitHubRun:
    repository: str
    sha: str
    run_id: str
    run_attempt: str
    url: str

    @classmethod
    def from_environment(cls, environment):
        repository = environment.get("GITHUB_REPOSITORY", "")
        sha = environment.get("GITHUB_SHA", "")
        run_id = environment.get("GITHUB_RUN_ID", "")
        attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
        server = environment.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "GitHub repository is missing")
        require(re.fullmatch(r"[0-9a-f]{40}", sha), "GitHub source SHA is missing")
        require(re.fullmatch(r"[1-9][0-9]*", run_id) and re.fullmatch(r"[1-9][0-9]*", attempt),
                "GitHub run identity is missing")
        parsed = urlsplit(server)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment and not parsed.path,
                "GitHub server must be an HTTPS origin")
        return cls(repository, sha, run_id, attempt, f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}")


def checked_out_source(run: GitHubRun, root: Path) -> str:
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                                     text=True, timeout=10).strip()
    require(actual == run.sha, "checked-out source HEAD differs from GITHUB_SHA")
    require((root / "tests/cobalt/CMakeLists.txt").is_file() and (root / "tests/cobalt/main.c").is_file(),
            "checked-out Cobalt application is missing")
    return actual


def runner_identity() -> dict:
    uid = os.getuid()
    name = pwd.getpwuid(uid).pw_name
    require(uid == 1001 and os.geteuid() == uid and name == "runner", "execution requires ordinary runner user UID 1001")
    return {"uid": uid, "username": name}


def ec2_metadata(opener=None) -> tuple[dict, dict]:
    """Read identity and instance-profile information; never fetch credentials."""
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://169.254.169.254/latest/"
    request = urllib.request.Request(base + "api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    with opener.open(request, timeout=5) as response:
        token = response.read(65536).decode()
    require(token and "\n" not in token, "IMDSv2 did not return a token")
    values = []
    for endpoint in ("dynamic/instance-identity/document", "meta-data/iam/info"):
        request = urllib.request.Request(base + endpoint, headers={"X-aws-ec2-metadata-token": token})
        with opener.open(request, timeout=5) as response:
            value = json.loads(response.read(1024 * 1024))
        require(isinstance(value, dict), "EC2 metadata must be a JSON object")
        values.append(value)
    return values[0], values[1]


@dataclass(frozen=True)
class Instance:
    account_id: str
    region: str
    ami_id: str
    instance_id: str
    instance_type: str
    architecture: str
    runner_profile_arn: str

    @classmethod
    def from_metadata(cls, identity, profile):
        require(profile.get("Code") == "Success", "EC2 did not identify an attached instance profile")
        names = ("accountId", "region", "imageId", "instanceId", "instanceType", "architecture")
        require(all(isinstance(identity.get(name), str) and identity[name] for name in names),
                "EC2 instance identity is incomplete")
        require(isinstance(profile.get("InstanceProfileArn"), str) and profile["InstanceProfileArn"],
                "EC2 instance profile ARN is missing")
        return cls(*(identity[name] for name in names), profile["InstanceProfileArn"])

    def for_plan(self, plan):
        expected = {"account_id": plan.installation.account_id, "region": plan.installation.region,
                    "ami_id": plan.image.ami_id, "instance_type": INSTANCE_TYPE, "architecture": "x86_64",
                    "runner_profile_arn": plan.installation.runner_profile_arn}
        for field, value in expected.items():
            require(getattr(self, field) == value, f"EC2 {field} differs from the execution inputs")
        require(re.fullmatch(r"i-[0-9a-f]+", self.instance_id), "EC2 instance ID is invalid")
        return ExecutionInstance(self)


@dataclass(frozen=True)
class ExecutionInstance:
    observed: Instance


@dataclass(frozen=True)
class ImageManifest:
    kernel_release: str
    recipe_id: str
    xenomai_prefix: str
    expected_report: dict
    reporter_sha256: str

    @classmethod
    def load(cls, path: Path, image):
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o022,
                    "image manifest must be a root-owned file without group or world write permission")
            data = stream.read()
        require(hashlib.sha256(data).hexdigest() == image.manifest_sha256,
                "installed manifest digest differs from the published image")
        return cls.parse(json.loads(data), image)

    @classmethod
    def parse(cls, value, image):
        require(isinstance(value, dict) and type(value.get("schema_version")) is int
                and value["schema_version"] == 1, "image manifest schema differs")
        require(value.get("recipe_id") == image.recipe_id, "image manifest recipe differs from the publication")
        kernel = value.get("kernel_release")
        require(isinstance(kernel, str) and re.fullmatch(r"[A-Za-z0-9_.+-]+", kernel), "kernel release is missing")
        xenomai = value.get("xenomai")
        require(isinstance(xenomai, dict) and set(xenomai) == {"core", "prefix", "version"}
                and xenomai["core"] == "cobalt" and xenomai["prefix"] == "/usr/xenomai"
                and isinstance(xenomai["version"], str) and re.fullmatch(r"3\.\d+\.\d+", xenomai["version"]),
                "image manifest must identify the Cobalt kernel and SDK")
        runner = value.get("runner_inventory")
        require(isinstance(runner, dict) and set(runner) == {"runner_version", "runner_listener_sha256", "bootstrap_files"},
                "image manifest runner inventory is incomplete")
        require(isinstance(runner["runner_version"], str) and re.fullmatch(r"\d+\.\d+\.\d+", runner["runner_version"]),
                "image manifest runner version is invalid")
        bootstrap = f"/usr/local/bin/runs-on-bootstrap-v{image.compatibility.runs_on_bootstrap_version}"
        require(isinstance(runner["bootstrap_files"], dict) and set(runner["bootstrap_files"]) == {bootstrap},
                "image bootstrap differs from the publication compatibility")
        digest(runner["bootstrap_files"][bootstrap], "bootstrap")
        digest(runner["runner_listener_sha256"], "runner listener")
        snapshots = value.get("snap_hashes")
        require(isinstance(snapshots, dict), "image manifest snap inventory is missing")
        for name, checksum in snapshots.items():
            require(isinstance(name, str) and name, "invalid snap inventory name")
            digest(checksum, "snap")
        expected = {"kernel_release": kernel, "recipe_id": image.recipe_id, "xenomai": dict(xenomai),
                    "config_sha256": digest(value.get("config_sha256"), "kernel configuration"),
                    "packages_sha256": digest(value.get("packages_sha256"), "packages"), "snap_hashes": snapshots,
                    **runner, "boot_mode": "uefi", "environment_passed": True}
        normalized = value.get("normalized_configuration", {})
        require(isinstance(normalized, dict), "image manifest normalized configuration is missing")
        reporter_digest = digest(normalized.get(str(REPORTER)), "guest reporter")
        return cls(kernel, image.recipe_id, xenomai["prefix"], expected, reporter_digest)

    def reporter(self, path: Path) -> Path:
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            require(metadata.st_uid == 0 and stat.S_ISREG(metadata.st_mode) and not metadata.st_mode & 0o022,
                    "guest reporter must be root-owned without group or world write permission")
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        require(checksum == self.reporter_sha256, "installed guest reporter differs from the published manifest")
        return path


@dataclass(frozen=True)
class GuestEvidence:
    report: dict

    @classmethod
    def parse(cls, report, manifest: ImageManifest, instance: ExecutionInstance):
        require(isinstance(report, dict) and type(report.get("schema_version")) is int
                and report["schema_version"] == 1, "guest report schema differs")
        require(report.get("environment_passed") is True, "guest environment_passed must confirm process limits and SDK activation")
        for key, value in manifest.expected_report.items():
            require(report.get(key) == value, f"guest {key} differs from the published image")
        identity = report.get("identity")
        require(isinstance(identity, dict), "guest report is missing EC2 identity")
        expected_identity = {"accountId": instance.observed.account_id, "region": instance.observed.region,
                             "imageId": instance.observed.ami_id, "instanceId": instance.observed.instance_id,
                             "instanceType": instance.observed.instance_type, "architecture": instance.observed.architecture}
        for key, value in expected_identity.items():
            require(identity.get(key) == value, f"guest EC2 {key} differs from the observed instance")
        return cls(report)


@dataclass(frozen=True)
class ApplicationEvidence:
    test_name: str = "cobalt"
    executed_tests: int = 1
    success_marker: str = SUCCESS_MARKER

    @classmethod
    def load(cls, path: Path):
        root = ET.parse(path).getroot()
        require(root.tag in ("testsuite", "testsuites"), "CTest must produce a JUnit test suite")
        cases = list(root.iter("testcase"))
        require(len(cases) == 1 and cases[0].get("name") == "cobalt"
                and cases[0].get("status") in (None, "run")
                and not any(cases[0].find(tag) is not None for tag in ("failure", "error", "skipped")),
                "one successful, non-skipped Cobalt application test is required")
        for suite in root.iter("testsuite"):
            require(suite.get("tests", "1") == "1", "CTest suite must contain exactly one test")
            require(all(suite.get(key, "0") == "0" for key in ("failures", "errors", "skipped", "disabled")),
                    "CTest suite reports an unsuccessful or skipped test")
        output = "".join(node.text or "" for node in cases[0].iter("system-out"))
        require(SUCCESS_MARKER in output.splitlines(), "Cobalt test did not report primary-mode execution at priority 50")
        return cls()


def stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # A compiler child can outlive its terminated parent while holding the group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run_logged(command: list[str], log_path: Path, *, timeout: int, root: Path):
    print("Running: " + shlex.join(command), flush=True)
    with log_path.open("w") as stream:
        process = subprocess.Popen(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
            if status:
                raise subprocess.CalledProcessError(status, command)
        except BaseException:
            stop_process_group(process)
            raise
        finally:
            stream.flush()
            print(log_path.read_text(errors="replace"), end="", flush=True)


def execute(inputs_path: Path, output: Path, *, root: Path = ROOT, environment=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    require(not any(output.iterdir()), "execution output directory must be empty")
    result = {"schema_version": 1, "kind": "runs-on-execution", "status": "failed", "started_at": timestamp()}
    try:
        result["stage"] = "inputs"
        run = GitHubRun.from_environment(os.environ if environment is None else environment)
        result["github"] = asdict(run)
        plan = ExecutionPlan.from_inputs(json.loads(inputs_path.read_text()), run.repository)
        result["installation"] = {"name": plan.installation.name, "account_id": plan.installation.account_id,
                                  "region": plan.installation.region, "environment": plan.installation.environment}
        result["image"] = {"publication_id": plan.image.publication_id, "ami_id": plan.image.ami_id,
                           "snapshot_id": plan.image.snapshot_id, "recipe_id": plan.image.recipe_id,
                           "disk_sha256": plan.image.disk_sha256, "manifest_sha256": plan.image.manifest_sha256}
        result["stage"] = "runner"
        result["runner"] = runner_identity()
        result["source_sha"] = checked_out_source(run, root)
        result["stage"] = "instance"
        identity, profile = ec2_metadata()
        observed = Instance.from_metadata(identity, profile)
        result["instance"] = asdict(observed)
        instance = observed.for_plan(plan)
        result["stage"] = "manifest"
        manifest = ImageManifest.load(MANIFEST, plan.image)
        reporter = manifest.reporter(REPORTER)
        result["stage"] = "guest"
        guest_path = output / "guest-report.json"
        run_logged([str(reporter), "--platform", "ec2", "--environment", "--release", manifest.kernel_release,
                    "--recipe", plan.image.recipe_id, "--output", str(guest_path)],
                   output / "guest-report.log", timeout=60, root=root)
        GuestEvidence.parse(json.loads(guest_path.read_text()), manifest, instance)
        result["guest_report"] = guest_path.name
        result["stage"] = "configure"
        build = output / "build"
        run_logged(["/usr/bin/cmake", "-S", str(root / "tests/cobalt"), "-B", str(build), "-G", "Ninja",
                    "-DCMAKE_C_COMPILER=/usr/bin/gcc-13", "-DCMAKE_MAKE_PROGRAM=/usr/bin/ninja",
                    f"-DXENOMAI_ROOT={manifest.xenomai_prefix}"], output / "configure.log", timeout=120, root=root)
        result["stage"] = "build"
        run_logged(["/usr/bin/cmake", "--build", str(build)], output / "build.log", timeout=120, root=root)
        result["stage"] = "application"
        junit = output / "ctest.xml"
        run_logged(["/usr/bin/ctest", "--test-dir", str(build), "--verbose", "--output-on-failure",
                    "--output-junit", str(junit)], output / "ctest.log", timeout=60, root=root)
        result["application"] = {**asdict(ApplicationEvidence.load(junit)), "junit": junit.name}
        result.update(status="passed", stage="complete")
    except BaseException as error:
        result["error"] = {"type": type(error).__name__, "message": str(error) or "execution interrupted"}
        raise
    finally:
        result["finished_at"] = timestamp()
        write_result(output / "execution.yaml", result)
    return result


def interrupted(signum, frame):
    raise KeyboardInterrupt("execution interrupted by signal")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True, help="JSON containing installation and published_image YAML strings")
    parser.add_argument("--output", type=Path, required=True, help="new or empty directory for execution evidence")
    args = parser.parse_args(argv)
    try:
        execute(args.inputs, args.output)
    except KeyboardInterrupt as error:
        print(str(error) or "Execution interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Execution failed: {error}", file=sys.stderr)
        return 1
    print(args.output.resolve() / "execution.yaml")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
