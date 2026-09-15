#!/usr/bin/env python3
"""Select a qualified retained image and verify an application run against EC2."""
import argparse
import dataclasses
import importlib.util
from pathlib import Path

from example import (AMI, BUILD_TAG, EXPIRY_TAG, INSTANCE, OWNER_TAG, PURPOSE_TAG, ROOT, SHA256, Cloud,
                     file_sha, load_bindings, load_cleanup_context, match, parse_time, read_json, require,
                     tags_of, utcnow, write_json)
from qualification import QualificationRun
from runner_instances import adopt_test_instances


@dataclasses.dataclass(frozen=True)
class AcceptedImage:
    repository: str
    account_id: str
    region: str
    ami_id: str
    ami_boot_mode: str
    build_id: str
    recipe_id: str
    kernel_release: str
    expires_at: str
    qualification_run_id: str
    qualification_run_attempt: str
    qualification_url: str
    qualification_sha256: str

    @classmethod
    def parse(cls, record, deployment, *, allow_expired=False):
        require(set(record) == {"schema_version", "image"} and record["schema_version"] == 1, "accepted image schema differs")
        value = record["image"]
        require(isinstance(value, dict), "no qualified retained image has been accepted yet")
        require(set(value) == {field.name for field in dataclasses.fields(cls)}, "accepted image fields differ")
        for field in ("repository", "account_id", "region"):
            require(value[field] == getattr(deployment, field), f"accepted image {field} differs")
        match(value["ami_id"], AMI, "accepted AMI")
        require(value["ami_boot_mode"] in ("uefi", "uefi-preferred"), "accepted AMI must support the qualified UEFI boot")
        match(value["build_id"], r"[1-9]\d*-[1-9]\d*-(one|two)", "accepted image build")
        match(value["recipe_id"], SHA256, "accepted recipe")
        match(value["qualification_sha256"], SHA256, "qualification digest")
        match(value["kernel_release"], r"[A-Za-z0-9.+_-]+", "accepted kernel release")
        match(value["qualification_run_id"], r"[1-9][0-9]*", "accepted qualification run ID")
        match(value["qualification_run_attempt"], r"[1-9][0-9]*", "accepted qualification run attempt")
        require(isinstance(value["qualification_url"], str), "qualification evidence URL must be a string")
        expires_at = parse_time(value["expires_at"])
        require(allow_expired or expires_at > utcnow(), "accepted image has expired; qualify and accept a new image")
        return cls(**value)

    def inspect(self, cloud, minimum_seconds=0):
        images = cloud.call("ec2", "describe-images", {"ImageIds": [self.ami_id], "Owners": [self.account_id]})["Images"]
        require(len(images) == 1, "accepted image is absent or owned by another account")
        image = images[0]
        require(image["ImageId"] == self.ami_id and image["OwnerId"] == self.account_id
                and image["State"] == "available" and not image.get("Public", False)
                and image["Architecture"] == "x86_64" and image.get("BootMode") == self.ami_boot_mode,
                "accepted AMI state or identity differs")
        tags = tags_of(image)
        for key, expected in {OWNER_TAG: self.repository, BUILD_TAG: self.build_id, PURPOSE_TAG: "candidate",
                              EXPIRY_TAG: self.expires_at, "ami-example:recipe-id": self.recipe_id, "ami-example:retain": "true"}.items():
            require(tags.get(key) == expected, f"accepted AMI tag differs: {key}")
        require(minimum_seconds is None or parse_time(self.expires_at).timestamp() - utcnow().timestamp() > minimum_seconds,
                "accepted image expires before the required lifetime has elapsed")
        return image


def selected_record(result, deployment, checksum, evidence_url=""):
    require(result["status"] == "qualified" and result["lifecycle"]["retain"] is True,
            "accept only a qualified retained result")
    require(result["lifecycle"]["cleanup"]["status"] == "passed"
            and result["cloud"]["ami_id"] in result["lifecycle"]["cleanup"]["retained_images"], "qualified image was not retained after cleanup")
    require(result["validation"]["direct_boot"]["status"] == "passed"
            and len(result["validation"]["runs_on"]) == 2
            and all(value["status"] == "passed" for value in result["validation"]["runs_on"]), "image qualification evidence is incomplete")
    require(result["cloud"]["boot_mode"] == "uefi", "accepted image must have qualified with actual UEFI boot")
    execution = result["execution"]
    qualification = QualificationRun.from_result(result)
    value = {"repository": deployment.repository, "account_id": result["cloud"]["account_id"],
             "region": result["cloud"]["region"], "ami_id": result["cloud"]["ami_id"],
             "ami_boot_mode": result["cloud"]["ami_boot_mode"], "build_id": execution["build_id"],
             "recipe_id": result["source"]["recipe_id"], "kernel_release": result["payload"]["kernel_release"],
             "expires_at": result["lifecycle"]["expires_at"], "qualification_sha256": checksum,
             "qualification_run_id": qualification.run_id, "qualification_run_attempt": qualification.run_attempt,
             "qualification_url": evidence_url}
    record = {"schema_version": 1, "image": value}
    AcceptedImage.parse(record, deployment)
    return record


def prepare(deployment, record, build):
    execution = QualificationRun(build)
    accepted = AcceptedImage.parse(record, deployment)
    return {"build_id": execution.build_id, "region": accepted.region,
            "ami_id": accepted.ami_id, "recipe_id": accepted.recipe_id, "kernel_release": accepted.kernel_release,
            "role_arn": deployment.controller_role_arn, "environment": deployment.environment}


def verify(cloud, accepted, smoke, build, instance_id):
    dispatch = QualificationRun(build).build_id
    expected_instance = match(instance_id, INSTANCE, "application instance")
    require(smoke["ctest_passed"], "Cobalt compile/run failed")
    identity = smoke["identity"]
    require(identity["identity"]["instanceId"] == expected_instance, "expected instance and guest identities differ")
    for guest in (identity, smoke["environment"]):
        require(guest["recipe_id"] == accepted.recipe_id and guest["kernel_release"] == accepted.kernel_release,
                "running recipe or kernel differs from accepted image")
        require(guest["xenomai"]["core"] == "cobalt", "application did not run on Cobalt")
        for key, expected in {"imageId": accepted.ami_id, "accountId": accepted.account_id,
                              "region": accepted.region, "instanceType": cloud.deployment.instance_type}.items():
            require(guest["identity"][key] == expected, f"guest {key} differs")
        require(guest["identity"]["instanceId"] == identity["identity"]["instanceId"], "instance changed between steps")
        require(guest["stage"] == "a" and guest["sentinel_absent_at_start"] is True
                and guest["sentinel"] == f"/var/tmp/ami-example-{dispatch}-sentinel", "fresh instance sentinel differs")
    require(smoke["environment"].get("environment_passed") is True, "Cobalt environment did not persist between steps")
    observed = adopt_test_instances(cloud, dispatch, [expected_instance], image=accepted.inspect(cloud),
                                    expiry=accepted.expires_at, instance_type=cloud.deployment.instance_type)
    require(len(observed) == 1 and observed[0]["InstanceId"] == expected_instance, "controller and guest instance identities differ")
    require(observed[0].get("CurrentInstanceBootMode") == "uefi", "test runner boot mode differs")
    return {"status": "passed", "build_id": dispatch, "ami_id": accepted.ami_id, "recipe_id": accepted.recipe_id,
            "kernel_release": accepted.kernel_release, "instance_id": observed[0]["InstanceId"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    tasks = parser.add_subparsers(dest="task", required=True)
    for task in ("accept", "prepare", "inspect", "verify"):
        command = tasks.add_parser(task)
        command.add_argument("--deployment", required=True)
        command.add_argument("--record", required=True, type=Path,
                             help="accepted image destination" if task == "accept" else "accepted image record")
        if task == "accept":
            command.add_argument("--result", required=True)
            command.add_argument("--evidence-url", default="")
        else:
            command.add_argument("--output", required=True, type=Path)
        if task in ("prepare", "verify"):
            command.add_argument("--build-id", required=True)
        if task == "inspect":
            command.add_argument("--minimum-seconds", type=int, default=0)
        if task == "verify":
            command.add_argument("--smoke", required=True, type=Path)
            command.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    deployment = (load_bindings(args.deployment) if args.task in ("prepare", "verify")
                  else load_cleanup_context(args.deployment))
    if args.task == "accept":
        write_json(args.record, selected_record(read_json(args.result), deployment, file_sha(Path(args.result)), args.evidence_url))
        return
    record = read_json(args.record)
    if args.task == "prepare":
        write_json(args.output, prepare(deployment, record, args.build_id))
        return
    accepted = AcceptedImage.parse(record, deployment)
    cloud = Cloud(deployment)
    if args.task == "inspect":
        require(args.minimum_seconds >= 0, "minimum image lifetime must be nonnegative")
        write_json(args.output, accepted.inspect(cloud, args.minimum_seconds))
    elif args.task == "verify":
        spec = importlib.util.spec_from_file_location("verify_results", ROOT / "scripts/verify-run-results.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        write_json(args.output, verify(cloud, accepted, module.read_smoke(args.smoke), args.build_id, args.instance_id))


if __name__ == "__main__":
    main()
