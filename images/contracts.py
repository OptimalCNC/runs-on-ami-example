"""The local disk contract shared by Build, Validate, and Publish."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile


def sha256(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


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
        disk = value["disk"]
        disk_path = path.parent / disk["path"]
        size = disk_path.stat().st_size
        if size != 16 * 1024**3:
            raise ValueError("built image must contain a 16 GiB raw disk")
        if sha256(disk_path) != disk["sha256"]:
            raise ValueError("raw disk checksum differs from build record")
        return cls(disk_path, disk["sha256"], size, value["kernel_release"], value["xenomai_version"])
