#!/usr/bin/env python3
"""Bound launch/registration even when a queued candidate never runs a step."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import time

from example import (ROOT, BUILD_TAG, EXPIRY_TAG, OWNER_TAG, PURPOSE_TAG, RUNS_ON_TAG, Cloud, build_id,
                     filters_for, load_deployment, parse_time, read_json, require, resource_tags, tags_of, timestamp, write_json)

spec = importlib.util.spec_from_file_location("github_api", ROOT / "scripts/github-api.py")
github = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github)


def selected_jobs(jobs, variant):
    result = {}
    for job in jobs:
        for stage in ("configure", "build", "probe", "smoke-a", "smoke-b"):
            if job["name"].endswith(f"{variant} / {stage}"):
                require(stage not in result, f"ambiguous GitHub job name: {stage}")
                result[stage] = job
    return result


def registration_waiting(jobs, stock=False, retained=False):
    targets = [] if retained else [("build", "configure")]
    if not stock:
        targets += [("smoke-a", "probe"), ("smoke-b", "smoke-a")]
    return [stage for stage, prerequisite in targets
            if jobs.get(prerequisite, {}).get("conclusion") == "success"
            and jobs.get(stage, {}).get("status", "queued") == "queued"]


def runner_instance_id(job):
    # RunsOn's public service template uses this same runner-name field.
    parts = job["runner_name"].split("--")
    require(len(parts) >= 2 and re.fullmatch(r"i-[0-9a-f]{17}", parts[1]), "GitHub job lacks an exact RunsOn instance identity")
    return parts[1]


def adopt_test_instances(cloud, build, jobs=None, *, image=None, expiry=None, instance_type=None):
    """Adopt only instances named by this dispatch's GitHub job records."""
    d = cloud.deployment
    images = [image] if image else cloud.call("ec2", "describe-images", {"Owners": [d.account_id], "Filters": filters_for(d.repository, build)})["Images"]
    observations = []
    for stage, job in (jobs or {}).items():
        if not stage.startswith("smoke-") or not job.get("runner_name"):
            continue
        instance = cloud.instance(runner_instance_id(job))
        matches = [candidate for candidate in images if candidate["ImageId"] == instance["ImageId"]]
        require(len(matches) == 1, "GitHub job runner uses an unexpected AMI")
        candidate = matches[0]
        tags = tags_of(instance)
        require(tags.get(RUNS_ON_TAG) == d.repository, "RunsOn stack must propagate its repository ownership marker")
        require(tags.get(OWNER_TAG) in (None, d.repository) and tags.get(BUILD_TAG) in (None, build)
                and tags.get(PURPOSE_TAG) in (None, "test"), "candidate instance is already owned by another build")
        require(parse_time(instance["LaunchTime"]) >= parse_time(candidate["CreationDate"]), "candidate instance predates image")
        require(instance["InstanceType"] == (instance_type or d.instance_type) and instance.get("InstanceLifecycle", "on-demand") == "on-demand",
                "RunsOn launched an unqualified instance type or purchasing model")
        if instance["State"]["Name"] != "terminated" and (tags.get(BUILD_TAG) != build or tags.get(OWNER_TAG) != d.repository):
            inherited = resource_tags(d, build, "test", expiry or tags_of(candidate)[EXPIRY_TAG])
            cloud.call("ec2", "create-tags", {"Resources": [instance["InstanceId"]], "Tags": inherited})
        observations.append(instance)
    return observations


def monitor(cloud, variant, run_id, attempt, output, poll=15, result=None):
    d = cloud.deployment
    build = build_id(run_id, attempt, variant)
    started = time.monotonic()
    # Retained qualification has a 60-minute hosted job; reserve 15 minutes for
    # setup and reporting instead of inheriting the full image-build deadline.
    deadline_seconds = min(d.deadlines["workflow_seconds"], 45 * 60) if result is not None else d.deadlines["workflow_seconds"]
    queued_since = {}
    report = {"status": "failed", "build_id": build, "started_at": timestamp(), "observed_instances": {}}
    image = None
    should_cancel = True
    try:
        if result is not None:
            from qualification import QualificationRun
            from retained_candidate import inspect_candidate
            require(QualificationRun.from_result(result).build_id == build, "watchdog candidate belongs to another qualification attempt")
            image = inspect_candidate(cloud, result)
        while True:
            current = selected_jobs(github.jobs(d.repository, run_id, attempt), variant)
            report["jobs"] = current
            for instance in adopt_test_instances(cloud, build, current, image=image,
                                                  expiry=result["lifecycle"]["expires_at"] if result else None):
                report["observed_instances"][instance["InstanceId"]] = instance
            now = time.monotonic()
            require(now - started < deadline_seconds, "independent workflow deadline exceeded")
            for stage in registration_waiting(current, variant == "stock", result is not None):
                queued_since.setdefault(stage, now)
                require(now - queued_since[stage] < d.deadlines["registration_seconds"], f"{stage} launch/registration deadline exceeded")
            # Jobs not yet runnable do not consume a registration deadline.
            queued_since = {stage: since for stage, since in queued_since.items()
                            if stage in registration_waiting(current, variant == "stock", result is not None)}
            final = current.get("build" if variant == "stock" else "smoke-b", {})
            failed = any(job.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required") for job in current.values())
            if final.get("status") == "completed" or failed:
                report["status"] = "passed" if final.get("conclusion") == "success" and not failed else "upstream_failed"
                should_cancel = False
                return report
            write_json(output / "watchdog.json", report)
            time.sleep(poll)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        write_json(output / "watchdog.json", report)
        try:
            if should_cancel:
                # The completed-workflow cleanup runs independently after this cancellation.
                github.cancel(d.repository, run_id)
        finally:
            cloud.retain(output / "watchdog.json", f"{build}/watchdog.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", default="infra/deployment.json")
    parser.add_argument("--variant", choices=("one", "two", "stock"), required=True)
    parser.add_argument("--result", help="candidate result carrying a separate retained-image qualification context")
    args = parser.parse_args()
    output = ROOT / "artifacts" / "watchdog"
    output.mkdir(parents=True, exist_ok=True)
    monitor(Cloud(load_deployment(args.deployment)), args.variant, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], output,
            result=read_json(args.result) if args.result else None)


if __name__ == "__main__":
    main()
