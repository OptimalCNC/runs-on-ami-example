#!/usr/bin/env python3
"""Credential-free validation; never starts builders, probes or Terraform apply."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

from example import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools", action="store_true", help="also check pinned Packer, Terraform and workflows")
    parser.add_argument("--xenomai-prefix", type=Path, help="compile the Cobalt application against an installed Cobalt SDK")
    parser.add_argument("--cobalt-runtime", action="store_true", help="also execute CTest; requires a running Cobalt kernel")
    args = parser.parse_args()
    if args.cobalt_runtime and not args.xenomai_prefix:
        parser.error("--cobalt-runtime requires --xenomai-prefix")
    subprocess.run(["python3", "scripts/validate-inputs.py"], cwd=ROOT, check=True)
    subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests/unit", "-v"], cwd=ROOT, check=True)
    for path in sorted((ROOT / "images").rglob("*.sh")):
        subprocess.run(["bash", "-n", str(path)], check=True)
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
            terraform_cache = ROOT / "infra/.terraform/providers"
            for terraform_root in ("infra/foundation", "infra", "infra/operator"):
                source = ROOT / terraform_root
                target = directory / terraform_root
                target.mkdir(parents=True, exist_ok=True)
                # Validate reusable source with synthetic inputs, independent of local state/tfvars.
                for pattern in ("*.tf", ".terraform.lock.hcl", "*.tfvars.json.example"):
                    for path in source.glob(pattern):
                        shutil.copyfile(path, target / path.name)
                if (source / "tests").is_dir():
                    shutil.copytree(source / "tests", target / "tests")
                command = ["terraform", f"-chdir={target}"]
                subprocess.run([*command, "fmt", "-check", "-recursive"], check=True)
                plugins = [f"-plugin-dir={terraform_cache}"] if terraform_cache.is_dir() else []
                subprocess.run([*command, "init", "-backend=false", "-input=false", "-lockfile=readonly", *plugins], check=True)
                terraform_cache = target / ".terraform/providers"
                subprocess.run([*command, "validate"], check=True)
                if (target / "tests").is_dir():
                    subprocess.run([*command, "test", "-no-color"], check=True)
            template = "images/xenomai-cobalt/image.pkr.hcl"
            subprocess.run(["packer", "fmt", "-check", template], cwd=ROOT, check=True)
            subprocess.run(["packer", "validate", "-syntax-only", template], cwd=ROOT, check=True)
    print("Local validation passed. No cloud resources were created.")


if __name__ == "__main__":
    main()
