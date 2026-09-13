#!/usr/bin/env python3
"""Qualify candidate evidence against EC2 and GitHub control-plane observations."""
import argparse
from pathlib import Path
import xml.etree.ElementTree as ET

from example import (BUILD_TAG, OWNER_TAG, RUNS_ON_TAG, Cloud, CobaltIdentity, load_deployment, parse_time, read_json,
                     require, tags_of, write_json)
from qualification import QualificationRun
from watchdog import github, runner_instance_id, selected_jobs


def verify_guest(guest, result, deployment):
    require(CobaltIdentity.parse(guest.get("xenomai")) == CobaltIdentity.parse(result["payload"].get("xenomai")),
            "running Cobalt identity differs")
    require(guest["kernel_release"] == result["payload"]["kernel_release"], "running kernel differs")
    require(guest["recipe_id"] == result["source"]["recipe_id"], "running recipe differs")
    require(guest["config_sha256"] == result["payload"]["config_sha256"], "running config differs")
    require(guest["boot_mode"] == result["cloud"]["boot_mode"], "running boot mode differs")
    identity = guest["identity"]
    for field, expected in {"imageId": result["cloud"]["ami_id"], "accountId": deployment.account_id,
                            "region": deployment.region, "instanceType": deployment.instance_type}.items():
        require(identity[field] == expected, f"guest identity {field} differs")
    require(guest["runner_version"] == result["execution"]["inherited_runner_version"], "runner agent changed after snapshot")
    require(guest["runner_listener_sha256"] == result["payload"]["parent_inventory"]["runner_listener_sha256"], "runner agent binary changed")
    require(guest["bootstrap_files"] == result["execution"]["bootstrap_files"], "bootstrap files changed after snapshot")
    require(guest["packages_sha256"] == result["payload"]["packages_sha256"], "installed packages changed after snapshot")
    require(guest["snap_hashes"] == result["payload"]["snap_hashes"], "inherited snap content changed after snapshot")
    return identity["instanceId"]


def verify(cloud, result, probe, a, b, job_evidence):
    d = cloud.deployment
    qualification = QualificationRun.from_result(result)
    require(probe["status"] == "passed" and probe.get("terminated") is True, "direct boot has not passed and terminated")
    if "qualification" in result["validation"]:
        require(probe.get("build_id") == qualification.build_id, "direct boot belongs to another qualification dispatch")
    probe_id = verify_guest(probe["guest"], result, d)
    ids = []
    machines = []
    observations = []
    for stage, report in (("a", a), ("b", b)):
        identity = report["identity"]
        instance_id = verify_guest(identity, result, d)
        require(identity["stage"] == stage and identity["sentinel_absent_at_start"] is True, "sentinel check failed")
        expected_sentinel = f"/var/tmp/ami-example-{qualification.build_id}-sentinel"
        require(identity["sentinel"] == expected_sentinel, "sentinel path differs")
        environment = report["environment"]
        require(environment.get("environment_passed") is True, "later-step environment test failed")
        require(environment["stage"] == stage and environment["sentinel"] == expected_sentinel,
                "later-step qualification sentinel differs")
        require(verify_guest(environment, result, d) == instance_id, "instance changed between test steps")
        require(report["ctest_passed"], "Cobalt application failed")
        job = job_evidence[f"smoke-{stage}"]
        require(job["conclusion"] == "success", "GitHub smoke job/artifact upload failed")
        require(runner_instance_id(job) == instance_id, "GitHub job and guest instance identities differ")
        actual = cloud.instance(instance_id)
        require(actual["ImageId"] == result["cloud"]["ami_id"] and actual["InstanceType"] == d.instance_type,
                "controller observed wrong image or instance type")
        require(actual.get("InstanceLifecycle", "on-demand") == "on-demand", "test used a Spot instance")
        require(actual.get("CurrentInstanceBootMode") == result["cloud"]["boot_mode"], "controller observed wrong boot mode")
        tags = tags_of(actual)
        require(tags.get(RUNS_ON_TAG) == d.repository
                and tags.get(OWNER_TAG) in (None, d.repository)
                and tags.get(BUILD_TAG) in (None, qualification.build_id),
                "test instance ownership differs")
        require(parse_time(actual["LaunchTime"]) >= parse_time(result["lifecycle"]["created_at"]), "instance predates the candidate")
        ids.append(instance_id)
        machines.append(identity["machine_id"])
        observations.append({"status": "passed", "stage": stage, "instance_id": instance_id,
                             "guest": identity, "github_job_id": job["id"], "runner_id": job["runner_id"],
                             "application": {"name": "cobalt", "status": "passed"}})
    require(len({probe_id, *ids}) == 3, "probe and RunsOn tests must use three distinct instances")
    require(len(set(machines)) == 2 and all(machines), "machine identity was reused")
    result["validation"]["direct_boot"] = probe
    result["validation"]["runs_on"] = observations
    # Qualification is committed by finalization only after verified cleanup.
    result["status"] = "candidate"
    return result


def read_smoke(directory):
    tests = ET.parse(directory / "ctest.xml").getroot()
    cases = list(tests.iter("testcase"))
    passed = (len(cases) == 1 and cases[0].get("name") == "cobalt"
              and not list(tests.iter("failure")) and not list(tests.iter("error")) and not list(tests.iter("skipped")))
    return {"identity": read_json(directory / "identity.json"), "environment": read_json(directory / "environment.json"),
            "ctest_passed": passed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", default="infra/deployment.json")
    parser.add_argument("--result", required=True)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--smoke-a", type=Path, required=True)
    parser.add_argument("--smoke-b", type=Path, required=True)
    args = parser.parse_args()
    d = load_deployment(args.deployment)
    result = read_json(args.result)
    qualification = QualificationRun.from_result(result)
    jobs = selected_jobs(github.jobs(d.repository, qualification.run_id, qualification.run_attempt),
                         qualification.build_id.rsplit("-", 1)[-1])
    updated = verify(Cloud(d), result, read_json(args.probe), read_smoke(args.smoke_a), read_smoke(args.smoke_b), jobs)
    write_json(args.result, updated)
    print("Cobalt boot, two fresh RunsOn instances, environment, Cobalt application and artifact uploads verified.")


if __name__ == "__main__":
    main()
