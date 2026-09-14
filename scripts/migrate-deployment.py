#!/usr/bin/env python3
"""Archive a legacy checkout's deployment data and convert it locally, without cloud changes."""
import argparse
import copy
import dataclasses
import os
from pathlib import Path
import subprocess

from example import file_sha, load_deployment, read_json, require, write_json


LEGACY_FILES = (
    "infra/deployment.json", "infra/deployment.example.json",
    "infra/runs-on/deployment.json", "infra/runs-on/policy-overlay.json",
    "infra/resources.json", "accepted-image.json", "README.md", "infra/README.md",
    "docs/operations.md", "01-runs-on-custom-ami-example-plan.md", "docs/plans/custom-ami-example.md",
    "infra/example.auto.tfvars.json", "infra/operator/example.auto.tfvars.json",
    "infra/terraform.tfstate", "infra/terraform.tfstate.backup", "infra/operator/terraform.tfstate",
)


def preserve(source, target):
    data = source.read_bytes()
    if target.exists():
        require(target.read_bytes() == data, f"refusing to overwrite different archived data: {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    require(file_sha(source) == file_sha(target), f"archive verification failed: {source}")


def archive_legacy(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    legacy = read_json(source / "infra/deployment.json")
    archive = output / "archive/legacy"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    paths = set(LEGACY_FILES)
    for name in ("source_ami", "controller_ami"):
        path = Path(legacy[name]["inventory_file"])
        require(not path.is_absolute() and (source / path).resolve().is_relative_to(source),
                "legacy inventory must belong to the selected source directory")
        paths.add(str(path))
    entries = []
    for name in sorted(paths):
        path = source / name
        if path.is_file():
            preserve(path, archive / name)
            entries.append({"path": name, "sha256": file_sha(path), "bytes": path.stat().st_size})
    if (source / "archive.json").is_file():
        commit = read_json(source / "archive.json")["source_commit"]
    else:
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    record = {"source_commit": commit, "files": entries}
    # An existing archive remains authoritative, including extra evidence it contains.
    if (archive / "archive.json").exists():
        existing = read_json(archive / "archive.json")
        require(existing["source_commit"] == commit, "archive belongs to a different source commit")
        for entry in existing["files"]:
            require(file_sha(archive / entry["path"]) == entry["sha256"], "existing archive was changed")
    else:
        write_json(archive / "archive.json", record)
    return archive


def convert(archive, output, name_prefix=None):
    from deployment_config import load_spec

    archive, output = Path(archive).resolve(), Path(output).resolve()
    legacy = read_json(archive / "infra/deployment.json")
    terraform_path = archive / "infra/example.auto.tfvars.json"
    terraform = read_json(terraform_path) if terraform_path.exists() else {}
    prefix = name_prefix or terraform.get("name_prefix")
    require(prefix is not None, "supply --name-prefix when the legacy Terraform inputs are unavailable")
    service = read_json(archive / "infra/runs-on/deployment.json")
    bindings = {key: value for key, value in legacy.items() if key not in ("source_ami", "controller_ami")}
    fields = ("account_id", "region", "repository", "environment", "subnet_id", "instance_type",
              "builder_instance_type", "root_volume_gib", "parent_root_volume_gib", "private",
              "retain_hours", "deadlines")
    spec = {"schema_version": 1, "name_prefix": prefix, **{key: legacy[key] for key in fields},
            "source_ami": {key: legacy["source_ami"][key] for key in ("id", "owner")},
            "controller_ami": {key: legacy["controller_ami"][key] for key in ("id", "owner")},
            "runs_on": {"mode": "existing", "stack_name": service["stack_name"],
                        "environment": legacy["runs_on"]["environment"],
                        "vpc_cidr": service["parameters"]["VpcCidrBlock"],
                        "private": service["parameters"]["Private"] == "true"},
            "artifact_retention_days": terraform.get("artifact_retention_days", 365),
            "github_oidc_subject_prefix": terraform.get("github_oidc_subject_prefix"),
            "infrastructure": {key: value for key, value in terraform.items()
                               if key.startswith("existing_") or key in ("operator_user_arn", "artifact_bucket_name")}}
    for name in ("spec.json", "bindings.json", "manifest.json", "migration.json"):
        require(not (output / name).exists(), f"migration output already exists: {output / name}")
    write_json(output / "spec.json", spec)
    load_spec(output / "spec.json")
    bindings["parent_selections"] = {role: spec[role + "_ami"] for role in ("source", "controller")}
    write_json(output / "bindings.json", bindings)
    manifest = copy.deepcopy(legacy)
    for role in ("source", "controller"):
        original = legacy[role + "_ami"]
        inventory = archive / original["inventory_file"]
        require(file_sha(inventory) == original["inventory_sha256"], "legacy inventory digest differs")
        target = output / "parents" / (role + "-inventory.json")
        preserve(inventory, target)
        manifest[role + "_ami"]["inventory_file"] = str(target.relative_to(output))
        write_json(output / "parents" / (role + ".json"), {
            **manifest[role + "_ami"], "inventory_file": target.name,
        })
    write_json(output / "manifest.json", manifest)
    resolved = dataclasses.asdict(load_deployment(output / "manifest.json"))
    for name in ("source_ami", "controller_ami"):
        resolved[name]["inventory_file"] = legacy[name]["inventory_file"]
    require(resolved == legacy, "converted effective deployment differs from the legacy deployment")
    accepted = archive / "accepted-image.json"
    if accepted.exists():
        preserve(accepted, output / "state/accepted-image.json")
    report = {"schema_version": 1, "effective_configuration_unchanged": True,
              "source_commit": read_json(archive / "archive.json")["source_commit"],
              "legacy_manifest_sha256": file_sha(archive / "infra/deployment.json"),
              "spec_sha256": file_sha(output / "spec.json"),
              "bindings_sha256": file_sha(output / "bindings.json"),
              "manifest_sha256": file_sha(output / "manifest.json"),
              "accepted_record_preserved": accepted.exists(),
              "cloud_changes": False, "terraform_state_changes": False,
              "image_rebuilt_or_requalified": False, "configuration_published": False}
    write_json(output / "migration.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="legacy checkout or its byte-preserving archive")
    parser.add_argument("--output", type=Path, required=True, help="private deployment directory")
    parser.add_argument("--name-prefix", help="existing resource name prefix if not in legacy Terraform inputs")
    args = parser.parse_args()
    archive = archive_legacy(args.source_dir, args.output)
    convert(archive, args.output, args.name_prefix)
    print("Archived original data and verified converted deployment parity. No cloud or live state changes.")


if __name__ == "__main__":
    main()
