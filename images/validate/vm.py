"""Firmware and temporary login configuration for local image VMs."""
from pathlib import Path
import subprocess

import yaml


def firmware_paths() -> tuple[Path, Path]:
    """Use a matching OVMF pair without enforced Secure Boot."""
    directory = Path("/usr/share/OVMF")
    for suffix in ("_4M", ""):
        code = directory / f"OVMF_CODE{suffix}.fd"
        variables = directory / f"OVMF_VARS{suffix}.fd"
        if code.is_file() and variables.is_file():
            return code, variables
    raise ValueError("install ovmf: matching OVMF_CODE_4M.fd and OVMF_VARS_4M.fd are required")


def create_seed(destination: Path, user: str, public_key: str, instance_id: str) -> Path:
    """Authorize temporary SSH access without changing image groups or limits."""
    user_data = destination / "user-data"
    user_data.write_text("#cloud-config\n" + yaml.safe_dump({
        "users": [{"name": user, "ssh_authorized_keys": [public_key.strip()]}],
        "ssh_pwauth": False,
        "package_update": False,
        "package_upgrade": False,
    }))
    metadata = destination / "meta-data"
    metadata.write_text(yaml.safe_dump({"instance-id": instance_id, "local-hostname": "cobalt-validation"}))
    seed = destination / "seed.img"
    subprocess.run(["cloud-localds", str(seed), str(user_data), str(metadata)], check=True)
    return seed
