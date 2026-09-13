#!/usr/bin/env python3
"""Select a qualified retained image and run read-only admission for later jobs."""
import argparse
import dataclasses
import importlib.util
import os
from pathlib import Path
import time

from example import (AMI, BUILD_TAG, EXPIRY_TAG, OWNER_TAG, PURPOSE_TAG, ROOT, SHA256, Cloud,
                     build_id, file_sha, load_deployment, match, outputs, parse_time,
                     read_json, require, tags_of, timestamp, utcnow, write_json)
from qualification import QualificationRun


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
        require(value["qualification_url"] == f"https://github.com/{deployment.repository}/actions/runs/{value['qualification_run_id']}/attempts/{value['qualification_run_attempt']}",
                "qualification URL differs from qualification run")
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
                "accepted image expires before a registration deadline and smoke test can finish")
        return image


def selected_record(result, deployment, checksum):
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
             "qualification_url": f"https://github.com/{deployment.repository}/actions/runs/{qualification.run_id}/attempts/{qualification.run_attempt}"}
    record = {"schema_version": 1, "image": value}
    AcceptedImage.parse(record, deployment)
    return record


def prepare(deployment, record):
    require(os.environ.get("CLOUD_ENABLED") == "true", "Cloud execution is disabled")
    require(os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch" and os.environ["GITHUB_REF"] == "refs/heads/main",
            "Cobalt application dispatches require main")
    require(os.environ["GITHUB_REPOSITORY"] == deployment.repository, "dispatch repository differs")
    accepted = AcceptedImage.parse(record, deployment)
    dispatch = build_id(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], "one")
    outputs({"build_id": dispatch, "label": deployment.label(dispatch + "-a", accepted.ami_id),
             "ami_id": accepted.ami_id, "recipe_id": accepted.recipe_id, "kernel_release": accepted.kernel_release,
             "region": deployment.region, "role_arn": deployment.controller_role_arn, "environment": deployment.environment})


def watchdog_module():
    spec = importlib.util.spec_from_file_location("watchdog", ROOT / "scripts/watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def monitor(cloud, accepted, output, poll=15):
    watchdog = watchdog_module()
    image = accepted.inspect(cloud, cloud.deployment.deadlines["registration_seconds"] + 900)
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    dispatch = build_id(run_id, attempt, "one")
    started = time.monotonic()
    report = {"status": "failed", "build_id": dispatch, "ami_id": accepted.ami_id, "started_at": timestamp(), "observed_instances": {}}
    cancel = True
    try:
        while True:
            jobs = watchdog.selected_jobs(watchdog.github.jobs(cloud.deployment.repository, run_id, attempt), "test")
            report["jobs"] = jobs
            for instance in watchdog.adopt_test_instances(cloud, dispatch, jobs, image=image, expiry=accepted.expires_at):
                report["observed_instances"][instance["InstanceId"]] = instance
            job = jobs.get("smoke-a", {})
            elapsed = time.monotonic() - started
            require(elapsed < cloud.deployment.deadlines["registration_seconds"] + 900, "application workflow deadline exceeded")
            if job.get("status", "queued") == "queued":
                require(elapsed < cloud.deployment.deadlines["registration_seconds"], "application launch/registration deadline exceeded")
            if job.get("status") == "completed":
                report["status"] = "passed" if job.get("conclusion") == "success" else "upstream_failed"
                cancel = False
                require(report["status"] == "passed", "Cobalt application job failed")
                return report
            write_json(output, report)
            time.sleep(poll)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        write_json(output, report)
        try:
            cloud.retain(output, f"{dispatch}/watchdog.json")
        finally:
            if cancel:
                watchdog.github.cancel(cloud.deployment.repository, run_id)


def verify(cloud, accepted, smoke, job):
    require(job.get("conclusion") == "success" and smoke["ctest_passed"], "Cobalt compile/run or artifact upload failed")
    dispatch = build_id(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], "one")
    identity = smoke["identity"]
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
    observed = watchdog_module().adopt_test_instances(cloud, dispatch, {"smoke-a": job}, image=accepted.inspect(cloud), expiry=accepted.expires_at)
    require(len(observed) == 1 and observed[0]["InstanceId"] == identity["identity"]["instanceId"], "GitHub job and guest instance identities differ")
    require(observed[0].get("CurrentInstanceBootMode") == "uefi", "test runner boot mode differs")
    return {"status": "passed", "build_id": dispatch, "ami_id": accepted.ami_id, "recipe_id": accepted.recipe_id,
            "kernel_release": accepted.kernel_release, "instance_id": observed[0]["InstanceId"], "github_job_id": job["id"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", choices=("accept", "prepare", "inspect", "watchdog", "verify", "retain"))
    parser.add_argument("--record", default="accepted-image.json")
    parser.add_argument("--result")
    parser.add_argument("--smoke", type=Path, default=Path("artifacts/application/smoke"))
    args = parser.parse_args()
    deployment = load_deployment(inventories=False)
    if args.task == "accept":
        require(args.result, "accept requires --result with the final qualified image result")
        write_json(args.record, selected_record(read_json(args.result), deployment, file_sha(Path(args.result))))
        return
    if args.task == "retain":
        cloud = Cloud(deployment)
        dispatch = build_id(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], "one")
        directory = Path("artifacts/application")
        locations = {str(path.relative_to(directory)): cloud.retain(path, f"{dispatch}/application/{path.relative_to(directory)}")
                     for path in sorted(directory.rglob("*")) if path.is_file()
                     and path.suffix in (".json", ".xml", ".log") and "build" not in path.relative_to(directory).parts}
        write_json(directory / "artifact-index.json", locations)
        cloud.retain(directory / "artifact-index.json", f"{dispatch}/application/artifact-index.json")
        return
    record = read_json(args.record)
    if args.task == "prepare":
        prepare(deployment, record)
        return
    accepted = AcceptedImage.parse(record, deployment)
    cloud = Cloud(deployment)
    if args.task == "inspect":
        write_json("artifacts/application/admission.json", accepted.inspect(cloud, deployment.deadlines["registration_seconds"] + 900))
    elif args.task == "watchdog":
        monitor(cloud, accepted, Path("artifacts/application/watchdog.json"))
    elif args.task == "verify":
        spec = importlib.util.spec_from_file_location("verify_results", ROOT / "scripts/verify-run-results.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        watchdog = watchdog_module()
        jobs = watchdog.selected_jobs(watchdog.github.jobs(deployment.repository, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]), "test")
        write_json("artifacts/application/result.json", verify(cloud, accepted, module.read_smoke(args.smoke), jobs.get("smoke-a", {})))


if __name__ == "__main__":
    main()
