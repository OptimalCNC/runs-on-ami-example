#!/usr/bin/env python3
"""Credential-free recipe checks, with an optional strict deployment boundary."""
import argparse
import re

from example import ROOT, SHA256, CobaltIdentity, file_sha, load_deployment, match, read_json, recipe, require, write_json


def static_checks(root=ROOT):
    lock = read_json(root / "images/xenomai-cobalt/inputs.lock.json")
    require(lock["schema_version"] == 1, "unsupported input-lock version")
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
    CobaltIdentity.parse({"version": lock["xenomai"]["version"], "core": lock["xenomai"]["core"],
                          "prefix": lock["xenomai"]["prefix"]})
    require(lock["xenomai"]["allowed_group_gid"] == 4242, "Cobalt access group differs")
    require(file_sha(root / "images/xenomai-cobalt/kernel.config") == lock["kernel"]["config_sha256"], "kernel config hash differs")
    require(re.fullmatch(r"https://snapshot\.ubuntu\.com/ubuntu/\d{8}T\d{6}Z/", lock["os"]["snapshot_url"]) is not None,
            "package snapshot must be date-addressed")
    for name, tool in lock["tools"].items():
        match(tool["version"], r"\d+\.\d+\.\d+(?:\.\d+)?", name + " version")
        match(tool["sha256"], SHA256, name + " hash")
        require(tool["url"].startswith("https://") and "latest" not in tool["url"], f"unlocked download: {name}")
    for name, package in lock["os"]["packages"].items():
        require(bool(package["version"]) and "*" not in package["version"], f"unlocked package: {name}")
        match(package["sha256"], SHA256, name + " hash")
    for name in lock["recipe_files"]:
        require((root / name).is_file(), f"recipe file missing: {name}")
    config = (root / "images/xenomai-cobalt/kernel.config").read_text()
    for value in ("CONFIG_XENOMAI=y", "CONFIG_DOVETAIL=y", "CONFIG_IRQ_PIPELINE=y", "CONFIG_XENO_OPT_VFILE=y",
                  f'CONFIG_XENO_VERSION_STRING="{lock["xenomai"]["version"]}"', "CONFIG_RUSTC_VERSION=0",
                  "CONFIG_IKCONFIG=y", "CONFIG_IKCONFIG_PROC=y", "CONFIG_ENA_ETHERNET=y", "CONFIG_BLK_DEV_NVME=y",
                  "CONFIG_EXT4_FS=y", "CONFIG_EFI=y", "CONFIG_EFI_STUB=y", 'CONFIG_LOCALVERSION="-xenomai-cobalt"',
                  "# CONFIG_LOCALVERSION_AUTO is not set", "# CONFIG_MODULE_SIG is not set"):
        require(value in config.splitlines(), f"required kernel configuration missing: {value}")
    return lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment")
    parser.add_argument("--output")
    args = parser.parse_args()
    lock = static_checks()
    if args.deployment:
        deployment = load_deployment(args.deployment)
        identity, files = recipe(lock, deployment)
        result = {"recipe_id": identity, "recipe_files": files}
        if args.output:
            write_json(args.output, result)
        print(f"Deployment and recipe validated: {identity}")
    else:
        print("Static locked inputs validated; account-specific deployment and inventories are still required.")


if __name__ == "__main__":
    main()
