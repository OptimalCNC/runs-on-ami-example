#!/usr/bin/env python3
"""Externally bounded direct boot and SSM probe; never registers a GitHub runner."""
import argparse
import base64
import dataclasses
import datetime as dt
from pathlib import Path
import shlex
import subprocess
import time
from urllib.parse import unquote, urlparse

from example import (ROOT, AmiIdentity, AwsCommandError, Cloud, CobaltIdentity, ImageIdentity, ParentSelection,
                     file_sha, load_bindings, load_deployment, match, read_json, require, resource_tags, run,
                     OWNER_TAG, BUILD_TAG, tags_of, timestamp, utcnow, write_json)
from preflight import inspect_ami, inspect_instance_type, inspect_management, inspect_root_volume, resolve_parent
from qualification import QualificationRun


def wait_online(cloud, instance_id, deadline):
    while time.monotonic() < deadline:
        status = cloud.call("ssm", "describe-instance-information", {
            "Filters": [{"Key": "InstanceIds", "Values": [instance_id]}]})["InstanceInformationList"]
        if status and status[0]["PingStatus"] == "Online":
            checks = cloud.call("ec2", "describe-instance-status", {"InstanceIds": [instance_id], "IncludeAllInstances": True})["InstanceStatuses"]
            if checks and all(checks[0][kind]["Status"] == "ok" for kind in ("InstanceStatus", "SystemStatus")):
                return
        if cloud.instance(instance_id)["State"]["Name"] in ("terminated", "stopped", "shutting-down"):
            raise ValueError("probe instance stopped before SSM became available")
        time.sleep(10)
    raise TimeoutError("probe did not reach SSM before the boot deadline")


def ssm_output_key(value, bucket, region, prefix):
    url = urlparse(value)
    require(url.scheme == "https", "SSM output must use HTTPS")
    path = unquote(url.path.lstrip("/"))
    if url.netloc in (f"{bucket}.s3.{region}.amazonaws.com", f"{bucket}.s3.amazonaws.com"):
        key = path
    elif url.netloc in (f"s3.{region}.amazonaws.com", "s3.amazonaws.com"):
        require(path.startswith(bucket + "/"), "unexpected SSM output bucket")
        key = path[len(bucket) + 1:]
    else:
        raise ValueError("unexpected SSM output endpoint")
    require(key.startswith(prefix + "/"), "unexpected SSM output prefix")
    return key


def command(cloud, instance_id, script, build, deadline, output):
    d = cloud.deployment
    response = cloud.call("ssm", "send-command", {
        "InstanceIds": [instance_id], "DocumentName": "AWS-RunShellScript", "TimeoutSeconds": 60,
        "Parameters": {"commands": [script], "executionTimeout": [str(max(30, int(deadline - time.monotonic())))]},
        "OutputS3BucketName": d.artifact_bucket, "OutputS3KeyPrefix": f"{d.repository}/reports/ssm/{build}",
    })
    command_id = response["Command"]["CommandId"]
    write_json(output / "ssm-command.json", response)
    while time.monotonic() < deadline:
        try:
            invocation = cloud.call("ssm", "get-command-invocation", {"CommandId": command_id, "InstanceId": instance_id})
        except AwsCommandError as error:
            if error.code != "InvocationDoesNotExist":
                raise
            time.sleep(5)
            continue  # Eventual visibility of a newly issued command; bounded above.
        write_json(output / "ssm-invocation.json", invocation)
        if invocation["Status"] in ("Pending", "InProgress", "Delayed"):
            time.sleep(5)
            continue
        require(invocation["Status"] == "Success", f"guest probe command failed: {invocation['Status']}")
        url = urlparse(invocation.get("StandardOutputUrl", ""))
        if not url.netloc:
            time.sleep(5)
            continue
        key = ssm_output_key(invocation["StandardOutputUrl"], d.artifact_bucket, d.region, f"{d.repository}/reports/ssm/{build}")
        target = output / "guest-stdout.json"
        run(["aws", "s3api", "get-object", "--bucket", d.artifact_bucket, "--key", key,
             "--region", d.region, str(target)], timeout=90)
        return read_json(target)
    raise TimeoutError("guest command exceeded the probe deadline")


def diagnostics(cloud, instance_id, output):
    errors = []
    for name, operation, payload in [
        ("console", "get-console-output", {"InstanceId": instance_id, "Latest": True}),
        ("status", "describe-instance-status", {"InstanceIds": [instance_id], "IncludeAllInstances": True}),
        ("instance", "describe-instances", {"InstanceIds": [instance_id]}),
    ]:
        try:
            write_json(output / f"ec2-{name}.json", cloud.call("ec2", operation, payload))
        except (subprocess.SubprocessError, OSError) as error:
            errors.append(f"{name}: {error}")
    return errors


def probe(cloud, identity, build, output, result=None, fault=False):
    d = cloud.deployment
    image = inspect_ami(cloud, identity)
    instance_type = inspect_instance_type(cloud, d.instance_type, (identity,), d.vcpus)
    volume_gib = d.root_volume_gib if result else d.parent_root_volume_gib
    inspect_root_volume(image, volume_gib)
    inspect_management(cloud, (d.probe_profile_name,))
    if result:
        require(result["cloud"]["account_id"] == d.account_id and result["cloud"]["region"] == d.region, "candidate deployment differs")
        require(tags_of(image).get(OWNER_TAG) == d.repository
                and tags_of(image).get(BUILD_TAG) == result["execution"]["build_id"], "candidate ownership differs")
        require(tags_of(image).get("ami-example:recipe-id") == result["source"]["recipe_id"], "candidate recipe tag differs")
    expiry = timestamp(utcnow() + dt.timedelta(hours=2))
    tags = resource_tags(d, build, "probe", expiry)
    launch = {
        "ImageId": identity.id, "InstanceType": d.instance_type, "MinCount": 1, "MaxCount": 1,
        "ClientToken": f"ami-example-{build}-{identity.id}",
        "IamInstanceProfile": {"Name": d.probe_profile_name},
        "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": d.subnet_id, "Groups": [d.security_group_id],
                               "AssociatePublicIpAddress": not d.private, "DeleteOnTermination": True}],
        "MetadataOptions": {"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 1},
        "BlockDeviceMappings": [{"DeviceName": image["RootDeviceName"],
                                 "Ebs": {"VolumeSize": volume_gib, "VolumeType": "gp3", "Iops": 3000, "Throughput": 125,
                                         "Encrypted": True, "KmsKeyId": "alias/aws/ebs", "DeleteOnTermination": True}}],
        "TagSpecifications": [{"ResourceType": resource, "Tags": tags} for resource in ("instance", "volume")],
        "UserData": base64.b64encode((ROOT / "images/common/packer-user-data.sh").read_bytes()).decode(),
    }
    if instance_type.get("BurstablePerformanceSupported"):
        launch["CreditSpecification"] = {"CpuCredits": "standard"}
    write_json(output / "launch-request.json", launch)
    response = cloud.call("ec2", "run-instances", launch)
    instance_id = response["Instances"][0]["InstanceId"]
    observation = {"status": "failed", "build_id": build, "instance_id": instance_id,
                   "ami_id": identity.id, "started_at": timestamp()}
    write_json(output / "launch.json", response)
    try:
        deadline = time.monotonic() + d.deadlines["boot_seconds"]
        wait_online(cloud, instance_id, deadline)
        if result:
            release = result["payload"]["kernel_release"] + ("-intentional-failure" if fault else "")
            script = "set -eu\ncloud-init status --wait >/dev/null\n" + shlex.join([
                "/usr/local/bin/ami-example-guest-report", "--release", release,
                "--recipe", result["source"]["recipe_id"], "--stage", "probe"]) + "\n"
        else:
            encoded = base64.b64encode((ROOT / "images/common/inventory.py").read_bytes()).decode()
            script = ("set -eu\ncloud-init status --wait >/dev/null\npython3 - <<'PY'\n"
                      f"import base64\nexec(compile(base64.b64decode('{encoded}'), 'inventory.py', 'exec'))\nPY\n")
        # Initialization diagnostics go to SSM's durable stderr stream, separate from JSON stdout.
        script = ("trap 'journalctl -b --no-pager -n 500 >&2; cat /var/log/cloud-init-output.log >&2; "
                  "free -m >&2; df -B1 / >&2' EXIT\n") + script
        guest = command(cloud, instance_id, script, build, deadline, output)
        current = cloud.instance(instance_id)
        require(current["ImageId"] == identity.id and current["InstanceType"] == d.instance_type, "EC2 probe identity differs")
        require(current.get("CurrentInstanceBootMode") == identity.effective_boot_mode, "probe boot mode differs")
        if result:
            require(CobaltIdentity.parse(guest.get("xenomai")) == CobaltIdentity.parse(result["payload"].get("xenomai")),
                    "probe Cobalt identity differs")
            require(guest["identity"]["instanceId"] == instance_id and guest["identity"]["imageId"] == identity.id,
                    "guest and controller disagree about probe identity")
            require(guest["identity"]["accountId"] == d.account_id and guest["identity"]["region"] == d.region,
                    "guest cloud identity differs")
        else:
            require(not guest["registered"] and not guest["workspaces"] and not guest["secure_boot"], "parent is not a clean unsigned-kernel base")
            inventory = output / "inventory.json"
            write_json(inventory, guest)
            parent = AmiIdentity.parse({**dataclasses.asdict(identity), "inventory_file": inventory.name,
                                        "inventory_sha256": file_sha(inventory)}, "captured parent")
            write_json(output / "parent.json", parent.record())
        observation.update({"status": "passed", "guest": guest, "instance_type": current["InstanceType"],
                            "root_volume_gib": volume_gib})
        return observation
    except Exception as error:
        observation["error"] = str(error)
        raise
    finally:
        observation["diagnostic_errors"] = diagnostics(cloud, instance_id, output)
        try:
            cloud.call("ec2", "terminate-instances", {"InstanceIds": [instance_id]})
            cloud.wait_terminated([instance_id])
            observation["terminated"] = True
        finally:
            write_json(output / "probe.json", observation)
            for path in sorted(output.glob("*.json")):
                cloud.retain(path, f"{build}/probe/{path.name}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", help="resolved manifest for candidate qualification")
    parser.add_argument("--result")
    parser.add_argument("--capture-inventory", action="store_true", help="capture a selected parent without a build manifest")
    parser.add_argument("--bindings", help="infrastructure bindings for parent capture")
    parser.add_argument("--selection", help="parent selection JSON with exact id and owner")
    parser.add_argument("--build-id")
    parser.add_argument("--output", type=Path, required=True, help="probe evidence directory")
    parser.add_argument("--fault", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    require(bool(args.result) != bool(args.capture_inventory), "select either a candidate result or parent inventory capture")
    if args.capture_inventory:
        require(bool(args.bindings) and bool(args.selection) and not args.deployment,
                "parent capture requires --bindings and --selection")
        d = load_bindings(args.bindings)
        selection = ParentSelection.parse(read_json(args.selection))
    else:
        require(bool(args.deployment) and not args.bindings and not args.selection,
                "candidate qualification requires --deployment")
        d = load_deployment(args.deployment)
    if not args.execute:
        print("Would launch one disposable on-demand EC2 probe with an EBS root disk. Supply --execute only after cost approval.")
        return
    result = read_json(args.result) if args.result else None
    build = args.build_id or (QualificationRun.from_result(result).build_id if result else None)
    require(bool(build), "inventory capture requires a unique --build-id such as 1789200000-1-stock")
    match(build, r"[1-9][0-9]*-[1-9][0-9]*-(one|two|stock)", "probe build ID")
    cloud = Cloud(d)
    identity = (ImageIdentity.parse({"id": result["cloud"]["ami_id"], "owner": d.account_id,
                                     "architecture": result["cloud"]["architecture"],
                                     "boot_mode": result["cloud"]["ami_boot_mode"]}, "candidate")
                if result else resolve_parent(cloud, selection))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    observation = probe(cloud, identity, build, output, result, args.fault)
    if result:
        result["validation"]["direct_boot"] = observation
        write_json(args.result, result)
    print(f"Probe passed and instance terminated: {observation['instance_id']}")


if __name__ == "__main__":
    main()
