#!/usr/bin/env python3
"""Install verified packages from the single locked, signed Ubuntu snapshot."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    lock = json.loads(Path(sys.argv[1]).read_text())["os"]
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
    selection = [f"{name}={version}" for name, version in sorted(lock["packages"].items())]
    # No --allow-downgrades: an incompatible newer parent requires an explicit lock refresh.
    subprocess.run(["apt-get", "-y", "--no-install-recommends", "install", *selection], check=True)


if __name__ == "__main__":
    main()
