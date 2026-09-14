#!/usr/bin/env python3
"""Read explicit GitHub run evidence or request cancellation of an explicit run."""
import argparse
import dataclasses
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from example import INSTANCE, match, read_json, require, write_json
from execution_record import ExecutionRecord, RunAttempt


def request(path, method="GET", payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request("https://api.github.com" + path, data=body, method=method, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "runs-on-ami-example",
    })
    with urlopen(req, timeout=30) as response:
        value = response.read()
        return json.loads(value) if value else {}


def jobs(repository, run_id, attempt):
    selected = RunAttempt.parse(repository, run_id, attempt)
    result = []
    page = 1
    while True:
        value = request(f"/repos/{selected.repository}/actions/runs/{selected.run_id}/attempts/{selected.run_attempt}/jobs?per_page=100&page={page}")["jobs"]
        result.extend(value)
        if len(value) < 100:
            return result
        page += 1


def runner_instance_id(job):
    parts = job["runner_name"].split("--")
    require(len(parts) >= 2, "GitHub job lacks an exact RunsOn instance identity")
    return match(parts[1], INSTANCE, "GitHub job runner instance ID")


def selected_jobs(jobs, plan):
    selected = {}
    names = {job.name: stage for stage, job in plan.stages.items()}
    for job in jobs:
        stage = names.get(job["name"])
        if stage is None:
            continue
        require(stage not in selected, f"ambiguous GitHub job name: {job['name']}")
        selected[stage] = dict(job)
        if plan.stages[stage].adopt and job.get("runner_name"):
            selected[stage]["instance_id"] = runner_instance_id(job)
    return selected


def completed(repository, run_id, attempt=None):
    selected = RunAttempt.parse(repository, run_id, attempt or "1")
    path = f"/repos/{selected.repository}/actions/runs/{selected.run_id}"
    if attempt is not None:
        path += f"/attempts/{selected.run_attempt}"
    value = request(path)
    require(str(value["id"]) == selected.run_id and (attempt is None or str(value["run_attempt"]) == selected.run_attempt),
            "GitHub response does not identify the selected run attempt")
    require(value["status"] == "completed", "ownership recovery requires a completed run attempt")
    return RunAttempt.parse(repository, run_id, str(value["run_attempt"]))


def cancel(repository, run_id):
    selected = RunAttempt.parse(repository, run_id, "1")
    try:
        request(f"/repos/{selected.repository}/actions/runs/{selected.run_id}/cancel", "POST")
    except HTTPError as error:
        if error.code != 409:  # Already completed/cancelled is an idempotent outcome.
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("jobs", help="select jobs using an explicit execution record")
    fetch.add_argument("--record", required=True)
    fetch.add_argument("--output", required=True)
    for name in ("completed", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("--repository", required=True)
        command.add_argument("--run-id", required=True)
        if name == "completed":
            command.add_argument("--run-attempt")
            command.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "jobs":
        record = ExecutionRecord.parse(read_json(args.record))
        attempt = record.attempt
        write_json(args.output, selected_jobs(jobs(attempt.repository, attempt.run_id, attempt.run_attempt), record.plan))
    elif args.command == "completed":
        write_json(args.output, dataclasses.asdict(completed(args.repository, args.run_id, args.run_attempt)))
    else:
        cancel(args.repository, args.run_id)


if __name__ == "__main__":
    main()
