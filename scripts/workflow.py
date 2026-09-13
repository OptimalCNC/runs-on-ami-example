#!/usr/bin/env python3
"""Artifact handoff and command selection used by the small workflow DAGs."""
import argparse
import base64
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess

from example import (ROOT, Cloud, build_id, load_deployment, match, outputs, read_json, require,
                     write_json)
from cleanup import CleanupScope, cleanup


def configure():
    require(os.environ.get("CLOUD_ENABLED") == "true", "Cloud execution is disabled. Review costs and enable AMI_EXAMPLE_CLOUD_ENABLED after approval.")
    d = load_deployment()
    require(d.repository == os.environ["GITHUB_REPOSITORY"], "repository configuration differs")
    require(os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch" and os.environ["GITHUB_REF"] == "refs/heads/main",
            "only manual dispatch from main may build images")
    variant = os.environ.get("BUILD_VARIANT", "one")
    build = build_id(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], variant)
    outputs({"build_id": build, "controller_label": d.label(build + "-controller", d.controller_ami.id, parent=True),
             "region": d.region, "role_arn": d.controller_role_arn, "environment": d.environment,
             "matrix": '["stock"]' if os.environ.get("BUILD_STAGE") == "stock" else
                       ('["one"]' if os.environ.get("BUILD_STAGE") == "single" or os.environ.get("FAULT", "none") != "none" else '["one","two"]')})


def finalize():
    d = load_deployment()
    variant = os.environ["BUILD_VARIANT"]
    build = build_id(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], variant)
    handoff = ROOT / "artifacts/handoff"
    target = ROOT / "artifacts/final" / build
    target.mkdir(parents=True, exist_ok=True)
    cloud = Cloud(d)
    result = None
    errors = []
    try:
        source = handoff / f"{build}-build/image-result.json"
        if variant != "stock" and source.exists():
            result = read_json(source)
            shutil.copy2(source, target / "image-result.json")
        watchdog = read_json(handoff / f"{build}-watchdog/watchdog.json")
        require(watchdog["status"] == "passed", "independent watchdog did not observe successful completion")
        if variant == "stock":
            stock = read_json(handoff / f"{build}-build/stock-result.json")
            require(stock["status"] == "passed", "stock controller/builder qualification failed")
            write_json(target / "stock-result.json", stock)
        else:
            require(result is not None, "candidate creation record is missing")
            subprocess.run([
                "python3", "scripts/verify-run-results.py", "--result", str(target / "image-result.json"),
                "--probe", str(handoff / f"{build}-probe/probe.json"),
                "--smoke-a", str(handoff / f"{build}-smoke-a"), "--smoke-b", str(handoff / f"{build}-smoke-b"),
            ], check=True, cwd=ROOT)
            result = read_json(target / "image-result.json")
    except Exception as error:
        errors.append(str(error))
    finally:
        try:
            report = cleanup(cloud, CleanupScope.parse(d.repository, build=build), target / "cleanup", apply=True)
            if report["status"] != "passed":
                errors.append("resource cleanup incomplete")
        except Exception as error:
            report = {"status": "failed", "error": str(error)}
            errors.append(f"cleanup: {error}")
        if result:
            result["lifecycle"]["cleanup"] = {"status": report["status"], "retained_images": report.get("retained_images", [])}
            result["status"] = "qualified" if not errors else "failed"
            if errors:
                result["validation"]["errors"] = errors
            write_json(target / "image-result.json", result)
        write_json(target / "finalization.json", {"status": "passed" if not errors else "failed", "errors": errors})
        locations = {str(path.relative_to(target)): cloud.retain(path, f"{build}/final/{path.relative_to(target)}")
                     for path in sorted(target.rglob("*.json"))}
        write_json(target / "artifact-index.json", locations)
        cloud.retain(target / "artifact-index.json", f"{build}/final/artifact-index.json")
    require(not errors, f"qualification failed: {errors}")


@dataclasses.dataclass(frozen=True)
class CompletedDispatch:
    scope: CleanupScope
    workflow_name: str
    head_sha: str


def completed_dispatch(cloud, run_id, run_attempt=None):
    """Prove one completed attempt before authorizing any cleanup mutation."""
    from accepted_image import watchdog_module
    watchdog = watchdog_module()
    d = cloud.deployment
    CleanupScope.parse(d.repository, run_id=run_id, run_attempt=run_attempt)
    path = f"/repos/{d.repository}/actions/runs/{run_id}"
    if run_attempt:
        path += f"/attempts/{run_attempt}"
    run = watchdog.github.request(path)
    require(str(run["id"]) == run_id and (run_attempt is None or str(run["run_attempt"]) == run_attempt),
            "cleanup API response does not identify the selected run attempt")
    require(run["event"] == "workflow_dispatch" and run["head_branch"] == "main"
            and run["status"] == "completed", "cleanup requires a completed manual main run attempt")
    require(run["name"] in ("Build and test Cobalt AMIs", "Run Cobalt application"), "unexpected workflow cleanup target")
    scope = CleanupScope.parse(d.repository, run_id=run_id, run_attempt=str(run["run_attempt"]))
    return CompletedDispatch(scope, run["name"], match(run["head_sha"], r"[0-9a-f]{40}", "cleanup workflow commit"))


def recover_dispatch_ownership(cloud, dispatch):
    """Recover only registered jobs from the proven completed attempt."""
    from accepted_image import AcceptedImage, watchdog_module
    watchdog = watchdog_module()
    d = cloud.deployment
    run_id, attempt = dispatch.scope.run_id, dispatch.scope.run_attempt
    application = dispatch.workflow_name == "Run Cobalt application"
    accepted = None
    jobs = watchdog.github.jobs(d.repository, run_id, attempt)
    for variant in (("test",) if application else ("one", "two")):
        selected = {stage: job for stage, job in watchdog.selected_jobs(jobs, variant).items()
                    if stage.startswith("smoke-") and job.get("runner_name")
                    and cloud.instance(watchdog.runner_instance_id(job))["State"]["Name"] != "terminated"}
        if not selected:
            continue
        if application and accepted is None:
            source = watchdog.github.request(f"/repos/{d.repository}/contents/accepted-image.json?ref={dispatch.head_sha}")
            accepted = AcceptedImage.parse(json.loads(base64.b64decode(source["content"])), d, allow_expired=True)
        build = build_id(run_id, attempt, "one" if application else variant)
        image = accepted.inspect(cloud, minimum_seconds=None) if accepted else None
        watchdog.adopt_test_instances(cloud, build, selected, image=image, expiry=accepted.expires_at if accepted else None)


def cleanup_workflow(cloud, output, run_id=None, run_attempt=None):
    if not run_id:
        require(run_attempt is None, "cleanup attempt requires a run ID")
        return cleanup(cloud, CleanupScope.parse(cloud.deployment.repository, expired=True), output, apply=True)
    # Completion must be established outside the cleanup fallback. A newer active
    # attempt must never acquire cleanup authorization from an older completion.
    dispatch = completed_dispatch(cloud, run_id, run_attempt)
    try:
        recover_dispatch_ownership(cloud, dispatch)
    finally:
        # If GitHub job recovery fails, already-owned resources in this proven
        # completed attempt remain safe to clean; other attempts are excluded.
        report = cleanup(cloud, dispatch.scope, output, apply=True)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", choices=("configure", "build", "probe", "finalize", "compare", "cleanup-config", "cleanup"))
    args = parser.parse_args()
    if args.task == "configure":
        configure()
    elif args.task == "build":
        command = ["python3", "scripts/build-image.py", "--variant", os.environ["BUILD_VARIANT"], "--execute"]
        if os.environ.get("RETAIN") == "true":
            command.append("--retain")
        subprocess.run(command, check=True)
    elif args.task == "probe":
        expected = build_id(os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], os.environ["BUILD_VARIANT"])
        require(os.environ["BUILD_ID"] == expected, "stale probe inputs: use a new manual dispatch or re-run all jobs")
        command = ["python3", "scripts/probe-ami.py", "--result", f"artifacts/{os.environ['BUILD_ID']}/image-result.json", "--execute"]
        if os.environ.get("FAULT") == "probe-identity":
            command.append("--fault")
        subprocess.run(command, check=True)
    elif args.task == "finalize":
        finalize()
    elif args.task == "compare":
        paths = sorted((ROOT / "artifacts/comparison").glob("**/image-result.json"))
        require(len(paths) == 2, "comparison requires exactly two qualified result artifacts")
        completed = subprocess.run(["python3", "scripts/compare-builds.py", *map(str, paths), "--output", "artifacts/comparison/reproducibility.json"])
        cloud = Cloud(load_deployment())
        for path in [*paths, ROOT / "artifacts/comparison/reproducibility.json"]:
            cloud.retain(path, f"comparisons/{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}/{path.parent.name}/{path.name}")
        require(completed.returncode == 0, "reproducibility comparison failed")
    elif args.task == "cleanup-config":
        d = load_deployment(inventories=False)
        require(d.repository == os.environ["GITHUB_REPOSITORY"], "cleanup repository differs")
        outputs({"region": d.region, "role_arn": d.controller_role_arn, "environment": d.environment})
    elif args.task == "cleanup":
        run_attempt = os.environ.get("CLEANUP_RUN_ATTEMPT") or None
        require(os.environ.get("GITHUB_EVENT_NAME") != "workflow_run" or run_attempt is not None,
                "completed workflow cleanup requires the triggering run attempt")
        report = cleanup_workflow(Cloud(load_deployment(inventories=False)), ROOT / "artifacts/cleanup",
                                  os.environ.get("CLEANUP_RUN_ID") or None, run_attempt)
        require(report["status"] == "passed", "cleanup incomplete; retained diagnostics describe remaining resources")


if __name__ == "__main__":
    main()
