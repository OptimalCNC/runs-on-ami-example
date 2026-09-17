#!/usr/bin/env python3
"""Check pinned image sources, tools, packages, and kernel configuration."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SHA256 = r"[0-9a-f]{64}"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def match(value, pattern, name):
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, f"invalid {name}")
    return value


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def static_checks(root=ROOT):
    lock = json.loads((root / "images/build/xenomai-cobalt/inputs.lock.json").read_text())
    require(lock["schema_version"] == 1, "unsupported input-lock version")
    publishing_tools = json.loads((root / "images/publish/tools.lock.json").read_text())
    require(publishing_tools["schema_version"] == 1, "unsupported publishing-tool lock version")
    match(lock["kernel"]["sha256"], SHA256, "Linux archive hash")
    match(lock["kernel"]["version"], r"\d+\.\d+\.\d+", "Linux version")
    match(lock["kernel"]["release"], re.escape(lock["kernel"]["version"]) + r"(?:-[a-z0-9.]+)*-xenomai-cobalt",
          "Cobalt kernel release")
    for name, repository, archive in (("kernel", "xenomai/linux-dovetail", "linux-dovetail"),
                                      ("xenomai", "xenomai/xenomai3/xenomai", "xenomai")):
        source = lock[name]
        commit = match(source["commit"], r"[0-9a-f]{40}", name + " source commit")
        match(source["sha256"], SHA256, name + " archive hash")
        url = (f"https://gitlab.com/api/v4/projects/xenomai%2Flinux-dovetail/repository/archive.tar.gz?sha={commit}"
               if name == "kernel" else f"https://gitlab.com/{repository}/-/archive/{commit}/{archive}-{commit}.tar.gz")
        require(source["url"] == url,
                f"{name} source must be the exact upstream commit archive")
    match(lock["xenomai"]["version"], r"3\.\d+\.\d+", "Xenomai version")
    require(lock["xenomai"]["core"] == "cobalt", "image requires the Cobalt core")
    require(lock["xenomai"]["prefix"] == "/usr/xenomai", "Xenomai installation prefix differs")
    require(lock["xenomai"]["allowed_group_gid"] == 4242, "Cobalt access group differs")
    require(file_sha(root / "images/build/xenomai-cobalt/kernel.config") == lock["kernel"]["config_sha256"], "kernel config hash differs")
    require(re.fullmatch(r"https://snapshot\.ubuntu\.com/ubuntu/\d{8}T\d{6}Z/", lock["os"]["snapshot_url"]) is not None,
            "package snapshot must be date-addressed")
    for name, tool in {**lock["tools"], **publishing_tools["tools"]}.items():
        match(tool["version"], r"\d+\.\d+\.\d+(?:\.\d+)?", name + " version")
        match(tool["sha256"], SHA256, name + " hash")
        require(tool["url"].startswith("https://") and "latest" not in tool["url"], f"unlocked download: {name}")
    for name, package in lock["os"]["packages"].items():
        require(bool(package["version"]) and "*" not in package["version"], f"unlocked package: {name}")
        match(package["sha256"], SHA256, name + " hash")
    require(set(lock["os"]["build_only_packages"]) <= set(lock["os"]["packages"]), "unlocked build-only package")
    for name, pin in lock["runner"].items():
        match(pin["version"], r"\d+\.\d+\.\d+", name + " version")
        match(pin["sha256"], SHA256, name + " hash")
        require(pin["url"].startswith("https://github.com/") and f"/v{pin['version']}/" in pin["url"],
                f"runner download must select its locked release: {name}")
    for name in lock["recipe_files"]:
        require((root / name).is_file(), f"recipe file missing: {name}")
    config = (root / "images/build/xenomai-cobalt/kernel.config").read_text()
    for value in ("CONFIG_XENOMAI=y", "CONFIG_DOVETAIL=y", "CONFIG_IRQ_PIPELINE=y", "CONFIG_XENO_OPT_VFILE=y",
                  f'CONFIG_XENO_VERSION_STRING="{lock["xenomai"]["version"]}"', "CONFIG_RUSTC_VERSION=0",
                  "CONFIG_IKCONFIG=y", "CONFIG_IKCONFIG_PROC=y", "CONFIG_ENA_ETHERNET=y", "CONFIG_BLK_DEV_NVME=y",
                  "CONFIG_EXT4_FS=y", "CONFIG_EFI=y", "CONFIG_EFI_STUB=y", 'CONFIG_LOCALVERSION="-xenomai-cobalt"',
                  "# CONFIG_LOCALVERSION_AUTO is not set", "# CONFIG_MODULE_SIG is not set"):
        require(value in config.splitlines(), f"required kernel configuration missing: {value}")
    return lock


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    static_checks()
    print("Pinned image inputs and kernel configuration validated.")


if __name__ == "__main__":
    main()
