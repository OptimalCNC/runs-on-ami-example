#!/usr/bin/env python3
"""Build a finalized Cobalt raw disk locally, without AWS credentials."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from contracts import sha256, write_json


ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "images"
LOCK = IMAGES / "build/xenomai-cobalt/inputs.lock.json"


def build_seed(directory: Path, public_key: str) -> Path:
    """Supply only temporary login access and deterministic first-boot setup."""
    user_data = {
        "ssh_authorized_keys": [public_key.strip()],
        "bootcmd": [
            ["systemctl", "disable", "--now", "apt-daily.timer", "apt-daily-upgrade.timer"],
            ["systemctl", "mask", "apt-daily.service", "apt-daily-upgrade.service", "unattended-upgrades.service"],
        ],
    }
    user_data_path = directory / "user-data"
    user_data_path.write_text("#cloud-config\n" + json.dumps(user_data, indent=2) + "\n")
    metadata_path = directory / "meta-data"
    metadata_path.write_text(json.dumps({"instance-id": "cobalt-build"}) + "\n")
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


def export_build(output: Path, packer_output: Path, lock: dict):
    """Publish the contract only after the stopped VM yields a complete artifact."""
    disk = packer_output / "disk.raw"
    final_disk = output / "disk.raw"
    # A rename preserves sparse extents without copying the disposable build disk.
    disk.replace(final_disk)
    result = {
        "disk": {"path": final_disk.name, "sha256": sha256(final_disk)},
        "kernel_release": lock["kernel"]["release"],
        "xenomai_version": lock["xenomai"]["version"],
    }
    write_json(output / "build.json", result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for the image and build evidence")
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--accelerator", choices=("kvm", "tcg"), default="kvm")
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    environment.setdefault("PACKER_CACHE_DIR", str(IMAGES / ".local/cache/packer"))
    lock = json.loads(LOCK.read_text())
    source = lock["source_image"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as temporary:
        work = Path(temporary)
        key = work / "build-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "cobalt-build", "-f", str(key)], check=True)
        seed = build_seed(work, key.with_suffix(".pub").read_text())
        packer_output = work / "packer"
        variables = {
            "source_url": source["url"], "source_sha256": source["sha256"],
            "cpus": args.cpus, "memory_mib": args.memory_mib, "accelerator": args.accelerator,
            "seed_iso": str(seed), "ssh_private_key_file": str(key),
            "recipe_directory": str(IMAGES / "build"), "output_directory": str(output),
            "packer_output_directory": str(packer_output),
        }
        variables_path = work / "packer-vars.json"
        variables_path.write_text(json.dumps(variables, indent=2) + "\n")
        template = IMAGES / "build/xenomai-cobalt/image.pkr.hcl"
        print(f"Building Cobalt disk; progress: {output / 'packer.log'}", flush=True)
        run_packer(["packer", "build", "-color=false", "-on-error=cleanup", f"-var-file={variables_path}", str(template)],
                   environment, output / "packer.log")
        export_build(output, packer_output, lock)
    print(output / "build.json")


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print("Build interrupted; temporary VM and build files were cleaned up.", file=sys.stderr)
        raise SystemExit(130)
