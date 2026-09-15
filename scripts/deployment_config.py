"""Operator choices and pure assembly of resolved deployment inputs."""
from __future__ import annotations

import copy
import dataclasses
import ipaddress
import os
from pathlib import Path

from example import (Cloud, CloudTarget, CleanupContext, ImageIdentity, InfrastructureBindings, ParentSelection,
                     BuildInputs, INSTANCE_TYPE, load_bindings, load_parent_record, match,
                     read_json, require, write_json)

DEFAULTS = {
    "environment": "ami-build", "name_prefix": "ami-example", "subnet_id": None,
    "instance_type": "t3.small", "builder_instance_type": "c7i.large", "root_volume_gib": 16,
    "parent_root_volume_gib": 30, "retain_hours": 24, "private": True,
    "artifact_retention_days": 365, "github_oidc_subject_prefix": None, "infrastructure": {},
    "deadlines": {"boot_seconds": 900, "registration_seconds": 900, "workflow_seconds": 14400},
}
REQUIRED = {"schema_version", "account_id", "region", "repository", "source_ami", "controller_ami", "runs_on"}
FOUNDATION_OPTIONS = {"artifact_bucket_name", "existing_oidc_provider_arn", "existing_controller_role_arn",
                      "operator_user_arn", "existing_builder_profile_name", "existing_probe_profile_name",
                      "existing_artifact_bucket"}


@dataclasses.dataclass(frozen=True)
class OperatorSpec:
    values: dict
    directory: Path

    @classmethod
    def parse(cls, value: dict, directory: Path | None = None) -> OperatorSpec:
        require(isinstance(value, dict), "operator spec must be an object")
        require(REQUIRED <= set(value) and not (set(value) - REQUIRED - set(DEFAULTS)), "operator spec fields differ")
        require(value["schema_version"] == 1, "unsupported operator spec version")
        resolved = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(value)}
        CloudTarget.parse({key: resolved[key] for key in ("account_id", "region")})
        match(resolved["repository"], r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "repository")
        match(resolved["environment"], r"[A-Za-z0-9_-]+", "environment")
        match(resolved["name_prefix"], r"[a-z0-9-]{3,32}", "name_prefix")
        for name in ("source_ami", "controller_ami"):
            ParentSelection.parse(resolved[name], name)
        for name in ("instance_type", "builder_instance_type"):
            match(resolved[name], INSTANCE_TYPE, name)
        if resolved["subnet_id"] is not None:
            match(resolved["subnet_id"], r"subnet-[0-9a-f]{17}", "subnet_id")
        for name in ("root_volume_gib", "parent_root_volume_gib"):
            require(type(resolved[name]) is int and 8 <= resolved[name] <= 256, f"{name} must be 8..256")
        require(type(resolved["retain_hours"]) is int and 1 <= resolved["retain_hours"] <= 168, "retain_hours must be 1..168")
        require(type(resolved["artifact_retention_days"]) is int and resolved["artifact_retention_days"] >= 30,
                "artifact_retention_days must be at least 30")
        require(type(resolved["private"]) is bool, "private must be boolean")
        deadlines = resolved["deadlines"]
        require(isinstance(deadlines, dict) and set(deadlines) == set(DEFAULTS["deadlines"]), "deadline keys differ")
        for key, seconds in deadlines.items():
            require(type(seconds) is int and 60 <= seconds <= 18000, f"invalid deadline: {key}")
        require(deadlines["workflow_seconds"] > deadlines["registration_seconds"], "workflow deadline too short")
        options = resolved["infrastructure"]
        require(isinstance(options, dict) and not (set(options) - FOUNDATION_OPTIONS - {"existing_security_group_id"}),
                "unsupported infrastructure option")
        require(isinstance(resolved["runs_on"], dict) and bool(resolved["runs_on"].get("stack_name")),
                "runs_on must select a stack_name")
        return cls(resolved, (directory or Path.cwd()).resolve())

    def scope(self) -> dict:
        return {key: self.values[key] for key in ("account_id", "region", "repository")}


def load_spec(path: str | Path) -> OperatorSpec:
    path = Path(path).resolve()
    return OperatorSpec.parse(read_json(path), path.parent)


def terraform_outputs(value: dict) -> dict:
    require(isinstance(value, dict), "Terraform output must be an object")
    return {key: item["value"] if isinstance(item, dict) and "value" in item else item for key, item in value.items()}


@dataclasses.dataclass(frozen=True)
class FoundationBindings(CleanupContext):
    environment: str
    name_prefix: str
    controller_role_arn: str
    controller_role_managed: bool
    builder_profile_name: str
    probe_profile_name: str
    management_role_arns: dict[str, str]
    management_profile_arns: dict[str, str]
    oidc_provider_arn: str
    ebs_key_arn: str

    @classmethod
    def from_outputs(cls, outputs: dict) -> FoundationBindings:
        raw = terraform_outputs(outputs)
        fields = {f.name for f in dataclasses.fields(cls)}
        require(fields <= set(raw), "foundation bindings are incomplete")
        value = {key: raw[key] for key in fields}
        CleanupContext.parse({f.name: value[f.name] for f in dataclasses.fields(CleanupContext)})
        account = value["account_id"]
        role = rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+"
        match(value["controller_role_arn"], role, "controller role")
        require(type(value["controller_role_managed"]) is bool, "controller role ownership must be boolean")
        match(value["environment"], r"[A-Za-z0-9_-]+", "environment")
        match(value["name_prefix"], r"[a-z0-9-]{3,32}", "name_prefix")
        for field, pattern in (("management_role_arns", role),
                               ("management_profile_arns", rf"arn:aws:iam::{account}:instance-profile/[A-Za-z0-9+=,.@_/-]+")):
            require(isinstance(value[field], dict) and set(value[field]) == {"builder", "probe"}, f"{field} needs builder and probe")
            for item in value[field].values():
                match(item, pattern, field)
        for name in ("builder", "probe"):
            match(value[name + "_profile_name"], r"[A-Za-z0-9+=,.@_-]+", name + " profile")
            require(value["management_profile_arns"][name].rsplit("/", 1)[1] == value[name + "_profile_name"],
                    "management profile name and ARN differ")
        require(value["oidc_provider_arn"] == f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com", "OIDC provider account differs")
        match(value["ebs_key_arn"], rf"arn:aws:kms:{value['region']}:{account}:key/[A-Za-z0-9-]+", "EBS key")
        return cls(**value)


def require_scope(value: dict, spec: OperatorSpec, name: str) -> None:
    require(all(value.get(key) == expected for key, expected in spec.scope().items()), f"{name} deployment scope differs")


def foundation_inputs(spec: OperatorSpec) -> dict:
    value = spec.values
    fields = ("account_id", "region", "repository", "environment", "name_prefix", "artifact_retention_days", "github_oidc_subject_prefix")
    return {**{key: value[key] for key in fields},
            **{key: item for key, item in value["infrastructure"].items() if key in FOUNDATION_OPTIONS}}


@dataclasses.dataclass(frozen=True)
class ObservedParent:
    image: ImageIdentity
    root_volume_gib: int


@dataclasses.dataclass(frozen=True)
class DeploymentObservations:
    scope: dict[str, str]
    network: dict[str, str]
    instance_type: str
    vcpus: int
    parents: dict[str, ObservedParent]

    @classmethod
    def parse(cls, spec: OperatorSpec, observed: dict) -> DeploymentObservations:
        require_scope(observed, spec, "inspection")
        require(observed.get("schema_version") == 1, "unsupported inspection version")
        network = observed["network"]
        require(set(network) == {"subnet_id", "vpc_id", "vpc_cidr"}, "network observation fields differ")
        subnet = match(network["subnet_id"], r"subnet-[0-9a-f]{17}", "observed subnet")
        vpc = match(network["vpc_id"], r"vpc-[0-9a-f]{17}", "observed VPC")
        cidr = str(ipaddress.IPv4Network(network["vpc_cidr"]))
        if spec.values["subnet_id"] is not None:
            require(subnet == spec.values["subnet_id"], "selected subnet differs from inspection")
        machine = observed["instance_type"]
        require(machine["name"] == spec.values["instance_type"], "selected instance type differs from inspection")
        require(type(machine["vcpus"]) is int and 1 <= machine["vcpus"] <= 64, "invalid observed vCPU count")
        parents = {}
        for role in ("source", "controller"):
            parent = observed["parents"][role]
            identity = ImageIdentity.parse({key: parent[key] for key in ("id", "owner", "architecture", "boot_mode")})
            selection = ParentSelection.parse(spec.values[role + "_ami"])
            require((identity.id, identity.owner) == (selection.id, selection.owner), f"{role} parent selection differs")
            volume = parent["root_volume_gib"]
            require(type(volume) is int and 8 <= volume <= spec.values["parent_root_volume_gib"], f"{role} parent root exceeds selected volume")
            if role == "source":
                require(volume <= spec.values["root_volume_gib"], "source parent root exceeds candidate root volume")
            parents[role] = ObservedParent(identity, volume)
        return cls(spec.scope(), {"subnet_id": subnet, "vpc_id": vpc, "vpc_cidr": cidr}, machine["name"], machine["vcpus"], parents)

    def record(self) -> dict:
        return {"schema_version": 1, **self.scope, "network": self.network,
                "instance_type": {"name": self.instance_type, "vcpus": self.vcpus},
                "parents": {role: {**dataclasses.asdict(parent.image), "root_volume_gib": parent.root_volume_gib}
                            for role, parent in self.parents.items()}}


def management_inputs(spec: OperatorSpec, foundation: dict, observed: dict) -> dict:
    foundation = dataclasses.asdict(FoundationBindings.from_outputs(foundation))
    require_scope(foundation, spec, "foundation")
    require(all(foundation[key] == spec.values[key] for key in ("environment", "name_prefix")),
            "foundation environment or name prefix differs from operator spec")
    observed = DeploymentObservations.parse(spec, observed)
    value = spec.values
    result = {"foundation": foundation, **observed.network,
              "source_ami_id": value["source_ami"]["id"], "controller_ami_id": value["controller_ami"]["id"],
              **{key: value[key] for key in ("instance_type", "builder_instance_type", "root_volume_gib", "parent_root_volume_gib")}}
    if "existing_security_group_id" in value["infrastructure"]:
        result["existing_security_group_id"] = value["infrastructure"]["existing_security_group_id"]
    return result


def assemble_bindings(spec: OperatorSpec, foundation: dict, management: dict, observed: dict, service: dict) -> InfrastructureBindings:
    foundation_binding = FoundationBindings.from_outputs(foundation)
    foundation, management = dataclasses.asdict(foundation_binding), terraform_outputs(management)
    require(all(foundation[key] == spec.values[key] for key in ("environment", "name_prefix")),
            "foundation environment or name prefix differs from operator spec")
    require(FoundationBindings.from_outputs(management.get("foundation", {})) == foundation_binding,
            "management policy foundation bindings differ")
    for name, output in (("foundation", foundation), ("management", management), ("RunsOn inspection", service)):
        require_scope(output, spec, name)
    observed = DeploymentObservations.parse(spec, observed)
    expected_management = {**observed.network,
                           "source_ami_id": spec.values["source_ami"]["id"], "controller_ami_id": spec.values["controller_ami"]["id"],
                           **{key: spec.values[key] for key in ("instance_type", "builder_instance_type", "root_volume_gib", "parent_root_volume_gib")}}
    require(all(management.get(key) == value for key, value in expected_management.items()),
            "management policy image or network bindings differ")
    require(service.get("stack_name") == spec.values["runs_on"]["stack_name"], "inspected RunsOn stack differs")
    require(service["runs_on"]["environment"] == spec.values["runs_on"].get("environment", spec.values["environment"]),
            "inspected RunsOn environment differs")
    value = spec.values
    names = ("account_id", "region", "repository", "environment", "instance_type", "builder_instance_type",
             "root_volume_gib", "parent_root_volume_gib", "deadlines", "retain_hours", "private")
    result = {key: value[key] for key in names}
    result.update({key: foundation[key] for key in ("controller_role_arn", "builder_profile_name", "probe_profile_name", "artifact_bucket")})
    result.update(security_group_id=management["security_group_id"], vpc_id=observed.network["vpc_id"],
                  subnet_id=observed.network["subnet_id"], vcpus=observed.vcpus, runs_on=service["runs_on"])
    return InfrastructureBindings.parse(result)


def assemble_manifest(bindings_path: str | Path, source_parent: str | Path, controller_parent: str | Path, output: str | Path) -> Path:
    selections = read_json(bindings_path).get("parent_selections")
    require(isinstance(selections, dict) and set(selections) == {"source", "controller"},
            "bindings must record the provisioned parent selections")
    selected = {role: ParentSelection.parse(value, role) for role, value in selections.items()}
    bindings = load_bindings(bindings_path)
    installation = bindings.require_runs_on()
    parents = {"source_ami": load_parent_record(source_parent),
               "controller_ami": load_parent_record(controller_parent, installation)}
    for role, selection in selected.items():
        parent = parents[role + "_ami"]
        require((parent.id, parent.owner) == (selection.id, selection.owner), f"captured {role} parent differs from provisioned selection")
    inputs = BuildInputs(**{f.name: getattr(bindings, f.name) for f in dataclasses.fields(bindings)}, **parents)
    destination = Path(output).resolve()
    value = dataclasses.asdict(inputs)
    for name, parent in parents.items():
        value[name]["inventory_file"] = os.path.relpath(parent.inventory_path, destination.parent)
    BuildInputs.parse(value, base_dir=destination.parent)
    require(not destination.exists(), "manifest output already exists")
    write_json(destination, value)
    return destination


def inspect(spec: OperatorSpec, subnet_id: str | None = None) -> dict:
    """Read exact selected resource facts; never launch or modify a resource."""
    value = spec.values
    subnet_id = subnet_id or value["subnet_id"]
    match(subnet_id, r"subnet-[0-9a-f]{17}", "select a subnet for inspection")
    if value["subnet_id"] is not None:
        require(subnet_id == value["subnet_id"], "subnet argument differs from operator spec")
    cloud = Cloud(CloudTarget.parse({key: value[key] for key in ("account_id", "region")}))
    subnets = cloud.call("ec2", "describe-subnets", {"SubnetIds": [subnet_id]})["Subnets"]
    require(len(subnets) == 1 and subnets[0]["OwnerId"] == value["account_id"], "selected subnet owner differs")
    subnet = subnets[0]
    vpc = cloud.call("ec2", "describe-vpcs", {"VpcIds": [subnet["VpcId"]]})["Vpcs"][0]
    machine = cloud.call("ec2", "describe-instance-types", {"InstanceTypes": [value["instance_type"]]})["InstanceTypes"][0]
    parents = {}
    for role in ("source", "controller"):
        selection = ParentSelection.parse(value[role + "_ami"])
        images = cloud.call("ec2", "describe-images", {"ImageIds": [selection.id], "Owners": [selection.owner]})["Images"]
        require(len(images) == 1 and images[0]["State"] == "available", f"{role} parent is unavailable")
        image = images[0]
        roots = [entry["Ebs"] for entry in image["BlockDeviceMappings"] if entry["DeviceName"] == image["RootDeviceName"] and "Ebs" in entry]
        require(len(roots) == 1, "parent must have one EBS root device")
        parents[role] = {"id": image["ImageId"], "owner": image["OwnerId"], "architecture": image["Architecture"],
                         "boot_mode": image.get("BootMode", "legacy-bios"), "root_volume_gib": roots[0]["VolumeSize"]}
    result = {"schema_version": 1, **spec.scope(), "network": {"subnet_id": subnet_id, "vpc_id": vpc["VpcId"], "vpc_cidr": vpc["CidrBlock"]},
              "instance_type": {"name": machine["InstanceType"], "vcpus": machine["VCpuInfo"]["DefaultVCpus"]}, "parents": parents}
    return DeploymentObservations.parse(spec, result).record()


def bootstrap_values(bindings: InfrastructureBindings) -> dict:
    return {"repository_variables": {"AMI_DEPLOYMENT_ENVIRONMENT": bindings.environment},
            "environment_variables": {"AMI_CONTROLLER_ROLE_ARN": bindings.controller_role_arn,
                                      "AMI_REGION": bindings.region,
                                      "AMI_STATE_URI": f"s3://{bindings.artifact_bucket}/{bindings.repository}/state"},
            "cleanup_context": dataclasses.asdict(bindings.cleanup())}
