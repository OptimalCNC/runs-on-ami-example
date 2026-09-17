#!/usr/bin/env python3
"""Observe the finalized image from its non-root Cobalt validation process."""
import argparse
import glob
import gzip
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import pwd
import resource
import subprocess


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def cobalt_identity(image, config):
    """Return the installed identity only after checking the running Cobalt core."""
    xenomai = image["xenomai"]
    if (set(xenomai) != {"version", "core", "prefix"} or xenomai["core"] != "cobalt"
            or xenomai["prefix"] != "/usr/xenomai"
            or not re.fullmatch(r"3\.\d+\.\d+", xenomai["version"])):
        raise ValueError("image manifest must identify Xenomai Cobalt at /usr/xenomai")
    for symbol in (b"CONFIG_DOVETAIL=y", b"CONFIG_XENOMAI=y", b"CONFIG_XENO_OPT_VFILE=y"):
        if symbol not in config.splitlines():
            raise ValueError(f"running kernel lacks Cobalt requirement: {symbol.decode()}")
    if Path("/proc/xenomai/version").read_text().strip() != xenomai["version"]:
        raise ValueError("running Cobalt core version differs from image manifest")
    for option, value in (("--core", "cobalt"), ("--version", xenomai["version"])):
        actual = subprocess.check_output([xenomai["prefix"] + "/bin/xeno-config", option], text=True).strip()
        if actual != value:
            raise ValueError(f"installed Xenomai {option} differs from image manifest")
    state = subprocess.check_output([xenomai["prefix"] + "/sbin/corectl", "--status"], text=True).strip()
    if state != "running":
        raise ValueError(f"Cobalt core is not running: {state}")
    group = grp.getgrnam("xenomai")
    if group.gr_gid != 4242 or "xenomai.allowed_group=4242" not in Path("/proc/cmdline").read_text().split():
        raise ValueError("Cobalt non-root access group differs from recipe")
    if os.geteuid() != 0 and 4242 not in {os.getgid(), *os.getgroups()}:
        raise ValueError("runner process lacks Cobalt group membership")
    return dict(xenomai)


def report(expected_release, expected_recipe):
    if os.getuid() != 1001 or os.geteuid() != 1001 or pwd.getpwuid(os.getuid()).pw_name != "runner":
        raise ValueError("VM validation must run as runner with UID 1001")
    if resource.getrlimit(resource.RLIMIT_MEMLOCK)[0] != resource.RLIM_INFINITY:
        raise ValueError("runner process must inherit unlimited locked memory")
    if resource.getrlimit(resource.RLIMIT_RTPRIO)[0] < 99:
        raise ValueError("runner process must inherit real-time priority limit 99")
    path = Path("/etc/ami-example.json")
    if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ValueError("image manifest ownership/permissions differ")
    image = json.loads(path.read_text())
    release = subprocess.check_output(["uname", "-r"], text=True).strip()
    if release != expected_release or image["kernel_release"] != release:
        raise ValueError(f"running kernel mismatch: {release}")
    if image["recipe_id"] != expected_recipe:
        raise ValueError("baked recipe identity differs")
    config = gzip.decompress(Path("/proc/config.gz").read_bytes())
    if b"CONFIG_IKCONFIG_PROC=y\n" not in config:
        raise ValueError("embedded kernel configuration is unavailable")
    config_sha = hashlib.sha256(config).hexdigest()
    if config_sha != image["config_sha256"]:
        raise ValueError("running effective configuration differs from compiled configuration")
    xenomai = cobalt_identity(image, config)
    if any(p.read_bytes()[4:5] == b"\x01" for p in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*")):
        raise ValueError("Secure Boot is outside the unsigned-kernel qualification boundary")
    package_lines = subprocess.check_output([
        "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n"
    ], text=True).splitlines()
    package_text = "\n".join(sorted(line for line in package_lines if line.endswith("\tinstalled"))) + "\n"
    result = {"schema_version": 1, "kernel_release": release, "recipe_id": image["recipe_id"], "xenomai": xenomai,
            "boot_mode": "uefi" if Path("/sys/firmware/efi").exists() else "legacy-bios",
            "config_sha256": config_sha, "cmdline": Path("/proc/cmdline").read_text().strip(),
            "machine_id": Path("/etc/machine-id").read_text().strip(),
            "packages_sha256": hashlib.sha256(package_text.encode()).hexdigest(),
            "snap_hashes": {p.name: sha(p) for p in sorted(Path("/var/lib/snapd/snaps").glob("*.snap"))},
            "bootstrap_files": {p: sha(p) for p in sorted(glob.glob("/usr/local/bin/runs-on-bootstrap-*"))},
            "runner_listener_sha256": sha("/home/runner/bin/Runner.Listener"),
            "runner_version": subprocess.check_output(["/home/runner/bin/Runner.Listener", "--version"], text=True).strip()}
    result.update(runner_uid=os.getuid(), process_limits_passed=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = report(args.release, args.recipe)
    text = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
