#!/usr/bin/env python3
"""Commit image qualification from verified runtime evidence and completed cleanup."""
import argparse
import copy
import dataclasses
from pathlib import Path

from example import Cloud, INSTANCE, load_cleanup_context, match, read_json, require, write_json
from qualification import QualificationRun
from retained_candidate import inspect_candidate


@dataclasses.dataclass(frozen=True)
class VerifiedImage:
    result: dict
    qualification: QualificationRun

    @classmethod
    def parse(cls, result, build, deployment):
        require(isinstance(result, dict), "image result must be an object")
        require(result.get("status") == "candidate", "finalization requires a verified candidate")
        qualification = QualificationRun.from_result(result)
        require(qualification.build_id == build, "verification belongs to a different execution")
        target = result["cloud"]
        require(target["account_id"] == deployment.account_id and target["region"] == deployment.region,
                "verified image account or region differs")
        validation = result["validation"]
        require(not validation.get("errors"), "image verification contains errors")
        probe = validation["direct_boot"]
        require(probe["status"] == "passed" and probe.get("terminated") is True,
                "direct boot has not passed and terminated")
        require(probe.get("build_id") == build, "direct boot belongs to a different execution")
        tests = validation["runs_on"]
        require(len(tests) == 2 and {test["stage"] for test in tests} == {"a", "b"},
                "two runtime test stages are required")
        instances = []
        machines = []
        for check in [probe, *tests]:
            require(check["status"] == "passed", "runtime verification has not passed")
            identifier = match(check["instance_id"], INSTANCE, "verified instance ID")
            guest = check["guest"]
            identity = guest["identity"]
            require(identity["instanceId"] == identifier and identity["imageId"] == target["ami_id"]
                    and identity["accountId"] == deployment.account_id and identity["region"] == deployment.region,
                    "verified instance belongs to another image or cloud target")
            instances.append(identifier)
            if check is not probe:
                require(check["application"] == {"name": "cobalt", "status": "passed"},
                        "Cobalt application verification is missing")
                require(guest["stage"] == check["stage"] and guest["sentinel_absent_at_start"] is True
                        and guest["sentinel"] == f"/var/tmp/ami-example-{build}-sentinel",
                        "runtime freshness evidence differs")
                machines.append(guest["machine_id"])
        require(len(set(instances)) == 3, "probe and runtime tests must use three distinct instances")
        require(len(set(machines)) == 2 and all(machines), "runtime machine identity was reused")
        return cls(copy.deepcopy(result), qualification)


@dataclasses.dataclass(frozen=True)
class CompletedCleanup:
    retained_images: tuple[str, ...]

    @classmethod
    def parse(cls, report, build, deployment):
        require(report["status"] == "passed", "resource cleanup has not passed")
        require(report["account_id"] == deployment.account_id and report["region"] == deployment.region,
                "cleanup account or region differs")
        scope = report["scope"]
        require(scope["owner"] == deployment.repository and scope["build"] == build
                and not scope.get("run_id") and not scope.get("run_attempt") and not scope.get("expired"),
                "cleanup belongs to a different execution")
        require(not report["errors"] and not any(report["remaining"].values()), "cleanup has unresolved resources or errors")
        images = report["retained_images"]
        require(isinstance(images, list) and all(isinstance(image, str) for image in images),
                "cleanup retained image evidence is missing")
        return cls(tuple(images))


def finalize(cloud, build, result, cleanup_report):
    """Return a completed result; neither run tests nor remove resources here."""
    updated = copy.deepcopy(result) if isinstance(result, dict) else {}
    cleanup_status = {"status": "failed", "retained_images": []}
    errors = []
    try:
        completed = CompletedCleanup.parse(cleanup_report, build, cloud.deployment)
        cleanup_status = {"status": "passed", "retained_images": list(completed.retained_images)}
    except Exception as error:
        errors.append(f"cleanup: {error}")
    try:
        verified = VerifiedImage.parse(result, build, cloud.deployment)
    except Exception as error:
        errors.append(f"verification: {error}")
    if not errors and verified.result["execution"]["build_id"] != verified.qualification.build_id:
        try:
            inspect_candidate(cloud, verified.result, for_launch=False)
            cleanup_status["retained_images"] = [verified.result["cloud"]["ami_id"]]
        except Exception as error:
            errors.append(f"source image retention: {error}")
            cleanup_status = {"status": "failed", "retained_images": []}
    if not errors and verified.result["lifecycle"]["retain"] and verified.result["cloud"]["ami_id"] not in cleanup_status["retained_images"]:
        errors.append("retained candidate is missing from cleanup evidence")
        cleanup_status["status"] = "failed"
    updated["status"] = "failed" if errors else "qualified"
    if errors:
        record_errors(updated, errors)
    if not isinstance(updated.get("lifecycle"), dict):
        updated["lifecycle"] = {}
    updated["lifecycle"]["cleanup"] = cleanup_status
    return updated


def record_errors(result, errors):
    result["status"] = "failed"
    if not isinstance(result.get("validation"), dict):
        result["validation"] = {}
    previous = result["validation"].get("errors", [])
    result["validation"]["errors"] = previous + errors if isinstance(previous, list) else errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {}
    try:
        context = QualificationRun.parse(args.build_id)
        loaded = read_json(args.result)
        require(isinstance(loaded, dict), "image result must be an object")
        result = loaded
        cleanup_report = read_json(args.cleanup)
        result = finalize(Cloud(load_cleanup_context(args.deployment)), context.build_id, result, cleanup_report)
    except Exception as error:
        record_errors(result, [str(error)])
    write_json(args.output, result)
    require(result["status"] == "qualified", f"qualification failed: {result.get('validation', {}).get('errors', [])}")
    print(f"Image qualification completed: {args.output}")


if __name__ == "__main__":
    main()
