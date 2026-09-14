#!/usr/bin/env python3
"""Read immutable candidate evidence and prepare it for an independent qualification."""
import argparse
import dataclasses
import json
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from example import (AMI, BUILD_TAG, EXPIRY_TAG, OWNER_TAG, PURPOSE_TAG, ROOT, SHA256,
                     AmiIdentity, Cloud, CobaltIdentity, ImageIdentity, file_sha, load_deployment, match, parse_time, read_json, recipe,
                     require, run, tags_of, utcnow, write_json)
from preflight import inspect_ami, inspect_instance_type, inspect_root_volume
from qualification import QualificationRun


@dataclasses.dataclass(frozen=True)
class VersionedArtifact:
    bucket: str
    key: str
    version: str

    @classmethod
    def parse(cls, uri, deployment):
        require(isinstance(uri, str), "candidate URI must be a string")
        parsed = urlparse(uri)
        query = parse_qs(parsed.query, keep_blank_values=True)
        key = unquote(parsed.path.lstrip("/"))
        require(parsed.scheme == "s3" and parsed.netloc == deployment.artifact_bucket and not parsed.fragment,
                "candidate evidence must use the designated S3 bucket")
        require(key.startswith(deployment.repository + "/")
                and all(part not in (".", "..") for part in key.split("/")) and str(PurePosixPath(key)) == key,
                "candidate evidence must stay under the repository prefix")
        require(set(query) == {"versionId"} and len(query["versionId"]) == 1
                and query["versionId"][0] not in ("", "null"), "candidate evidence needs one immutable S3 VersionId")
        return cls(parsed.netloc, key, query["versionId"][0])


def download_json(cloud, uri, target):
    reference = VersionedArtifact.parse(uri, cloud.deployment)
    require(reference.key.endswith(".json"), "candidate evidence must be JSON")
    cloud.artifacts()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    response = json.loads(run(["aws", "s3api", "get-object", "--bucket", reference.bucket, "--key", reference.key,
                               "--version-id", reference.version, "--expected-bucket-owner", cloud.deployment.account_id,
                               "--region", cloud.deployment.region, "--no-cli-pager", str(target)], timeout=90))
    require(response.get("VersionId") == reference.version, "S3 returned a different candidate evidence version")
    return read_json(target)


def validate_source(result, deployment, *, for_launch=True):
    require(result.get("schema_version") == 1 and result.get("status") == "candidate", "retain a candidate creation record before qualification")
    execution = result["execution"]
    original = QualificationRun.from_result({"execution": execution, "validation": {}})
    require(result["cloud"]["account_id"] == deployment.account_id and result["cloud"]["region"] == deployment.region,
            "source candidate account or region differs")
    match(result["cloud"]["ami_id"], AMI, "source candidate AMI")
    match(result["source"]["recipe_commit"], r"[0-9a-f]{40}", "source build commit")
    match(execution["controller_instance_id"], r"i-[0-9a-f]{17}", "source controller instance")
    match(result["source"]["recipe_id"], SHA256, "candidate recipe digest")
    require(result["source"]["recipe_id"] == result["payload"]["recipe_id"], "candidate recipe identities differ")
    match(result["source"]["input_lock_sha256"], SHA256, "candidate input lock digest")
    require(isinstance(result["source"]["recipe_files"], dict) and result["source"]["recipe_files"], "candidate recipe files are missing")
    for checksum in result["source"]["recipe_files"].values():
        match(checksum, SHA256, "candidate recipe file digest")
    cobalt = CobaltIdentity.parse(result["payload"]["xenomai"])
    if for_launch:
        parent = AmiIdentity.parse(result["source"]["parent_ami"], "candidate parent")
        require(parent.semantic_identity(deployment.region) == deployment.source_ami.semantic_identity(deployment.region),
                "candidate parent identity differs")
        require(execution["runs_on_version"] == deployment.require_runs_on().version, "candidate RunsOn service version differs")
        require(result["cloud"]["instance_type"] == deployment.instance_type, "candidate runtime instance type differs")
        lock = read_json(ROOT / "images/xenomai-cobalt/inputs.lock.json")
        recipe_id, hashes = recipe(lock, deployment)
        require(result["source"]["recipe_id"] == recipe_id and result["source"]["recipe_files"] == hashes
                and result["source"]["input_lock_sha256"] == file_sha(ROOT / "images/xenomai-cobalt/inputs.lock.json"),
                "candidate recipe differs from the configured locked inputs")
        require(result["payload"]["kernel_release"] == lock["kernel"]["release"], "candidate kernel release differs")
        require(cobalt == CobaltIdentity.parse({name: lock["xenomai"][name] for name in ("version", "core", "prefix")}),
                "candidate Cobalt identity differs")
    require(result["lifecycle"]["retain"] is True and result["cloud"]["architecture"] == "x86_64"
            and result["cloud"]["boot_mode"] == "uefi", "candidate must be retained for a UEFI runtime")
    for field in ("config_sha256", "packages_sha256", "initramfs_sha256"):
        match(result["payload"][field], SHA256, f"candidate {field}")
    inputs = [uri for uri in result["lifecycle"]["artifact_locations"] if urlparse(uri).path.endswith("/inputs.tar")]
    require(len(inputs) == 1, "candidate must identify its retained input archive")
    archive = VersionedArtifact.parse(inputs[0], deployment)
    expected_keys = {f"{deployment.repository}/{prefix}{original.build_id}/inputs.tar" for prefix in ("", "reports/")}
    require(archive.key in expected_keys, "input archive belongs to another source build")
    return original


def inspect_candidate(cloud, result, minimum_seconds=0, *, for_launch=True):
    d = cloud.deployment
    require(result["cloud"]["account_id"] == d.account_id and result["cloud"]["region"] == d.region,
            "candidate cloud target differs")
    identity = ImageIdentity.parse({"id": result["cloud"]["ami_id"], "owner": d.account_id,
                                    "architecture": result["cloud"]["architecture"],
                                    "boot_mode": result["cloud"]["ami_boot_mode"]}, "candidate")
    require(identity.boot_mode in ("uefi", "uefi-preferred") and result["cloud"]["boot_mode"] == "uefi",
            "candidate must retain its qualified UEFI boot behavior")
    image = inspect_ami(cloud, identity)
    if for_launch:
        inspect_instance_type(cloud, d.instance_type, (identity,), d.vcpus)
        inspect_root_volume(image, d.root_volume_gib)
    expected = {OWNER_TAG: d.repository, BUILD_TAG: result["execution"]["build_id"], PURPOSE_TAG: "candidate",
                EXPIRY_TAG: result["lifecycle"]["expires_at"], "ami-example:recipe-id": result["source"]["recipe_id"],
                "ami-example:retain": "true"}
    require(result["lifecycle"]["retain"] is True and all(tags_of(image).get(key) == value for key, value in expected.items()),
            "retained candidate ownership, recipe, retention or expiry differs")
    require(parse_time(image["CreationDate"]) == parse_time(result["lifecycle"]["created_at"]), "candidate creation time differs")
    snapshot_ids = [mapping["Ebs"]["SnapshotId"] for mapping in image["BlockDeviceMappings"] if "Ebs" in mapping]
    require(snapshot_ids and sorted(snapshot_ids) == sorted(result["cloud"]["snapshot_ids"]), "candidate snapshot identities differ")
    require(minimum_seconds is None or parse_time(result["lifecycle"]["expires_at"]).timestamp() - utcnow().timestamp() > minimum_seconds,
            "candidate expiry does not cover qualification")
    return image


def prepare(cloud, context: QualificationRun, source_uri, output, config_output=None, minimum_seconds=0):
    d = cloud.deployment
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=False)
    result = download_json(cloud, source_uri, destination / "source-image-result.json")
    source = validate_source(result, d)
    require(source.build_id != context.build_id, "retained qualification requires a separate execution identity")
    require(minimum_seconds >= 0, "minimum remaining lifetime must be nonnegative")
    inspect_candidate(cloud, result, minimum_seconds)
    result["validation"] = {"qualification": dataclasses.asdict(context), "direct_boot": {"status": "pending"},
                            "runs_on": [], "reproducibility": {"status": "pending"}}
    result["lifecycle"]["artifact_locations"].append(source_uri)
    result["lifecycle"]["cleanup"] = {"status": "pending"}
    write_json(destination / "image-result.json", result)
    cloud.retain(destination / "image-result.json", f"{context.build_id}/qualification/image-result.json")
    config = {"build_id": context.build_id, "ami_id": result["cloud"]["ami_id"], "recipe_id": result["source"]["recipe_id"],
              "kernel_release": result["payload"]["kernel_release"],
              "label_a": d.label(context.build_id + "-a", result["cloud"]["ami_id"]),
              "label_b": d.label(context.build_id + "-b", result["cloud"]["ami_id"])}
    write_json(config_output or destination / "image-config.json", config)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preparation = subparsers.add_parser("prepare", help="prepare a retained candidate for qualification")
    preparation.add_argument("--deployment", required=True, help="resolved deployment manifest")
    preparation.add_argument("--build-id", required=True)
    preparation.add_argument("--source-uri", required=True, help="versioned S3 URI of the candidate creation record")
    preparation.add_argument("--output", type=Path, required=True)
    preparation.add_argument("--config-output", type=Path, help="image configuration JSON (default: OUTPUT/image-config.json)")
    preparation.add_argument("--minimum-seconds", type=int, default=0, help="required remaining candidate lifetime")
    args = parser.parse_args(argv)
    context = QualificationRun.parse(args.build_id)
    prepare(Cloud(load_deployment(args.deployment)), context, args.source_uri, args.output, args.config_output, args.minimum_seconds)


if __name__ == "__main__":
    main()
