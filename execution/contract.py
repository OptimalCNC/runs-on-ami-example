"""Parse installation and publication records into an executable image request."""

from dataclasses import asdict, dataclass
import re

import yaml


def mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def text(value, pattern: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {label}")
    return value


def integer(value, minimum: int, label: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"invalid {label}")
    return value


def version(value, label: str) -> str:
    return text(value, r"v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", label).removeprefix("v")


def record(value, kind: str) -> dict:
    value = mapping(value, kind)
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1 or value.get("kind") != kind:
        raise ValueError(f"expected a version 1 {kind} record")
    return value


@dataclass(frozen=True)
class Installation:
    name: str
    account_id: str
    region: str
    environment: str
    github_organization: str
    runner_profile_arn: str
    bootstrap_version: str
    runner_max_runtime_minutes: int

    @classmethod
    def parse(cls, value: dict) -> "Installation":
        value = record(value, "runs-on-installation")
        account = text(value.get("account_id"), r"[0-9]{12}", "installation account_id")
        runtime = mapping(value.get("runtime"), "installation runtime")
        versions = mapping(value.get("versions"), "installation versions")
        return cls(
            name=text(value.get("name"), r"[a-z][a-z0-9-]{2,23}", "installation name"),
            account_id=account,
            region=text(value.get("region"), r"[a-z]{2}-[a-z]+-[1-9][0-9]*", "installation region"),
            environment=text(value.get("environment"), r"[a-z][a-z0-9-]*", "RunsOn environment"),
            github_organization=text(value.get("github_organization"), r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", "GitHub organization"),
            runner_profile_arn=text(
                runtime.get("runner_profile_arn"),
                rf"arn:aws:iam::{account}:instance-profile/(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]+",
                "runner instance profile ARN",
            ),
            bootstrap_version=version(versions.get("bootstrap"), "installation bootstrap version"),
            runner_max_runtime_minutes=integer(runtime.get("runner_max_runtime_minutes"), 15, "runner maximum runtime"),
        )

    def as_record(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "runs-on-installation",
            "name": self.name,
            "account_id": self.account_id,
            "region": self.region,
            "environment": self.environment,
            "github_organization": self.github_organization,
            "versions": {"bootstrap": f"v{self.bootstrap_version}"},
            "runtime": {
                "runner_profile_arn": self.runner_profile_arn,
                "runner_max_runtime_minutes": self.runner_max_runtime_minutes,
            },
        }


@dataclass(frozen=True)
class ImageCompatibility:
    architecture: str
    boot_mode: str
    secure_boot: bool
    minimum_root_volume_gib: int
    ena_support: bool
    runs_on_bootstrap_version: str

    @classmethod
    def parse(cls, value: dict) -> "ImageCompatibility":
        value = mapping(value, "image compatibility")
        if (value.get("architecture") != "x86_64" or value.get("boot_mode") != "uefi"
                or value.get("secure_boot") is not False or value.get("ena_support") is not True):
            raise ValueError("image must support x86_64, UEFI without Secure Boot, and ENA")
        return cls(
            architecture="x86_64", boot_mode="uefi", secure_boot=False, ena_support=True,
            minimum_root_volume_gib=integer(value.get("minimum_root_volume_gib"), 1, "image minimum root volume", 16),
            runs_on_bootstrap_version=version(value.get("runs_on_bootstrap_version"), "image bootstrap version"),
        )


@dataclass(frozen=True)
class PublishedImage:
    ami_id: str
    snapshot_id: str
    publication_id: str
    disk_sha256: str
    disk_size_bytes: int
    recipe_id: str
    manifest_sha256: str
    compatibility: ImageCompatibility
    target_name: str
    account_id: str
    region: str

    @classmethod
    def parse(cls, value: dict) -> "PublishedImage":
        value = record(value, "published-image")
        if value.get("status") != "available":
            raise ValueError("published image must be available")
        target = mapping(value.get("target"), "publishing target")
        artifact = mapping(value.get("artifact"), "published artifact")
        account = text(value.get("account_id"), r"[0-9]{12}", "image account_id")
        region = text(value.get("region"), r"[a-z]{2}-[a-z]+-[1-9][0-9]*", "image region")
        if target.get("account_id") != account or target.get("region") != region:
            raise ValueError("image identity differs from its publishing target")
        compatibility = ImageCompatibility.parse(value.get("compatibility"))
        size = integer(artifact.get("size_bytes"), 1, "image disk size")
        if size != compatibility.minimum_root_volume_gib * 1024**3:
            raise ValueError("image disk size differs from its root volume requirement")
        return cls(
            ami_id=text(value.get("ami_id"), r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})", "AMI ID"),
            snapshot_id=text(value.get("snapshot_id"), r"snap-(?:[0-9a-f]{8}|[0-9a-f]{17})", "snapshot ID"),
            publication_id=text(value.get("publication_id"), r"[0-9a-f]{32}", "publication ID"),
            disk_sha256=text(artifact.get("sha256"), r"[0-9a-f]{64}", "disk digest"),
            disk_size_bytes=size,
            recipe_id=text(artifact.get("recipe_id"), r"[0-9a-f]{64}", "recipe ID"),
            manifest_sha256=text(artifact.get("manifest_sha256"), r"[0-9a-f]{64}", "manifest digest"),
            compatibility=compatibility,
            target_name=text(target.get("name"), r"[a-z][a-z0-9-]{2,23}", "publishing target name"),
            account_id=account,
            region=region,
        )

    def as_record(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "published-image",
            "status": "available",
            "publication_id": self.publication_id,
            "target": {"name": self.target_name, "account_id": self.account_id, "region": self.region},
            "account_id": self.account_id,
            "region": self.region,
            "artifact": {
                "sha256": self.disk_sha256,
                "size_bytes": self.disk_size_bytes,
                "recipe_id": self.recipe_id,
                "manifest_sha256": self.manifest_sha256,
            },
            "compatibility": asdict(self.compatibility),
            "snapshot_id": self.snapshot_id,
            "ami_id": self.ami_id,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    installation: Installation
    image: PublishedImage

    @classmethod
    def parse(cls, installation: dict, image: dict, repository: str | None = None) -> "ExecutionPlan":
        installation = Installation.parse(installation)
        image = PublishedImage.parse(image)
        if (installation.name, installation.account_id, installation.region) != (image.target_name, image.account_id, image.region):
            raise ValueError("published image belongs to a different installation")
        if installation.bootstrap_version != image.compatibility.runs_on_bootstrap_version:
            raise ValueError("image and installation bootstrap versions differ")
        if repository is not None:
            repository = text(repository, r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "GitHub repository")
            if repository.split("/", 1)[0].lower() != installation.github_organization.lower():
                raise ValueError("repository owner differs from the installation's GitHub organization")
        return cls(installation, image)

    @classmethod
    def from_inputs(cls, inputs: dict, repository: str | None = None) -> "ExecutionPlan":
        inputs = mapping(inputs, "workflow inputs")
        records = []
        for key in ("installation", "published_image"):
            source = inputs.get(key)
            if not isinstance(source, str):
                raise ValueError(f"{key} input must contain YAML text")
            try:
                records.append(yaml.safe_load(source))
            except yaml.YAMLError as error:
                raise ValueError(f"{key} input must contain valid YAML") from error
        return cls.parse(*records, repository=repository)

    def as_inputs(self) -> dict[str, str]:
        return {
            "installation": yaml.safe_dump(self.installation.as_record(), sort_keys=False),
            "published_image": yaml.safe_dump(self.image.as_record(), sort_keys=False),
        }
