"""The local disk contract shared by Build, Validate, and Publish."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile


def sha256(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict):
    """Replace a result atomically so interrupted work cannot look complete."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def digest(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value


def artifact_file(root: Path, name, label: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError(f"{label} must be relative to build.json")
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"{label} must be a file inside the build directory")
    return path


@dataclass(frozen=True)
class BuiltImage:
    disk_path: Path
    disk_sha256: str
    disk_size_bytes: int
    kernel_release: str
    xenomai_version: str

    @classmethod
    def load(cls, path: Path):
        path = Path(path).resolve()
        value = read_json(path)
        if value.get("schema_version") != 2 or value.get("kind") != "built-image":
            raise ValueError("expected a version 2 built-image contract")
        disk = value.get("disk", {})
        if not isinstance(disk, dict) or disk.get("format") != "raw":
            raise ValueError("only a finalized raw disk can be consumed")
        disk_path = artifact_file(path.parent, disk.get("path"), "disk path")
        disk_digest = digest(disk.get("sha256"), "disk digest")
        size = disk.get("size_bytes")
        if type(size) is not int or size != 16 * 1024**3 or size != disk_path.stat().st_size:
            raise ValueError("built image must contain a 16 GiB raw disk")
        release = value.get("kernel_release")
        version = value.get("xenomai_version")
        if not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+-xenomai-cobalt", release):
            raise ValueError("build must identify the Cobalt kernel release")
        if not isinstance(version, str) or not re.fullmatch(r"3\.\d+\.\d+", version):
            raise ValueError("build must identify the Xenomai 3 version")
        if sha256(disk_path) != disk_digest:
            raise ValueError("raw disk checksum differs from build record")
        return cls(disk_path, disk_digest, size, release, version)
