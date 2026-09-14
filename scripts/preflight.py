#!/usr/bin/env python3
"""Read-only AWS admission checks before launching any candidate resources."""
import argparse

from example import Cloud, ImageIdentity, load_deployment, parse_time, require, utcnow, write_json


def resolve_parent(cloud, selection):
    images = cloud.call("ec2", "describe-images", {"ImageIds": [selection.id], "Owners": [selection.owner]})["Images"]
    require(len(images) == 1, f"AMI owner/visibility mismatch: {selection.id}")
    image = images[0]
    require(image.get("ImageId") == selection.id and image.get("OwnerId") == selection.owner,
            "parent AMI selection differs from AWS")
    return ImageIdentity.parse({"id": selection.id, "owner": selection.owner,
                                "architecture": image["Architecture"],
                                "boot_mode": image.get("BootMode", "legacy-bios")}, "selected parent")


def inspect_ami(cloud, identity):
    images = cloud.call("ec2", "describe-images", {"ImageIds": [identity.id], "Owners": [identity.owner]})["Images"]
    require(len(images) == 1, f"AMI owner/visibility mismatch: {identity.id}")
    image = images[0]
    for field, expected in {"ImageId": identity.id, "OwnerId": identity.owner, "Architecture": identity.architecture,
                            "State": "available", "VirtualizationType": "hvm", "RootDeviceType": "ebs"}.items():
        require(image.get(field) == expected, f"AMI {field} mismatch: {image.get(field)} != {expected}")
    require(image.get("BootMode", "legacy-bios") == identity.boot_mode, "AMI boot mode differs")
    require(not image.get("Public", False) or identity.owner != cloud.deployment.account_id,
            "account-owned source/candidate images must remain private")
    deprecation = image.get("DeprecationTime")
    require(not deprecation or parse_time(deprecation) > utcnow(), "AMI is already deprecated")
    if identity.owner == cloud.deployment.account_id:
        permissions = cloud.call("ec2", "describe-image-attribute", {"ImageId": identity.id, "Attribute": "launchPermission"})
        require(not permissions.get("LaunchPermissions"), "qualification images must be private to this account")
    for mapping in image["BlockDeviceMappings"]:
        if "Ebs" not in mapping:
            continue
        if identity.owner == cloud.deployment.account_id:
            snapshot = cloud.call("ec2", "describe-snapshots", {"SnapshotIds": [mapping["Ebs"]["SnapshotId"]]})["Snapshots"][0]
            if snapshot.get("Encrypted"):
                key = cloud.call("kms", "describe-key", {"KeyId": snapshot["KmsKeyId"]})["KeyMetadata"]
                require(key["KeyState"] == "Enabled" and key["KeyManager"] == "AWS",
                        "this single-account adapter supports the enabled AWS-managed EBS key only")
    return image


def inspect_instance_type(cloud, name, identities, vcpus=None):
    instance_type = cloud.call("ec2", "describe-instance-types", {"InstanceTypes": [name]})["InstanceTypes"][0]
    require(instance_type["InstanceType"] == name, "instance type differs")
    if vcpus is not None:
        require(instance_type["VCpuInfo"]["DefaultVCpus"] == vcpus, "instance vCPU selection differs")
    require("x86_64" in instance_type["ProcessorInfo"]["SupportedArchitectures"], "instance is not x86_64")
    require(instance_type["Hypervisor"] == "nitro", "initial qualification requires Nitro")
    for identity in identities:
        require(identity.effective_boot_mode in instance_type["SupportedBootModes"], "instance does not support locked boot mode")
    return instance_type


def inspect_root_volume(image, size):
    roots = [m["Ebs"] for m in image["BlockDeviceMappings"] if m["DeviceName"] == image["RootDeviceName"] and "Ebs" in m]
    require(len(roots) == 1 and roots[0]["VolumeSize"] <= size, "configured root volume is smaller than the AMI")


def inspect_management(cloud, profiles):
    d = cloud.deployment
    subnet = cloud.call("ec2", "describe-subnets", {"SubnetIds": [d.subnet_id]})["Subnets"][0]
    group = cloud.call("ec2", "describe-security-groups", {"GroupIds": [d.security_group_id]})["SecurityGroups"][0]
    require(subnet["VpcId"] == group["VpcId"] == d.vpc_id, "management network VPC differs")
    require(group["IpPermissions"] == [], "management security group must have no inbound rules")
    for name in profiles:
        cloud.call("iam", "get-instance-profile", {"InstanceProfileName": name})
    cloud.artifacts()
    return subnet


def inspect_deployment(cloud):
    d = cloud.deployment
    source = inspect_ami(cloud, d.source_ami)
    controller = inspect_ami(cloud, d.controller_ami)
    instance_type = inspect_instance_type(cloud, d.instance_type, (d.source_ami, d.controller_ami), d.vcpus)
    builder_type = inspect_instance_type(cloud, d.builder_instance_type, (d.source_ami,))
    for image in (source, controller):
        inspect_root_volume(image, d.parent_root_volume_gib)
    inspect_root_volume(source, d.root_volume_gib)
    subnet = inspect_management(cloud, (d.builder_profile_name, d.probe_profile_name))
    return {"source_ami": source, "controller_ami": controller, "instance_type": instance_type,
            "builder_instance_type": builder_type, "subnet": subnet}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", required=True, help="resolved deployment manifest")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = inspect_deployment(Cloud(load_deployment(args.deployment)))
    write_json(args.output, result)
    print("Read-only cloud preflight passed; no resources were created.")


if __name__ == "__main__":
    main()
