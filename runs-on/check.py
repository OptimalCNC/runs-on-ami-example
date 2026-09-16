#!/usr/bin/env python3
"""Check RunsOn installation source without creating cloud resources."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent


def check_terraform(environment):
    with tempfile.TemporaryDirectory(prefix="runs-on-check-") as temporary:
        for component in ("bootstrap", "deployment"):
            source = ROOT / component
            target = Path(temporary) / component
            target.mkdir()
            # Only reusable source enters the check; local state and tfvars stay outside.
            for pattern in ("*.tf", ".terraform.lock.hcl"):
                for path in source.glob(pattern):
                    shutil.copyfile(path, target / path.name)
            if (source / "tests").is_dir():
                shutil.copytree(source / "tests", target / "tests")

            clean_environment = {key: value for key, value in environment.items() if not key.startswith("TF_")}
            clean_environment.update(TF_DATA_DIR=str(target / ".terraform"),
                                     TF_WORKSPACE="default", TF_IN_AUTOMATION="true")
            command = ["terraform", f"-chdir={target}"]
            subprocess.run([*command, "fmt", "-check", "-recursive"], env=clean_environment, check=True)
            caches = (ROOT / ".local/terraform" / component / "providers", source / ".terraform/providers")
            cache = next((path for path in caches if path.is_dir()), None)
            plugins = [f"-plugin-dir={cache}"] if cache else []
            subprocess.run([*command, "init", "-backend=false", "-input=false", "-lockfile=readonly", *plugins],
                           env=clean_environment, check=True)
            subprocess.run([*command, "validate"], env=clean_environment, check=True)
            if (target / "tests").is_dir():
                subprocess.run([*command, "test", "-no-color"], env=clean_environment, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", action="store_true", help="also run isolated Terraform formatting, validation, and mock tests")
    args = parser.parse_args()
    environment = dict(os.environ)
    environment["PATH"] = str(ROOT / ".local/tools/bin") + os.pathsep + environment.get("PATH", os.defpath)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-v"],
                   cwd=ROOT, env=environment, check=True)
    if args.tools:
        check_terraform(environment)
    print("RunsOn installation checks passed. No cloud resources were created.", flush=True)


if __name__ == "__main__":
    main()
