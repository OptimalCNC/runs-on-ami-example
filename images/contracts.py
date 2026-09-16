"""The local disk contract shared by Build, Validate, and Publish."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile

import yaml


def sha256(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_yaml(path: Path) -> dict:
    value = yaml.safe_load(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a YAML mapping: {path}")
    return value


def write_yaml(path: Path, value: dict):
    """Replace a result atomically so interrupted work cannot look complete."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            yaml.safe_dump(value, stream, sort_keys=False)
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def digest(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value


@dataclass(frozen=True)
class Compatibility:
    architecture: str
    boot_mode: str
    secure_boot: bool
    minimum_root_volume_gib: int
    ena_support: bool
    runs_on_bootstrap_version: str

    @classmethod
    def parse(cls, value: dict):
        if not isinstance(value, dict):
            raise ValueError("missing image compatibility")
        if (value.get("architecture") != "x86_64" or value.get("boot_mode") != "uefi"
                or value.get("secure_boot") is not False or value.get("ena_support") is not True):
            raise ValueError("image must support x86_64, UEFI without Secure Boot, and ENA")
        size = value.get("minimum_root_volume_gib")
        if type(size) is not int or not 1 <= size <= 16384:
            raise ValueError("invalid minimum root volume size")
        version = value.get("runs_on_bootstrap_version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("missing RunsOn bootstrap version")
        return cls(**{key: value[key] for key in cls.__dataclass_fields__})

    def as_dict(self):
        return asdict(self)


def artifact_file(root: Path, name, label: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError(f"{label} must be relative to build.yaml")
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"{label} must be a file inside the build directory")
    return path


@dataclass(frozen=True)
class BuiltImage:
    record_path: Path
    disk_path: Path
    disk_sha256: str
    disk_size_bytes: int
    recipe_id: str
    manifest_path: Path
    manifest_sha256: str
    manifest: dict
    compatibility: Compatibility

    @classmethod
    def load(cls, path: Path):
        path = Path(path).resolve()
        value = read_yaml(path)
        if value.get("schema_version") != 1 or value.get("kind") != "built-image":
            raise ValueError("expected a version 1 built-image contract")
        recipe = digest(value.get("recipe_id"), "recipe_id")
        disk = value.get("disk", {})
        payload = value.get("payload", {})
        if not isinstance(disk, dict) or disk.get("format") != "raw":
            raise ValueError("only a finalized raw disk can be consumed")
        if not isinstance(payload, dict):
            raise ValueError("missing payload manifest")
        disk_path = artifact_file(path.parent, disk.get("path"), "disk path")
        manifest_path = artifact_file(path.parent, payload.get("manifest"), "manifest path")
        disk_digest = digest(disk.get("sha256"), "disk digest")
        manifest_digest = digest(payload.get("sha256"), "manifest digest")
        compatibility = Compatibility.parse(value.get("compatibility"))
        size = disk.get("size_bytes")
        if (type(size) is not int or size <= 0 or size % (512 * 1024)
                or size != disk_path.stat().st_size
                or size != compatibility.minimum_root_volume_gib * 1024**3):
            raise ValueError("raw disk size differs from the EBS volume contract")
        if sha256(manifest_path) != manifest_digest:
            raise ValueError("payload manifest checksum differs")
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict) or manifest.get("recipe_id") != recipe:
            raise ValueError("payload recipe differs from build record")
        xenomai = manifest.get("xenomai", {})
        if (not isinstance(xenomai, dict) or xenomai.get("core") != "cobalt"
                or xenomai.get("prefix") != "/usr/xenomai"
                or not isinstance(manifest.get("kernel_release"), str)):
            raise ValueError("payload must identify the Cobalt kernel and SDK")
        if sha256(disk_path) != disk_digest:
            raise ValueError("raw disk checksum differs from build record")
        return cls(path, disk_path, disk_digest, size, recipe, manifest_path, manifest_digest, manifest, compatibility)
