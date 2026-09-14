#!/usr/bin/env python3
"""Run Packer on a pinned stock RunsOn controller and publish a candidate record."""
import argparse
import dataclasses
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

from example import (ROOT, Cloud, CobaltIdentity, file_sha, load_deployment, match, read_json, recipe,
                     EXPIRY_TAG, RUNS_ON_TAG, InvalidInput, require, resource_tags, run, tags_of, timestamp, utcnow, write_json)
from preflight import inspect_deployment


def controller_identity(cloud):
    spec = importlib.util.spec_from_file_location("guest_report", ROOT / "images/common/guest-report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = module.metadata()
    observed = cloud.instance(metadata["instanceId"])
    d = cloud.deployment
    require(observed["ImageId"] == d.controller_ami.id and observed["InstanceType"] == d.instance_type,
            "controller must run on the independently pinned stock AMI and qualified type")
    require(tags_of(observed).get(RUNS_ON_TAG) == d.repository, "configure the RunsOn stack ownership marker first")
    require(observed.get("CurrentInstanceBootMode") == d.controller_ami.effective_boot_mode, "controller boot mode differs")
    expected = d.controller_ami.inventory
    actual = json.loads(run(["sudo", "python3", str(ROOT / "images/common/inventory.py")]))
    for key in ("os_version", "packages_sha256", "runner_version", "runner_listener_sha256", "bootstrap_files", "snap_hashes", "secure_boot"):
        require(actual[key] == expected[key], f"controller inventory differs: {key}")
    return {"instance_id": observed["InstanceId"], "ami_id": observed["ImageId"], "inventory": actual}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True, help="resolved deployment manifest")
    parser.add_argument("--build-id", required=True, help="unique execution ID: NUMBER-ATTEMPT-one, -two, or -stock")
    parser.add_argument("--output", type=Path, required=True, help="new artifact directory for this build")
    parser.add_argument("--config-output", type=Path, help="image configuration JSON (default: OUTPUT/image-config.json)")
    parser.add_argument("--retain", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    build = match(args.build_id, r"[1-9][0-9]*-[1-9][0-9]*-(one|two|stock)", "build ID")
    variant = build.rsplit("-", 1)[1]
    destination = args.output.resolve()
    config_output = args.config_output or destination / "image-config.json"
    d = load_deployment(args.deployment)
    lock = read_json(ROOT / "images/xenomai-cobalt/inputs.lock.json")
    recipe_id, hashes = recipe(lock, d)
    if not args.execute:
        print("Would launch one on-demand Packer builder; a full build also creates an AMI and EBS snapshots.")
        print("Review the deployment and cost plan before supplying --execute.")
        return
    tracked = [*hashes, "images/xenomai-cobalt/inputs.lock.json"]
    run(["git", "ls-files", "--error-unmatch", *tracked], cwd=ROOT)
    require(not run(["git", "status", "--porcelain", "--", *tracked], cwd=ROOT), "commit all locked build inputs first")
    commit = run(["git", "rev-parse", "HEAD"], cwd=ROOT)
    destination.mkdir(parents=True, exist_ok=False)
    d = load_deployment(d.snapshot(destination / "deployment"))
    cloud = Cloud(d)
    preflight = inspect_deployment(cloud)
    write_json(destination / "preflight.json", preflight)
    controller = controller_identity(cloud)
    write_json(destination / "controller.json", controller)
    expiry = timestamp(utcnow() + dt.timedelta(hours=6))
    stage = destination / "work" / "recipe"
    stage.mkdir(parents=True, exist_ok=False)
    for name in [*hashes, "images/xenomai-cobalt/inputs.lock.json"]:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    shutil.copy2(d.source_ami.inventory_path, stage / "source-inventory.json")
    write_json(stage / "recipe.json", {"recipe_id": recipe_id})
    candidate_tags = {v["Key"]: v["Value"] for v in resource_tags(d, build, "candidate", expiry)}
    candidate_tags.update({"ami-example:recipe-id": recipe_id, "ami-example:retain": str(args.retain).lower()})
    resource_name = f"ami-example-{d.repository.replace('/', '-')[:48]}-{build}"
    key = cloud.call("ec2", "create-key-pair", {
        "KeyName": resource_name, "KeyType": "ed25519",
        "TagSpecifications": [{"ResourceType": "key-pair", "Tags": resource_tags(d, build, "builder", expiry)}],
    })
    private_key = stage.parent / "builder-key.pem"
    descriptor = os.open(private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(key["KeyMaterial"])
    variables = {"region": d.region, "source_ami": d.source_ami.id,
                 "instance_type": d.builder_instance_type, "subnet_id": d.subnet_id, "security_group_id": d.security_group_id,
                 "builder_profile_name": d.builder_profile_name,
                 "root_device_name": preflight["source_ami"]["RootDeviceName"], "root_volume_gib": d.root_volume_gib,
                 "ami_name": resource_name,
                 "recipe_directory": str(stage), "output_directory": str(destination),
                 "associate_public_ip_address": not d.private, "stock_only": variant == "stock",
                 "ssh_keypair_name": key["KeyName"], "ssh_private_key_file": str(private_key),
                 "build_tags": {v["Key"]: v["Value"] for v in resource_tags(d, build, "builder", expiry)},
                 "candidate_tags": candidate_tags}
    variable_file = destination / "packer-vars.json"
    write_json(variable_file, variables)
    template = "images/xenomai-cobalt/image.pkr.hcl"
    locations = {}
    try:
        subprocess.run(["packer", "validate", f"-var-file={variable_file}", template], check=True, cwd=ROOT)
        with (destination / "packer.log").open("w") as log:
            subprocess.run(["packer", "build", "-color=false", "-on-error=cleanup", f"-var-file={variable_file}", template],
                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=5400, check=True)
        if variant == "stock":
            write_json(destination / "stock-result.json", {"status": "passed", "build_id": build,
                                                        "controller": controller["instance_id"], "recipe_id": recipe_id})
            write_json(config_output, {"build_id": build, "recipe_id": recipe_id})
            return
        image = read_json(destination / "image-manifest.json")
        require(image["recipe_id"] == recipe_id, "Packer returned the wrong recipe")
        expected_cobalt = CobaltIdentity.parse({key: lock["xenomai"][key] for key in ("version", "core", "prefix")})
        require(CobaltIdentity.parse(image.get("xenomai")) == expected_cobalt, "Packer returned the wrong Cobalt payload")
        require(image["kernel_release"] == lock["kernel"]["release"],
                "Packer returned the wrong Cobalt kernel release")
        manifest = read_json(destination / "packer-manifest.json")
        artifact = manifest["builds"][-1]["artifact_id"].split(":")
        require(len(artifact) == 2 and artifact[0] == d.region, "unexpected Packer AMI artifact")
        ami_id = artifact[1]
        candidate = cloud.call("ec2", "describe-images", {"ImageIds": [ami_id], "Owners": [d.account_id]})["Images"][0]
        require(candidate["State"] == "available", "candidate AMI is not available")
        candidate_identity = dataclasses.replace(d.source_ami, id=ami_id, owner=d.account_id,
                                                 boot_mode=candidate.get("BootMode", "legacy-bios"))
        require(candidate_identity.effective_boot_mode == d.source_ami.effective_boot_mode, "candidate boot behavior differs")
        from preflight import inspect_ami
        inspect_ami(cloud, candidate_identity)
        snapshots = [m["Ebs"]["SnapshotId"] for m in candidate["BlockDeviceMappings"] if "Ebs" in m]
        if args.retain:
            expiry = timestamp(utcnow() + dt.timedelta(hours=d.retain_hours))
            cloud.call("ec2", "create-tags", {"Resources": [ami_id, *snapshots], "Tags": [{"Key": EXPIRY_TAG, "Value": expiry}]})
        input_uri = cloud.retain(destination / "inputs.tar", f"{build}/inputs.tar")
        locations["inputs.tar"] = input_uri
        result = {
            "schema_version": 1, "status": "candidate",
            "source": {"recipe_id": recipe_id, "recipe_commit": commit, "recipe_files": hashes,
                       "input_lock_sha256": file_sha(ROOT / "images/xenomai-cobalt/inputs.lock.json"),
                       "parent_ami": d.source_ami.record()},
            "payload": image,
            "cloud": {"account_id": d.account_id, "region": d.region, "ami_id": ami_id, "snapshot_ids": snapshots,
                      "architecture": "x86_64", "boot_mode": d.source_ami.effective_boot_mode, "instance_type": d.instance_type},
            "execution": {"build_id": build,
                          "controller_instance_id": controller["instance_id"], "runs_on_version": d.require_runs_on().version,
                          "tool_versions": {name: pin["version"] for name, pin in lock["tools"].items()},
                          "inherited_runner_version": image["parent_inventory"]["runner_version"],
                          "bootstrap_files": image["parent_inventory"]["bootstrap_files"]},
            "validation": {"direct_boot": {"status": "pending"}, "runs_on": [], "reproducibility": {"status": "pending"}},
            "lifecycle": {"created_at": candidate["CreationDate"], "expires_at": expiry, "retain": args.retain,
                          "artifact_locations": [input_uri, f"s3://{d.artifact_bucket}/{d.repository}/{build}/"],
                          "cleanup": {"status": "pending"}},
        }
        result["cloud"]["ami_boot_mode"] = candidate_identity.boot_mode
        write_json(destination / "image-result.json", result)
        write_json(config_output, {"ami_id": ami_id, "kernel_release": image["kernel_release"], "recipe_id": recipe_id,
                                   "build_id": build, "label_a": d.label(f"{build}-a", ami_id),
                                   "label_b": d.label(f"{build}-b", ami_id)})
    finally:
        failures = []
        private_key.unlink(missing_ok=True)
        try:
            cloud.call("ec2", "delete-key-pair", {"KeyPairId": key["KeyPairId"]})
        except Exception as error:
            failures.append(f"temporary key cleanup: {error}")
        # Durable diagnostics are attempted before any cleanup is permitted to remove images.
        diagnostics = [*destination.glob("*"), *(destination / "deployment").rglob("*")]
        for path in sorted(diagnostics):
            relative = path.relative_to(destination).as_posix()
            if path.is_file() and relative not in locations:
                try:
                    locations[relative] = cloud.retain(path, f"{build}/{relative}")
                except (subprocess.SubprocessError, OSError, InvalidInput) as error:
                    failures.append(f"{relative}: {error}")
        write_json(destination / "artifact-index.json", locations)
        cloud.retain(destination / "artifact-index.json", f"{build}/artifact-index.json")
        require(not failures, f"artifact retention failed: {failures}")


if __name__ == "__main__":
    main()
