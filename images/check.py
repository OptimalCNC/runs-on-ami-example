#!/usr/bin/env python3
"""Check image source, tests, and optional Packer configuration without booting a VM."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", action="store_true", help="also check Packer formatting and syntax")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "validate-inputs.py")], check=True)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-v"], check=True)
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = sorted(name for name in subdirectories if not name.startswith("."))
        for name in sorted(files):
            if name.endswith(".sh"):
                subprocess.run(["bash", "-n", str(Path(directory) / name)], check=True)
    if args.tools:
        environment = dict(os.environ)
        tools = ROOT / ".local/tools"
        environment["PATH"] = str(tools / "bin") + os.pathsep + environment.get("PATH", "")
        if (tools / "plugins").is_dir():
            environment["PACKER_PLUGIN_PATH"] = str(tools / "plugins")
        template = ROOT / "xenomai-cobalt/image.pkr.hcl"
        subprocess.run(["packer", "fmt", "-check", str(template)], env=environment, check=True)
        subprocess.run(["packer", "validate", "-syntax-only", str(template)], env=environment, check=True)
    print("Image source checks passed. No VM or AWS resources were created.")


if __name__ == "__main__":
    main()
