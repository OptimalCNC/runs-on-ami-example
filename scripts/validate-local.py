#!/usr/bin/env python3
"""Check image, installation, and execution modules without creating cloud resources."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools", action="store_true", help="also check pinned Packer, Terraform and workflows")
    parser.add_argument("--xenomai-prefix", type=Path, help="compile the Cobalt application against an installed Cobalt SDK")
    parser.add_argument("--cobalt-runtime", action="store_true", help="also execute CTest; requires a running Cobalt kernel")
    args = parser.parse_args()
    if args.cobalt_runtime and not args.xenomai_prefix:
        parser.error("--cobalt-runtime requires --xenomai-prefix")
    subprocess.run([sys.executable, "scripts/validate-inputs.py"], cwd=ROOT, check=True)
    for tests in ("images/tests", "runs-on/tests", "execution/tests"):
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", tests, "-v"], cwd=ROOT, check=True)
    for directory, subdirectories, files in os.walk(ROOT / "images"):
        subdirectories[:] = sorted(name for name in subdirectories if not name.startswith("."))
        for name in sorted(files):
            if name.endswith(".sh"):
                subprocess.run(["bash", "-n", str(Path(directory) / name)], check=True)
    with tempfile.TemporaryDirectory(prefix="ami-example-validate-") as temporary:
        directory = Path(temporary)
        if args.xenomai_prefix:
            subprocess.run(["cmake", "-S", str(ROOT / "tests/cobalt"), "-B", str(directory / "cobalt"),
                            f"-DXENOMAI_ROOT={args.xenomai_prefix.resolve()}"], check=True)
            subprocess.run(["cmake", "--build", str(directory / "cobalt")], check=True)
            if args.cobalt_runtime:
                subprocess.run(["ctest", "--test-dir", str(directory / "cobalt"), "--output-on-failure"], check=True)
            else:
                print("Cobalt application compiled; execution requires a running Cobalt kernel.", flush=True)
        else:
            print("Cobalt application build and execution require the Cobalt SDK and kernel; not run locally.", flush=True)
        if args.tools:
            subprocess.run(["actionlint", "-shellcheck=", *map(str, (ROOT / ".github/workflows").glob("*.yml"))], check=True)
            for terraform_root in ("runs-on/bootstrap", "runs-on/deployment"):
                source = ROOT / terraform_root
                target = directory / terraform_root
                target.mkdir(parents=True, exist_ok=True)
                # Validate reusable source with synthetic inputs, independent of local state/tfvars.
                for pattern in ("*.tf", ".terraform.lock.hcl"):
                    for path in source.glob(pattern):
                        shutil.copyfile(path, target / path.name)
                if (source / "tests").is_dir():
                    shutil.copytree(source / "tests", target / "tests")
                command = ["terraform", f"-chdir={target}"]
                environment = {key: value for key, value in os.environ.items()
                               if not key.startswith(("TF_VAR_", "TF_CLI_ARGS"))}
                environment.update(TF_DATA_DIR=str(target / ".terraform"),
                                   TF_WORKSPACE="default", TF_IN_AUTOMATION="true")
                subprocess.run([*command, "fmt", "-check", "-recursive"], env=environment, check=True)
                caches = (ROOT / "runs-on/.local/terraform" / source.name / "providers",
                          source / ".terraform/providers")
                terraform_cache = next((path for path in caches if path.is_dir()), None)
                plugins = [f"-plugin-dir={terraform_cache}"] if terraform_cache else []
                subprocess.run([*command, "init", "-backend=false", "-input=false", "-lockfile=readonly", *plugins],
                               env=environment, check=True)
                subprocess.run([*command, "validate"], env=environment, check=True)
                if (target / "tests").is_dir():
                    subprocess.run([*command, "test", "-no-color"], env=environment, check=True)
            template = "images/xenomai-cobalt/image.pkr.hcl"
            subprocess.run(["packer", "fmt", "-check", template], cwd=ROOT, check=True)
            subprocess.run(["packer", "validate", "-syntax-only", template], cwd=ROOT, check=True)
    print("Local validation passed. No cloud resources were created.")


if __name__ == "__main__":
    main()
