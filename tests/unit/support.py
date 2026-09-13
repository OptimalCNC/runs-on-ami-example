import copy
import dataclasses
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from example import Deployment, OWNER_TAG, BUILD_TAG, PURPOSE_TAG, EXPIRY_TAG, RUNS_ON_TAG, tags_of


def module(name):
    key = name.replace("-", "_")
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    sys.modules[key] = result
    spec.loader.exec_module(result)
    return result


def deployment_dict():
    value = json.loads((ROOT / "infra/deployment.example.json").read_text())
    value.update(repository="example/repo", account_id="123456789012", region="us-east-1",
                 runs_on={"environment": "ami-example", "version": "3.2.0", "bootstrap_version": "0.1.12"}, vpc_id="vpc-0123456789abcdef0", subnet_id="subnet-0123456789abcdef0",
                 security_group_id="sg-0123456789abcdef0", controller_role_arn="arn:aws:iam::123456789012:role/example",
                 builder_profile_name="example-builder", probe_profile_name="example-probe", artifact_bucket="example-artifacts")
    for ami in ("source_ami", "controller_ami"):
        value[ami].update(id="ami-0123456789abcdef0", owner="123456789012", inventory_sha256="1" * 64)
    return value


def deployment():
    return Deployment.parse(deployment_dict())


def tags(build="123-1-one", purpose="candidate", owner="example/repo", expiry="2020-01-01T00:00:00Z"):
    return [{"Key": key, "Value": value} for key, value in {
        OWNER_TAG: owner, BUILD_TAG: build, PURPOSE_TAG: purpose, EXPIRY_TAG: expiry,
    }.items()]


def result():
    d = deployment()
    sha = "1" * 64
    parent = {"packages_sha256": sha, "package_inventory": "pkg\t1\tamd64\tinstalled\n", "runner_version": "2.328.0",
              "runner_listener_sha256": sha, "bootstrap_files": {"/usr/local/bin/runs-on-bootstrap-v0.1.12": sha},
              "registered": False, "workspaces": [], "secure_boot": False, "os_version": "24.04", "snap_hashes": {}}
    value = {
        "schema_version": 1, "status": "candidate",
        "source": {"recipe_id": sha, "recipe_commit": "2" * 40, "recipe_files": {"script": sha}, "input_lock_sha256": sha,
                   "parent_ami": dataclasses.asdict(d.source_ami)},
        "payload": {"schema_version": 1, "recipe_id": sha, "kernel_release": "6.12.90-cip24-xenomai-cobalt", "config_sha256": sha,
                    "payload_hashes": {"/boot/vmlinuz": sha}, "packages_sha256": sha, "package_inventory": "pkg\t1\tamd64\tinstalled\n", "normalized_configuration": {"/etc/fstab": sha},
                    "initramfs_sha256": sha, "initramfs_content": {"main/init": {"sha256": sha, "mode": "0o755"}},
                    "xenomai": {"version": "3.3.3", "core": "cobalt", "prefix": "/usr/xenomai"},
                    "xenomai_files": {"lib/libcobalt.so.2": {"sha256": sha, "mode": "0o755"}},
                    "toolchain": {"gcc": "gcc 13.3.0", "ld": "ld 2.42"}, "parent_inventory": parent},
        "cloud": {"account_id": d.account_id, "region": d.region, "ami_id": "ami-11111111111111111",
                  "snapshot_ids": ["snap-11111111111111111"], "architecture": "x86_64", "boot_mode": "uefi", "ami_boot_mode": "uefi", "instance_type": d.instance_type},
        "execution": {"build_id": "123-1-one", "workflow_ref": "example/repo/.github/workflows/build-and-test-image.yml@refs/heads/main",
                      "run_id": "123", "run_attempt": "1", "controller_instance_id": "i-00000000000000000", "runs_on_version": d.require_runs_on().version,
                      "tool_versions": {"packer": "1.16.0", "amazon_plugin": "1.8.2", "aws_cli": "2.31.8", "session_manager": "1.2.707.0"},
                      "inherited_runner_version": "2.328.0", "bootstrap_files": parent["bootstrap_files"]},
        "validation": {"direct_boot": {"status": "pending"}, "runs_on": [], "reproducibility": {"status": "pending"}},
        "lifecycle": {"created_at": "2026-09-12T01:00:00Z", "expires_at": "2026-09-12T07:00:00Z", "retain": False,
                      "artifact_locations": ["s3://example-artifacts/example/repo/123-1-one/"], "cleanup": {"status": "pending"}},
    }
    value["payload"]["snap_hashes"] = {}
    return value


def guest(stage, digit):
    return {"schema_version": 1, "kernel_release": "6.12.90-cip24-xenomai-cobalt", "recipe_id": "1" * 64, "config_sha256": "1" * 64,
            "cmdline": "BOOT_IMAGE=/boot/vmlinuz-6.12.90-cip24-xenomai-cobalt", "machine_id": digit * 32, "boot_mode": "uefi",
            "identity": {"instanceId": "i-" + digit * 17, "imageId": "ami-11111111111111111", "accountId": "123456789012",
                         "region": "us-east-1", "instanceType": "t3.small"}, "runner_version": "2.328.0", "stage": stage,
            "runner_listener_sha256": "1" * 64, "packages_sha256": "1" * 64,
            "snap_hashes": {},
            "xenomai": {"version": "3.3.3", "core": "cobalt", "prefix": "/usr/xenomai"},
            "bootstrap_files": {"/usr/local/bin/runs-on-bootstrap-v0.1.12": "1" * 64},
            "sentinel": "/var/tmp/ami-example-123-1-one-sentinel", "sentinel_absent_at_start": True}


def smoke(stage, digit):
    identity = guest(stage, digit)
    return {"identity": identity, "environment": {**copy.deepcopy(identity), "environment_passed": True}, "ctest_passed": True}


def evidence():
    return {f"smoke-{stage}": {"id": index, "runner_id": index + 10, "conclusion": "success",
                             "runner_name": f"runs-on--i-{str(index + 1) * 17}--123"} for index, stage in enumerate(("a", "b"), 1)}


def instance(digit, purpose="test", **overrides):
    return {"InstanceId": "i-" + digit * 17, "ImageId": "ami-11111111111111111", "InstanceType": "t3.small",
            "CurrentInstanceBootMode": "uefi",
            "LaunchTime": "2026-09-12T01:01:00Z", "State": {"Name": "running"},
            "Tags": tags(purpose=purpose) + ([{"Key": RUNS_ON_TAG, "Value": "example/repo"}] if purpose == "test" else []), **overrides}


class FakeCloud:
    def __init__(self):
        self.deployment = deployment()
        self.images = [{"ImageId": "ami-11111111111111111", "OwnerId": self.deployment.account_id, "Tags": tags(),
                        "CreationDate": "2026-09-12T01:00:00Z", "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-11111111111111111"}}]}]
        self.machines = [instance("2"), instance("3")]
        self.snapshots = [{"SnapshotId": "snap-11111111111111111", "Tags": tags()}]
        self.volumes = []
        self.keys = []
        self.mutations = []
        self.retained = []
        self.fail_retention = False

    def instances(self, filters):
        values = self.machines
        for filter in filters:
            if filter["Name"] == "image-id":
                values = [v for v in values if v["ImageId"] in filter["Values"]]
            elif filter["Name"].startswith("tag:"):
                values = [v for v in values if tags_of(v).get(filter["Name"][4:]) in filter["Values"]]
        return copy.deepcopy(values)

    def instance(self, identifier):
        return copy.deepcopy(next(v for v in self.machines if v["InstanceId"] == identifier))

    def wait_terminated(self, ids, seconds=600):
        assert all(self.instance(i)["State"]["Name"] == "terminated" for i in ids)

    def retain(self, path, key):
        if self.fail_retention:
            raise OSError("simulated artifact outage")
        self.retained.append(key)
        return "s3://example-artifacts/" + key

    def call(self, service, operation, payload=None):
        payload = payload or {}
        if operation.startswith("describe-"):
            mapping = {"describe-images": ("Images", self.images, "ImageIds", "ImageId"),
                       "describe-snapshots": ("Snapshots", self.snapshots, "SnapshotIds", "SnapshotId"),
                       "describe-volumes": ("Volumes", self.volumes, "VolumeIds", "VolumeId"),
                       "describe-key-pairs": ("KeyPairs", self.keys, "KeyPairIds", "KeyPairId")}
            if operation in mapping:
                name, values, ids, identifier = mapping[operation]
                if ids in payload:
                    values = [v for v in values if v[identifier] in payload[ids]]
                for filter in payload.get("Filters", []):
                    if filter["Name"].startswith("tag:"):
                        values = [v for v in values if tags_of(v).get(filter["Name"][4:]) in filter["Values"]]
                return {name: copy.deepcopy(values)}
            if operation == "describe-instances":
                return {"Reservations": [{"Instances": [self.instance(i) for i in payload["InstanceIds"]]}]}
            if operation == "describe-instance-status":
                return {"InstanceStatuses": []}
        if operation == "get-console-output":
            return {"Output": ""}
        self.mutations.append((operation, payload))
        if operation == "terminate-instances":
            for machine in self.machines:
                if machine["InstanceId"] in payload["InstanceIds"]:
                    machine["State"]["Name"] = "terminated"
        elif operation == "create-tags":
            for machine in self.machines:
                if machine["InstanceId"] in payload["Resources"]:
                    values = {**tags_of(machine), **{tag["Key"]: tag["Value"] for tag in payload["Tags"]}}
                    machine["Tags"] = [{"Key": key, "Value": value} for key, value in values.items()]
        elif operation == "deregister-image":
            self.images = [v for v in self.images if v["ImageId"] != payload["ImageId"]]
        elif operation == "delete-snapshot":
            self.snapshots = [v for v in self.snapshots if v["SnapshotId"] != payload["SnapshotId"]]
        elif operation == "delete-volume":
            self.volumes = [v for v in self.volumes if v["VolumeId"] != payload["VolumeId"]]
        elif operation == "delete-key-pair":
            self.keys = [v for v in self.keys if v["KeyPairId"] != payload["KeyPairId"]]
        else:
            raise AssertionError(f"unhandled fake AWS operation: {operation}")
        return {}
