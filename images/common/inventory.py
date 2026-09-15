#!/usr/bin/env python3
"""Describe a clean Ubuntu image before changing its packages or runner tools."""
import argparse
import glob
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def packages():
    text = subprocess.check_output([
        "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n"
    ], text=True)
    return "\n".join(sorted(line for line in text.splitlines() if line.endswith("\tinstalled"))) + "\n"


def inventory():
    os_release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    package_text = packages()
    listener = Path("/home/runner/bin/Runner.Listener")
    bootstraps = sorted(glob.glob("/usr/local/bin/runs-on-bootstrap-*"))
    registration = [str(p) for base in (Path("/home/runner"), Path("/root"), Path("/opt/actions-runner"))
                    for name in (".runner", ".credentials", ".credentials_rsaparams") if (p := base / name).exists()]
    workspaces = [str(p) for p in (Path("/home/runner/_work"), Path("/opt/actions-runner/_work"))
                  if p.exists() and any(p.iterdir())]
    secure_boot = any(p.read_bytes()[4:5] == b"\x01" for p in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"))
    return {
        "os_version": os_release["VERSION_ID"].strip('"'),
        "package_inventory": package_text,
        "packages_sha256": hashlib.sha256(package_text.encode()).hexdigest(),
        "runner_version": subprocess.check_output([str(listener), "--version"], text=True).strip() if listener.is_file() else None,
        "runner_listener_sha256": sha(listener) if listener.is_file() else None,
        "bootstrap_files": {p: sha(p) for p in bootstraps},
        "snap_hashes": {p.name: sha(p) for p in sorted(Path("/var/lib/snapd/snaps").glob("*.snap"))},
        "registered": bool(registration), "workspaces": workspaces,
        "secure_boot": secure_boot,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect")
    args = parser.parse_args()
    actual = inventory()
    if args.expect:
        expected = json.loads(Path(args.expect).read_text())
        if actual != expected:
            changed = [key for key in actual if actual[key] != expected.get(key)]
            raise SystemExit("Inherited image inventory differs: " + ", ".join(changed))
    print(json.dumps(actual, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
