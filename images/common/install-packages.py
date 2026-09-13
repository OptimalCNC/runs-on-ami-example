#!/usr/bin/env python3
"""Install from the single locked, signed Ubuntu snapshot and retain downloads."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    lock = json.loads(Path(sys.argv[1]).read_text())["os"]
    output = Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    sources = Path("/etc/apt/sources.list.d")
    for path in sources.iterdir():
        if path.suffix in (".sources", ".list"):
            path.unlink()
    Path("/etc/apt/sources.list").write_text("")
    (sources / "ami-example.sources").write_text(
        f"Types: deb\nURIs: {lock['snapshot_url']}\nSuites: {' '.join(lock['suites'])}\n"
        "Components: main universe\nArchitectures: amd64\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\nCheck-Valid-Until: no\n"
    )
    # apt verifies Release signatures and every package against the immutable index.
    subprocess.run(["apt-get", "update"], check=True)
    selection = [f"{name}={value['version']}" for name, value in sorted(lock["packages"].items())]
    cache = output / "debs"
    cache.mkdir(exist_ok=True)
    args = ["apt-get", "-y", "--no-install-recommends", "-o", f"Dir::Cache::archives={cache}",
            "-o", "APT::Keep-Downloaded-Packages=true", "install", "--reinstall", *selection]
    # No --allow-downgrades: an incompatible newer parent requires an explicit lock refresh.
    subprocess.run([*args, "--download-only"], check=True)
    downloaded = []
    by_identity = {}
    for deb in sorted(cache.glob("*.deb")):
        name, version = subprocess.check_output(["dpkg-deb", "-f", str(deb), "Package", "Version"], text=True).splitlines()
        name, version = name.removeprefix("Package: "), version.removeprefix("Version: ")
        checksum = hashlib.sha256(deb.read_bytes()).hexdigest()
        by_identity[(name, version)] = checksum
        downloaded.append({"file": deb.name, "package": name, "version": version, "sha256": checksum})
    for name, spec in lock["packages"].items():
        if by_identity.get((name, spec["version"])) != spec["sha256"]:
            raise SystemExit(f"locked package missing or checksum mismatch: {name}")
    (output / "package-downloads.json").write_text(json.dumps(downloaded, sort_keys=True, indent=2) + "\n")
    shutil.copytree("/var/lib/apt/lists", output / "apt-lists", dirs_exist_ok=True)
    subprocess.run([*args, "--no-download"], check=True)
    for name, spec in lock["packages"].items():
        version = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", name], text=True)
        if version != spec["version"]:
            raise SystemExit(f"installed version differs: {name}")


if __name__ == "__main__":
    main()
