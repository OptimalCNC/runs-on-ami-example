#!/usr/bin/env python3
"""Build a finalized Cobalt raw disk locally, without AWS credentials."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
from urllib.parse import urlsplit

from contracts import sha256, write_yaml


ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "images"
LOCK = IMAGES / "build/xenomai-cobalt/inputs.lock.json"


def firmware_paths() -> tuple[Path, Path]:
    """Use a matching OVMF pair without enforced Secure Boot."""
    directory = Path("/usr/share/OVMF")
    for suffix in ("_4M", ""):
        code = directory / f"OVMF_CODE{suffix}.fd"
        variables = directory / f"OVMF_VARS{suffix}.fd"
        if code.is_file() and variables.is_file():
            return code, variables
    raise ValueError("install ovmf: matching OVMF_CODE_4M.fd and OVMF_VARS_4M.fd are required")


@dataclass(frozen=True)
class SourceImage:
    url: str
    sha256: str
    format: str
    architecture: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("source_image must identify its URL, SHA-256, format and architecture")
        if not isinstance(value["url"], str):
            raise ValueError("source image URL must be a string")
        url = urlsplit(value["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("source image must use a public HTTPS URL")
        if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            raise ValueError("source image requires a SHA-256 digest")
        if value["format"] != "qcow2" or value["architecture"] != "x86_64":
            raise ValueError("source image must be an x86_64 QCOW2 disk")
        return cls(**value)


def stage_recipe(lock: dict, destination: Path, root: Path = ROOT) -> tuple[str, dict]:
    """Freeze the actual checkout before deriving the identity sent to the guest."""
    root = root.resolve()
    hashes = {}
    for name in lock["recipe_files"]:
        if (not isinstance(name, str) or Path(name).is_absolute()
                or ".." in Path(name).parts or name in hashes):
            raise ValueError("recipe files must be relative paths")
        source = (root / name).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"recipe file is unavailable: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[name] = sha256(target)
    recipe_id = hashlib.sha256(json.dumps(
        {"lock": lock, "files": hashes}, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    lock_path = destination / "images/build/xenomai-cobalt/inputs.lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n")
    (destination / "recipe.json").write_text(json.dumps({"recipe_id": recipe_id}) + "\n")
    return recipe_id, hashes


def build_seed(directory: Path, public_key: str, recipe_id: str) -> Path:
    """Supply only temporary login access and deterministic first-boot setup."""
    user_data = {
        "users": ["default"],
        "ssh_authorized_keys": [public_key.strip()],
        "ssh_pwauth": False,
        "disable_root": True,
        "package_update": False,
        "package_upgrade": False,
        "bootcmd": [
            ["systemctl", "disable", "--now", "apt-daily.timer", "apt-daily-upgrade.timer"],
            ["systemctl", "mask", "apt-daily.service", "apt-daily-upgrade.service", "unattended-upgrades.service"],
        ],
        "runcmd": [["sh", "-c", "if command -v snap >/dev/null 2>&1; then snap refresh --hold=forever; fi"]],
    }
    user_data_path = directory / "user-data"
    user_data_path.write_text("#cloud-config\n" + json.dumps(user_data, indent=2) + "\n")
    metadata_path = directory / "meta-data"
    metadata_path.write_text(json.dumps({"instance-id": "cobalt-build-" + recipe_id, "local-hostname": "cobalt-builder"}) + "\n")
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


def export_build(output: Path, packer_output: Path, lock: dict, recipe_id: str, hashes: dict, environment: dict):
    """Publish the contract only after the stopped VM yields a complete artifact."""
    disk = packer_output / "disk.raw"
    info = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", str(disk)], env=environment))
    if (info.get("format") != "raw" or info.get("virtual-size") != 16 * 1024**3
            or disk.stat().st_size != 16 * 1024**3):
        raise ValueError("Packer must produce one 16 GiB raw root disk")
    payload_path = output / "image-manifest.json"
    payload = json.loads(payload_path.read_text())
    if payload.get("recipe_id") != recipe_id or payload.get("kernel_release") != lock["kernel"]["release"]:
        raise ValueError("Packer returned a different image recipe or kernel")
    expected_xenomai = {key: lock["xenomai"][key] for key in ("version", "core", "prefix")}
    if payload.get("xenomai") != expected_xenomai:
        raise ValueError("Packer returned a different Cobalt payload")
    final_disk = output / "disk.raw"
    # A rename preserves sparse extents without copying the disposable build disk.
    disk.replace(final_disk)
    result = {
        "schema_version": 1,
        "kind": "built-image",
        "recipe_id": recipe_id,
        "source": dict(lock["source_image"]),
        "recipe_files": hashes,
        "disk": {"path": final_disk.name, "format": "raw", "sha256": sha256(final_disk),
                 "size_bytes": final_disk.stat().st_size},
        "compatibility": {"architecture": "x86_64", "boot_mode": "uefi", "secure_boot": False,
                          "minimum_root_volume_gib": 16, "ena_support": True,
                          "runs_on_bootstrap_version": lock["runner"]["bootstrap"]["version"]},
        "payload": {"manifest": payload_path.name, "sha256": sha256(payload_path)},
    }
    write_yaml(output / "build.yaml", result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for the image and build evidence")
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--accelerator", choices=("kvm", "tcg"), default="kvm")
    args = parser.parse_args(argv)
    if not 1 <= args.cpus <= 256 or not 2048 <= args.memory_mib <= 1048576:
        parser.error("cpus must be 1..256 and memory-mib must be 2048..1048576")
    if args.accelerator == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        parser.error("KVM requires read/write access to /dev/kvm; use --accelerator tcg for software emulation")
    environment = dict(os.environ)
    tools = IMAGES / ".local/tools"
    environment["PATH"] = str(tools / "bin") + os.pathsep + environment.get("PATH", "")
    if (tools / "plugins").is_dir():
        environment["PACKER_PLUGIN_PATH"] = str(tools / "plugins")
    environment.setdefault("PACKER_CACHE_DIR", str(IMAGES / ".local/cache/packer"))
    for tool in ("packer", "qemu-system-x86_64", "qemu-img", "cloud-localds", "ssh-keygen"):
        if shutil.which(tool, path=environment["PATH"]) is None:
            parser.error(f"required build tool is unavailable: {tool}")
    lock = json.loads(LOCK.read_text())
    source = SourceImage.parse(lock["source_image"])
    code, firmware_variables = firmware_paths()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as temporary:
        work = Path(temporary)
        recipe = work / "recipe"
        recipe_id, hashes = stage_recipe(lock, recipe)
        key = work / "build-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "cobalt-build", "-f", str(key)], check=True)
        seed = build_seed(work, key.with_suffix(".pub").read_text(), recipe_id)
        packer_output = work / "packer"
        variables = {
            "source_url": source.url, "source_sha256": source.sha256,
            "cpus": args.cpus, "memory_mib": args.memory_mib, "accelerator": args.accelerator,
            "firmware_code": str(code), "firmware_vars": str(firmware_variables),
            "seed_iso": str(seed), "ssh_private_key_file": str(key),
            "recipe_directory": str(recipe), "output_directory": str(output),
            "packer_output_directory": str(packer_output),
        }
        variables_path = work / "packer-vars.json"
        variables_path.write_text(json.dumps(variables, indent=2) + "\n")
        template = recipe / "images/build/xenomai-cobalt/image.pkr.hcl"
        subprocess.run(["packer", "validate", f"-var-file={variables_path}", str(template)], env=environment, check=True)
        print(f"Building Cobalt disk; progress: {output / 'packer.log'}", flush=True)
        run_packer(["packer", "build", "-color=false", "-on-error=cleanup", f"-var-file={variables_path}", str(template)],
                   environment, output / "packer.log")
        export_build(output, packer_output, lock, recipe_id, hashes, environment)
    print(output / "build.yaml")
