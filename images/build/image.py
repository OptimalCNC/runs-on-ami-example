#!/usr/bin/env python3
"""Build a finalized raw disk from a local recipe, without AWS credentials."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "images"
GUEST_WORKSPACE = "/mnt/image-build"


def build_seed(directory: Path, public_key: str, cloud_init: dict) -> Path:
    """Supply only temporary login access and deterministic first-boot setup."""
    user_data = {**cloud_init, "ssh_authorized_keys": [public_key.strip()]}
    user_data_path = directory / "user-data"
    user_data_path.write_text("#cloud-config\n" + json.dumps(user_data, indent=2) + "\n")
    metadata_path = directory / "meta-data"
    metadata_path.write_text(json.dumps({"instance-id": "image-build"}) + "\n")
    seed = directory / "seed.iso"
    subprocess.run(["cloud-localds", str(seed), str(user_data_path), str(metadata_path)], check=True)
    return seed


def run_packer(command: list[str], environment: dict, log_path: Path):
    """Keep Packer and its temporary VM in one interruptible process group."""
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            status = process.wait(timeout=5 * 60 * 60)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        if status:
            raise subprocess.CalledProcessError(status, command)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True, help="directory containing recipe.json and standalone installation scripts")
    parser.add_argument("--output", type=Path, required=True, help="new directory for the raw disk and build logs")
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--accelerator", choices=("kvm", "tcg"), default="kvm")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    environment.setdefault("PACKER_CACHE_DIR", str(IMAGES / ".local/cache/packer"))
    recipe_directory = args.recipe.resolve(strict=True)
    recipe = json.loads((recipe_directory / "recipe.json").read_text())
    config = json.loads(Path(__file__).with_name("image.json").read_text())
    source = recipe["source_image"]
    provision_commands = [shlex.join(["sudo", "bash", f"{GUEST_WORKSPACE}/{step}"])
                          for step in recipe["steps"]]
    prepare_command = shlex.join([
        "sudo", "bash", "-c", Path(__file__).with_name("prepare.sh").read_text(),
        "--", GUEST_WORKSPACE, config["ssh_username"],
    ])
    # Execute from memory so finalization can unmount the complete recipe tree.
    finalize_command = shlex.join([
        "sudo", "bash", "-c", Path(__file__).with_name("finalize.sh").read_text(),
        "--", GUEST_WORKSPACE,
    ])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as temporary:
        work = Path(temporary)
        key = work / "build-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "image-build", "-f", str(key)], check=True)
        seed = build_seed(work, key.with_suffix(".pub").read_text(), config["cloud_init"])
        packer_output = work / "packer"
        variables = {
            "source_url": source["url"], "source_checksum": f"file:{source['sha256sums_url']}",
            "root_disk_gib": config["root_disk_gib"], "build_disk_gib": config["build_disk_gib"],
            "ssh_username": config["ssh_username"],
            "guest_workspace": GUEST_WORKSPACE, "prepare_command": prepare_command,
            "provision_commands": provision_commands, "finalize_command": finalize_command,
            "cpus": args.cpus, "memory_mib": args.memory_mib, "accelerator": args.accelerator,
            "seed_iso": str(seed), "ssh_private_key_file": str(key),
            "recipe_directory": str(recipe_directory), "output_directory": str(output),
            "packer_output_directory": str(packer_output),
        }
        variables_path = work / "packer-vars.json"
        variables_path.write_text(json.dumps(variables, indent=2) + "\n")
        template = Path(__file__).with_name("image.pkr.hcl")
        print(f"Building disk from {recipe_directory}; progress: {output / 'packer.log'}", flush=True)
        run_packer(["packer", "build", "-color=false", "-on-error=cleanup", f"-var-file={variables_path}", str(template)],
                   environment, output / "packer.log")
        # A rename preserves sparse extents and exposes only the completed disk.
        (packer_output / "disk.raw").replace(output / "disk.raw")
    print(output / "disk.raw")


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print("Build interrupted; temporary VM and build files were cleaned up.", file=sys.stderr)
        raise SystemExit(130)
