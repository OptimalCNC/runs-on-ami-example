#!/usr/bin/env python3
"""Register, observe, or recover the ownership of explicitly planned GitHub jobs."""
import argparse
import importlib.util
from pathlib import Path
import time

from example import ROOT, Cloud, load_deployment, read_json, require, timestamp, write_json
from execution_record import ExecutionRecord, registered_records, retain_record
from runner_instances import adopt_test_instances

spec = importlib.util.spec_from_file_location("github_api", ROOT / "scripts/github-api.py")
github = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github)


def register(cloud, record, output):
    record.require_deployment(cloud.deployment)
    require(record.instance_type == cloud.deployment.instance_type, "execution instance type differs from deployment")
    if record.image is not None:
        record.image.inspect(cloud)
    write_json(output, record.as_dict())
    return retain_record(cloud, record, output)


def registration_waiting(jobs, plan):
    return [stage for stage, job in plan.stages.items()
            if job.needs and all(jobs.get(need, {}).get("conclusion") == "success" for need in job.needs)
            and jobs.get(stage, {}).get("status", "queued") == "queued"]


def instance_ids(jobs):
    return [job["instance_id"] for job in jobs.values() if job.get("instance_id")]


def monitor(cloud, record, output, poll=15):
    record.require_deployment(cloud.deployment)
    require(poll > 0, "poll interval must be positive")
    attempt = record.attempt
    started = time.monotonic()
    queued_since = {}
    report = {"status": "failed", "build_id": record.build_id, "started_at": timestamp(), "observed_instances": {}}
    try:
        image, expiry, _ = record.image.inspect(cloud) if record.image else (None, None, None)
        while True:
            current = github.selected_jobs(github.jobs(attempt.repository, attempt.run_id, attempt.run_attempt), record.plan)
            report["jobs"] = current
            for instance in adopt_test_instances(cloud, record.build_id, instance_ids(current), image=image,
                                                  expiry=expiry, instance_type=record.instance_type):
                report["observed_instances"][instance["InstanceId"]] = instance
            now = time.monotonic()
            require(now - started < record.deadline_seconds, "independent execution deadline exceeded")
            waiting = registration_waiting(current, record.plan)
            for stage in waiting:
                queued_since.setdefault(stage, now)
                require(now - queued_since[stage] < record.registration_seconds, f"{stage} launch/registration deadline exceeded")
            queued_since = {stage: since for stage, since in queued_since.items() if stage in waiting}
            final = current.get(record.terminal, {})
            failed = any(job.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required", "startup_failure")
                         for job in current.values())
            if final.get("status") == "completed" or failed:
                report["status"] = "passed" if final.get("conclusion") == "success" and not failed else "upstream_failed"
                return report
            write_json(output / "watchdog.json", report)
            time.sleep(poll)
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        write_json(output / "watchdog.json", report)
        cloud.retain(output / "watchdog.json", f"{record.build_id}/watchdog.json")


def recover(cloud, repository, run_id, attempt, output):
    completed = github.completed(repository, run_id, attempt)
    require(completed.repository == cloud.deployment.repository, "execution repository differs from deployment")
    report = {"status": "failed", "run_id": completed.run_id, "run_attempt": completed.run_attempt,
              "observed_instances": {}, "errors": []}
    try:
        records = registered_records(cloud, completed, output / "records")
        jobs = github.jobs(repository, completed.run_id, completed.run_attempt)
        for record in records:
            try:
                record.require_deployment(cloud.deployment)
                selected = github.selected_jobs(jobs, record.plan)
                active = [identifier for identifier in instance_ids(selected)
                          if cloud.instance(identifier)["State"]["Name"] != "terminated"]
                if not active:
                    continue
                image, expiry, _ = record.image.inspect(cloud, recovery=True) if record.image else (None, None, None)
                for instance in adopt_test_instances(cloud, record.build_id, active, image=image,
                                                      expiry=expiry, instance_type=record.instance_type):
                    report["observed_instances"][instance["InstanceId"]] = instance
            except Exception as error:
                report["errors"].append(f"{record.build_id}: {error}")
        require(not report["errors"], f"instance ownership recovery incomplete: {report['errors']}")
        report["status"] = "passed"
        return report
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        write_json(output / "recovery.json", report)
        cloud.retain(output / "recovery.json", f"recovery/{completed.run_id}/{completed.run_attempt}/recovery.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("register", "monitor", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--deployment", default="infra/deployment.json")
        command.add_argument("--output", type=Path, required=True,
                             help="execution JSON file" if name == "register" else "report directory")
        if name == "monitor":
            command.add_argument("--record", required=True)
            command.add_argument("--poll-seconds", type=int, default=15)
        else:
            command.add_argument("--repository", required=True)
            command.add_argument("--run-id", required=True)
            command.add_argument("--run-attempt", required=True)
        if name == "register":
            command.add_argument("--build-id", required=True)
            command.add_argument("--plan", required=True, help="JSON mapping stages to explicit name, needs, and adopt fields")
            command.add_argument("--terminal", required=True)
            command.add_argument("--deadline-seconds", type=int, required=True)
            command.add_argument("--registration-seconds", type=int, required=True)
            image = command.add_mutually_exclusive_group()
            image.add_argument("--accepted", help="accepted image record to snapshot")
            image.add_argument("--result", help="retained candidate result to snapshot")
    args = parser.parse_args()
    cloud = Cloud(load_deployment(args.deployment, inventories=args.command != "recover"))
    if args.command == "register":
        selection = ({"kind": "accepted" if args.accepted else "candidate", "record": read_json(args.accepted or args.result)}
                     if args.accepted or args.result else None)
        record = ExecutionRecord.parse({"schema_version": 1, "repository": args.repository,
            "run_id": args.run_id, "run_attempt": args.run_attempt, "build_id": args.build_id,
            "account_id": cloud.deployment.account_id, "region": cloud.deployment.region,
            "instance_type": cloud.deployment.instance_type,
            "plan": read_json(args.plan), "terminal": args.terminal, "deadline_seconds": args.deadline_seconds,
            "registration_seconds": args.registration_seconds, "image": selection})
        register(cloud, record, args.output)
    elif args.command == "monitor":
        report = monitor(cloud, ExecutionRecord.parse(read_json(args.record)), args.output, args.poll_seconds)
        require(report["status"] == "passed", "monitored jobs did not complete successfully")
    else:
        recover(cloud, args.repository, args.run_id, args.run_attempt, args.output)


if __name__ == "__main__":
    main()
