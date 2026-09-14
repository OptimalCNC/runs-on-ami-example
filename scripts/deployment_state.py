"""Publish and materialize private deployment inputs through versioned S3 objects."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid

from example import (AwsCommandError, Cloud, SHA256, canonical, load_cleanup_context, load_deployment,
                     match, read_json, require, run, write_json)


BUNDLE_NAMES = {"deployment.json", "parents/source.json", "parents/controller.json"}
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


@dataclasses.dataclass(frozen=True)
class VersionedObject:
    bucket: str
    key: str
    version_id: str

    @classmethod
    def parse(cls, uri):
        require(isinstance(uri, str), "versioned object URI must be a string")
        parts = urlsplit(uri)
        values = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        require(parts.scheme == "s3" and not parts.fragment and set(values) == {"versionId"}
                and len(values["versionId"]) == 1, "object URI must identify one immutable S3 version")
        bucket = match(parts.netloc, r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", "state bucket")
        key = unquote(parts.path.removeprefix("/"))
        require(bool(key) and all(part not in ("", ".", "..") for part in key.split("/")), "invalid state object key")
        version = values["versionId"][0]
        require(version not in ("", "null"), "S3 object has no immutable version")
        return cls(bucket, key, version)

    @property
    def uri(self):
        return f"s3://{self.bucket}/{quote(self.key, safe='/')}?versionId={quote(self.version_id, safe='')}"


@dataclasses.dataclass(frozen=True)
class ConfigurationBinding:
    uri: str
    sha256: str

    @classmethod
    def parse(cls, value):
        require(isinstance(value, dict) and set(value) == {"uri", "sha256"}, "state binding fields differ")
        reference = VersionedObject.parse(value["uri"])
        return cls(reference.uri, match(value["sha256"], SHA256, "state object digest"))

    def as_dict(self):
        return dataclasses.asdict(self)


def bundle_bytes(deployment_path):
    """Normalize locations while retaining exact inventory bytes and image identity."""
    deployment = load_deployment(deployment_path)
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = deployment.snapshot(Path(temporary) / "deployment")
        files = {"deployment.json": canonical(read_json(snapshot))}
        for name in ("parents/source.json", "parents/controller.json"):
            files[name] = (snapshot.parent / name).read_bytes()
    destination = io.BytesIO()
    with tarfile.open(fileobj=destination, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(content))
    result = destination.getvalue()
    require(len(result) <= MAX_BUNDLE_BYTES, "configuration bundle exceeds 32 MiB")
    return result


def unpack_bundle(content, destination):
    """Read only the three regular bundle members; never extract arbitrary paths."""
    require(len(content) <= MAX_BUNDLE_BYTES, "configuration bundle exceeds 32 MiB")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        for member in archive:
            require(member.name in BUNDLE_NAMES and member.name not in files and member.isfile()
                    and 0 <= member.size <= MAX_BUNDLE_BYTES, "unexpected configuration bundle member")
            files[member.name] = archive.extractfile(member).read()
    require(set(files) == BUNDLE_NAMES, "configuration bundle is incomplete")
    manifest = json.loads(files["deployment.json"])
    for role in ("source", "controller"):
        require(manifest[f"{role}_ami"]["inventory_file"] == f"parents/{role}.json",
                "bundle inventory references must stay inside the bundle")
    destination = Path(destination)
    require(not destination.exists(), f"materialization destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "deployment"
        for name, value in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        require(bundle_bytes(staging / "deployment.json") == content, "configuration bundle is not canonical")
        staging.rename(destination)
    return load_deployment(destination / "deployment.json")


def verify_materialized(deployment_path, binding):
    if not isinstance(binding, ConfigurationBinding):
        binding = ConfigurationBinding.parse(binding)
    require(hashlib.sha256(bundle_bytes(deployment_path)).hexdigest() == binding.sha256,
            "materialized deployment differs from its immutable configuration binding")
    return load_deployment(deployment_path)


def require_identity(deployment, context):
    for name in ("repository", "account_id", "region", "artifact_bucket"):
        require(getattr(deployment, name) == getattr(context, name), f"deployment {name} differs from bootstrap")


def state_bucket(uri, repository):
    parts = urlsplit(uri)
    require(parts.scheme == "s3" and not parts.query and not parts.fragment
            and unquote(parts.path).rstrip("/") == f"/{repository}/state",
            "state root must be s3://BUCKET/REPOSITORY/state")
    return match(parts.netloc, r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", "state bucket")


class StateStore:
    """S3 operations constrained to a verified deployment's durable state prefix."""

    def __init__(self, context):
        self.context = context
        self.cloud = Cloud(context)
        self.bucket = self.cloud.artifacts()
        self.prefix = f"{context.repository}/state/"

    def require_reference(self, reference):
        require(reference.bucket == self.bucket.name and reference.key.startswith(self.prefix),
                "state object belongs to another deployment")

    def head(self, key):
        return self.cloud.call("s3api", "head-object", {"Bucket": self.bucket.name, "Key": key,
                                                        "ExpectedBucketOwner": self.bucket.account_id})

    def current(self, name):
        key = self.prefix + name
        metadata = self.head(key)
        version = metadata.get("VersionId")
        require(isinstance(version, str) and version not in ("", "null"), "state object has no immutable S3 version")
        return VersionedObject(self.bucket.name, key, version)

    def read(self, reference):
        self.require_reference(reference)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "object"
            response = json.loads(run(["aws", "s3api", "get-object", "--bucket", reference.bucket,
                "--key", reference.key, "--version-id", reference.version_id,
                "--expected-bucket-owner", self.bucket.account_id, "--region", self.bucket.region,
                "--no-cli-pager", "--output", "json", str(target)], timeout=90, stderr=subprocess.PIPE))
            require(response.get("VersionId") == reference.version_id, "S3 returned another state object version")
            return target.read_bytes()

    def download(self, binding, destination):
        content = self.read(VersionedObject.parse(binding.uri))
        require(hashlib.sha256(content).hexdigest() == binding.sha256, "state object digest differs")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def put(self, name, content, condition=None):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "object"
            source.write_bytes(content)
            args = ["aws", "s3api", "put-object", "--bucket", self.bucket.name, "--key", self.prefix + name,
                    "--body", str(source), "--server-side-encryption", "AES256",
                    "--expected-bucket-owner", self.bucket.account_id, "--region", self.bucket.region,
                    "--no-cli-pager", "--output", "json"]
            if condition:
                args.extend(condition)
            try:
                response = json.loads(run(args, timeout=90, stderr=subprocess.PIPE))
            except subprocess.CalledProcessError as error:
                raise AwsCommandError(error) from error
        version = response.get("VersionId")
        require(isinstance(version, str) and version not in ("", "null"), "state upload did not produce an immutable S3 version")
        reference = VersionedObject(self.bucket.name, self.prefix + name, version)
        return ConfigurationBinding(reference.uri, hashlib.sha256(content).hexdigest())

    def publish(self, bundle):
        content = Path(bundle).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            deployment = unpack_bundle(content, Path(temporary) / "deployment")
            require_identity(deployment, self.context)
        return self.put("configuration/current.tar", content)

    def fetch(self, destination, binding=None):
        reference = VersionedObject.parse(binding.uri) if binding else self.current("configuration/current.tar")
        require(reference.key == self.prefix + "configuration/current.tar", "configuration URI identifies another kind of state")
        content = self.read(reference)
        observed = ConfigurationBinding(reference.uri, hashlib.sha256(content).hexdigest())
        require(binding is None or observed == binding, "configuration bundle digest differs")
        destination = Path(destination)
        with tempfile.TemporaryDirectory() as temporary:
            deployment = unpack_bundle(content, Path(temporary) / "deployment")
            require_identity(deployment, self.context)
        deployment = unpack_bundle(content, destination)
        write_json(destination / "binding.json", observed.as_dict())
        write_json(destination / "cleanup-context.json", dataclasses.asdict(deployment.cleanup()))
        return {**observed.as_dict(), "deployment_path": str((destination / "deployment.json").resolve()),
                "cleanup_context_path": str((destination / "cleanup-context.json").resolve())}

    def fetch_accepted(self, destination, binding=None):
        reference = VersionedObject.parse(binding.uri) if binding else self.current("acceptance/current.json")
        require(reference.key == self.prefix + "acceptance/current.json", "acceptance URI identifies another kind of state")
        content = self.read(reference)
        observed = ConfigurationBinding(reference.uri, hashlib.sha256(content).hexdigest())
        require(binding is None or observed == binding, "accepted selection digest differs")
        envelope = json.loads(content)
        require(isinstance(envelope, dict) and set(envelope) == {"schema_version", "revision", "record"}
                and envelope["schema_version"] == 1, "accepted selection envelope differs")
        match(envelope["revision"], r"[0-9a-f]{32}", "acceptance revision")
        from accepted_image import AcceptedImage
        AcceptedImage.parse(envelope["record"], self.context)
        write_json(destination, envelope["record"])
        write_json(str(destination) + ".binding.json", observed.as_dict())
        return observed.as_dict()

    def retain_qualification(self, content):
        checksum = hashlib.sha256(content).hexdigest()
        name = f"qualification/{checksum}.json"
        try:
            return self.put(name, content, ["--if-none-match", "*"])
        except AwsCommandError as error:
            if error.code != "PreconditionFailed":
                raise
            reference = self.current(name)
            require(self.read(reference) == content, "retained qualification evidence differs")
            return ConfigurationBinding(reference.uri, checksum)

    def promote(self, record, deployment_path, qualification_path, previous_version=None):
        from accepted_image import AcceptedImage, selected_record
        deployment = load_cleanup_context(deployment_path)
        require_identity(deployment, self.context)
        selected = AcceptedImage.parse(record, deployment)
        qualification_bytes = Path(qualification_path).read_bytes()
        checksum = hashlib.sha256(qualification_bytes).hexdigest()
        qualification = json.loads(qualification_bytes)
        expected = selected_record(qualification, deployment, checksum, selected.qualification_url)
        require(record == expected, "accepted selection differs from its qualification evidence")
        selected.inspect(Cloud(deployment))
        condition = ["--if-none-match", "*"]
        if previous_version is not None:
            require(previous_version not in ("", "null"), "previous selection version must be immutable")
            current = self.head(self.prefix + "acceptance/current.json")
            require(current.get("VersionId") == previous_version, "accepted selection changed before promotion")
            require(isinstance(current.get("ETag"), str) and bool(current["ETag"]), "accepted selection has no conditional-write ETag")
            condition = ["--if-match", current["ETag"]]
        evidence = self.retain_qualification(qualification_bytes)
        retained = selected_record(qualification, deployment, checksum, evidence.uri)
        envelope = {"schema_version": 1, "revision": uuid.uuid4().hex, "record": retained}
        return self.put("acceptance/current.json", canonical(envelope), condition)
