#!/usr/bin/env python3
"""Run each module's local checks and, optionally, lint GitHub workflows."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", action="store_true", help="also check Packer, Terraform, and workflows")
    args = parser.parse_args()
    for module in ("images", "runs-on", "execution"):
        command = [sys.executable, str(ROOT / module / "check.py")]
        if args.tools and module != "execution":
            command.append("--tools")
        subprocess.run(command, cwd=ROOT / module, check=True)
    if args.tools:
        environment = {**os.environ, "PATH": str(ROOT / ".github/.local/tools/bin") + os.pathsep + os.environ.get("PATH", "")}
        subprocess.run(["actionlint", "-shellcheck=", "-config-file=" + str(ROOT / ".github/actionlint.yaml"),
                        *map(str, sorted((ROOT / ".github/workflows").glob("*.yml")))],
                       cwd=ROOT, env=environment, check=True)
    print("Repository checks passed. No cloud resources were created.")


if __name__ == "__main__":
    main()
