#!/usr/bin/env python3
"""Plan or apply cleanup using live ownership tags, never untrusted artifact IDs."""
import argparse
import dataclasses
from pathlib import Path
import re
import subprocess
import time

from example import (ROOT, BUILD_TAG, EXPIRY_TAG, OWNER_TAG, PURPOSE_TAG, AwsCommandError, InvalidInput, Cloud,
                     filters_for, load_deployment, parse_time, require, tags_of, timestamp, utcnow, write_json)


@dataclasses.dataclass(frozen=True)
class CleanupScope:
    owner: str
    build: str | None = None
    run_id: str | None = None
    expired: bool = False
    run_attempt: str | None = None

    def includes(self, resource):
        tags = tags_of(resource)
        identity = tags.get(BUILD_TAG, "")
        if tags.get(OWNER_TAG) != self.owner or not re.fullmatch(r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", identity):
            return False
        if tags.get(PURPOSE_TAG) not in ("builder", "probe", "candidate", "test"):
            return False
        if self.build and identity != self.build:
            return False
        if self.run_id and identity.split("-", 1)[0] != self.run_id:
            return False
        if self.run_attempt and identity.split("-")[1] != self.run_attempt:
            return False
        if self.expired:
            try:
                return parse_time(tags[EXPIRY_TAG]) <= utcnow()
            except (KeyError, ValueError):
                return False
        return True

    @classmethod
    def parse(cls, owner, build=None, run_id=None, expired=False, run_attempt=None):
        require(sum(bool(value) for value in (build, run_id, expired)) == 1, "select one cleanup scope")
        if build:
            require(re.fullmatch(r"[1-9]\d*-[1-9]\d*-(?:one|two|stock)", build) is not None, "invalid cleanup build ID")
        if run_id:
            require(re.fullmatch(r"[1-9]\d*", run_id) is not None, "invalid cleanup run ID")
        if run_attempt is not None:
            require(run_id is not None and re.fullmatch(r"[1-9]\d*", run_attempt) is not None, "run attempt requires a run ID and a positive attempt")
        return cls(owner, build, run_id, expired, run_attempt)


def discover(cloud, scope):
    images = cloud.call("ec2", "describe-images", {"Owners": [cloud.deployment.account_id],
                                                  "Filters": filters_for(scope.owner)})["Images"]
    images = [image for image in images if scope.includes(image)]
    instances = {i["InstanceId"]: i for i in cloud.instances(filters_for(scope.owner)) if scope.includes(i)}
    unsafe = []
    observed = []
    for image in images:
        for instance in cloud.instances([{"Name": "image-id", "Values": [image["ImageId"]]}]):
            observed.append(instance)
            if instance["State"]["Name"] == "terminated" or instance["InstanceId"] in instances:
                continue
            # The AMI identifies the image, never the dispatch that owns a runner.
            # A retained image can have legitimate users in newer workflow runs.
            unsafe.append(instance["InstanceId"])
    volumes = cloud.call("ec2", "describe-volumes", {"Filters": filters_for(scope.owner)})["Volumes"]
    snapshots = cloud.call("ec2", "describe-snapshots", {"OwnerIds": [cloud.deployment.account_id],
                                                        "Filters": filters_for(scope.owner)})["Snapshots"]
    keys = cloud.call("ec2", "describe-key-pairs", {"Filters": filters_for(scope.owner)})["KeyPairs"]
    return {"images": images, "instances": list(instances.values()),
            "observed_candidate_instances": observed,
            "unsafe_instances": unsafe, "volumes": [v for v in volumes if scope.includes(v)],
            "snapshots": [s for s in snapshots if scope.includes(s)], "key_pairs": [k for k in keys if scope.includes(k)]}


def cleanup(cloud, scope, output, apply=False):
    found = discover(cloud, scope)
    report = {"schema_version": 1, "status": "planned", "scope": dataclasses.asdict(scope),
              "account_id": cloud.deployment.account_id, "region": cloud.deployment.region,
              "created_at": timestamp(), "resources": found, "errors": []}
    write_json(output / "cleanup-plan.json", report)
    if not apply:
        return report
    # Retain EC2 console/status and resource identities before removing cloud objects.
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("probe", ROOT / "scripts/probe-ami.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    for instance in found["instances"]:
        location = output / instance["InstanceId"]
        location.mkdir(parents=True, exist_ok=True)
        report["errors"].extend(module.diagnostics(cloud, instance["InstanceId"], location))
    durable = True
    scope_name = "expired" if scope.expired else (scope.build or f"run-{scope.run_id}")
    if scope.run_attempt:
        scope_name += f"-attempt-{scope.run_attempt}"
    for path in sorted(output.rglob("*.json")):
        try:
            cloud.retain(path, f"cleanup/{scope_name}/{path.relative_to(output)}")
        except (subprocess.SubprocessError, OSError, InvalidInput) as error:
            durable = False
            report["errors"].append(f"artifact retention: {error}")
    ids = []
    for instance in found["instances"]:
        current = cloud.instance(instance["InstanceId"])
        if scope.includes(current) and current["State"]["Name"] != "terminated":
            ids.append(current["InstanceId"])
    if ids:
        cloud.call("ec2", "terminate-instances", {"InstanceIds": ids})
        cloud.wait_terminated(ids)
    for key in found["key_pairs"]:
        current = cloud.call("ec2", "describe-key-pairs", {"KeyPairIds": [key["KeyPairId"]]})["KeyPairs"][0]
        if scope.includes(current):
            cloud.call("ec2", "delete-key-pair", {"KeyPairId": current["KeyPairId"]})
    retained_images = []
    deleted_images = []
    protected_snapshots = set()
    if durable:
        for image in found["images"]:
            if tags_of(image).get("ami-example:retain") == "true" and not scope.expired:
                retained_images.append(image["ImageId"])
                continue
            # Recheck users immediately before image deletion. Unknown or other-run
            # instances must finish independently; expiry never transfers ownership.
            users = [i["InstanceId"] for i in cloud.instances([{"Name": "image-id", "Values": [image["ImageId"]]}])
                     if i["State"]["Name"] != "terminated"]
            if users:
                report["errors"].append(f"image {image['ImageId']} still has active users: {users}")
                continue
            live = cloud.call("ec2", "describe-images", {"ImageIds": [image["ImageId"]], "Owners": [cloud.deployment.account_id]})["Images"]
            if live and scope.includes(live[0]):
                cloud.call("ec2", "deregister-image", {"ImageId": image["ImageId"]})
                deleted_images.append(image["ImageId"])
        # Allow EC2's deregistration to become visible before testing snapshot references.
        deadline = time.monotonic() + 120
        while True:
            remaining_images = cloud.call("ec2", "describe-images", {"Owners": [cloud.deployment.account_id]})["Images"]
            if not set(deleted_images) & {image["ImageId"] for image in remaining_images}:
                break
            require(time.monotonic() < deadline, "AMI deregistration did not become visible before the cleanup deadline")
            time.sleep(5)
        # Re-read *all* account images: shared/referenced snapshots must survive.
        referenced = {m["Ebs"]["SnapshotId"] for image in remaining_images for m in image.get("BlockDeviceMappings", []) if "Ebs" in m}
        protected_snapshots = referenced & {s["SnapshotId"] for s in found["snapshots"]}
        for snapshot in found["snapshots"]:
            if snapshot["SnapshotId"] not in referenced:
                live = cloud.call("ec2", "describe-snapshots", {"SnapshotIds": [snapshot["SnapshotId"]]})["Snapshots"][0]
                if scope.includes(live):
                    deadline = time.monotonic() + 120
                    while True:
                        try:
                            cloud.call("ec2", "delete-snapshot", {"SnapshotId": live["SnapshotId"]})
                            break
                        except AwsCommandError as error:
                            if error.code == "InvalidSnapshot.NotFound":
                                break
                            if error.code != "InvalidSnapshot.InUse" or time.monotonic() >= deadline:
                                raise
                            time.sleep(5)
        # Root disks usually disappear with instances. Delete only detached, tagged orphans.
        volumes = cloud.call("ec2", "describe-volumes", {"Filters": filters_for(scope.owner)})["Volumes"]
        for volume in volumes:
            if scope.includes(volume) and volume["State"] == "available" and not volume["Attachments"]:
                cloud.call("ec2", "delete-volume", {"VolumeId": volume["VolumeId"]})
    def outstanding(remaining):
        return {
            "instances": [i["InstanceId"] for i in remaining["instances"] if i["State"]["Name"] != "terminated"],
            "images": [i["ImageId"] for i in remaining["images"] if i["ImageId"] not in retained_images],
            "volumes": [v["VolumeId"] for v in remaining["volumes"]],
            "key_pairs": [k["KeyPairId"] for k in remaining["key_pairs"]],
            "snapshots": [s["SnapshotId"] for s in remaining["snapshots"] if s["SnapshotId"] not in protected_snapshots],
        }
    deadline = time.monotonic() + 120
    while True:
        leftovers = outstanding(discover(cloud, scope))
        if not any(leftovers.values()) or report["errors"] or found["unsafe_instances"] or time.monotonic() >= deadline:
            break
        time.sleep(5)
    report.update({"status": "passed" if not report["errors"] and not any(leftovers.values()) else "failed",
                   "remaining": leftovers, "retained_images": retained_images, "deleted_images": deleted_images,
                   "protected_snapshots": sorted(protected_snapshots)})
    write_json(output / "cleanup-report.json", report)
    cloud.retain(output / "cleanup-report.json", f"cleanup/{scope_name}/cleanup-report.json")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", default="infra/deployment.json")
    parser.add_argument("--build-id")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt")
    parser.add_argument("--expired", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", default="artifacts/cleanup")
    args = parser.parse_args()
    d = load_deployment(args.deployment, inventories=False)
    scope = CleanupScope.parse(d.repository, args.build_id, args.run_id, args.expired, args.run_attempt)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = cleanup(Cloud(d), scope, output, args.apply)
    print(f"Cleanup {report['status']}; see {output}")
    require(report["status"] in ("planned", "passed"), "cleanup incomplete; retained diagnostics describe remaining resources")


if __name__ == "__main__":
    main()
