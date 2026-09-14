"""Configuration state preserves complete inputs and exact historical versions."""
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import deployment_dict, result
from accepted_image import selected_record
from example import CleanupContext, InvalidInput, VersionedBucket, canonical, read_json, write_json
from deployment_state import (ConfigurationBinding, StateStore, VersionedObject, bundle_bytes,
                              state_bucket, unpack_bundle, verify_materialized)


def manifest(directory, folder="inventories"):
    directory = Path(directory)
    value = deployment_dict()
    inventory = result()["payload"]["parent_inventory"]
    inventory["packages_sha256"] = hashlib.sha256(inventory["package_inventory"].encode()).hexdigest()
    for role in ("source", "controller"):
        name = f"{folder}/{role}.json"
        write_json(directory / name, inventory)
        value[f"{role}_ami"]["inventory_file"] = name
        value[f"{role}_ami"]["inventory_sha256"] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    path = directory / "manifest.json"
    write_json(path, value)
    return path


def context():
    return CleanupContext.parse({"repository": "example/repo", "account_id": "123456789012",
                                 "region": "us-east-1", "artifact_bucket": "example-artifacts"})


def reference(name="configuration/current.tar", version="version/1+two"):
    return VersionedObject("example-artifacts", "example/repo/state/" + name, version)


def store():
    cloud = MagicMock()
    cloud.artifacts.return_value = VersionedBucket("example-artifacts", "123456789012", "us-east-1")
    with patch("deployment_state.Cloud", return_value=cloud):
        value = StateStore(context())
    return value


def promotion_inputs(directory):
    directory = Path(directory)
    qualification = result()
    qualification["status"] = "qualified"
    qualification["lifecycle"].update(retain=True, expires_at="2099-01-01T00:00:00Z",
        cleanup={"status": "passed", "retained_images": [qualification["cloud"]["ami_id"]]})
    qualification["validation"].update(direct_boot={"status": "passed"},
                                      runs_on=[{"status": "passed"}, {"status": "passed"}])
    evidence = directory / "qualification.json"
    deployment = directory / "context.json"
    write_json(evidence, qualification)
    write_json(deployment, dataclasses.asdict(context()))
    checksum = hashlib.sha256(evidence.read_bytes()).hexdigest()
    return selected_record(qualification, context(), checksum, "https://example.com/qualification"), deployment, evidence


class BundleInputs(unittest.TestCase):
    def test_relocation_preserves_complete_bundle_and_materialized_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = bundle_bytes(manifest(root / "first", "original"))
            second = bundle_bytes(manifest(root / "second", "elsewhere"))
            self.assertEqual(first, second)
            unpack_bundle(first, root / "copy")
            bound = ConfigurationBinding(reference().uri, hashlib.sha256(first).hexdigest())
            loaded = verify_materialized(root / "copy/deployment.json", bound)
            self.assertEqual(loaded.repository, "example/repo")
            self.assertEqual(first, bundle_bytes(root / "copy/deployment.json"))

    def test_changed_manifest_cannot_claim_an_older_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = manifest(temporary)
            bound = ConfigurationBinding(reference().uri, hashlib.sha256(bundle_bytes(source)).hexdigest())
            value = read_json(source)
            value["retain_hours"] += 1
            write_json(source, value)
            with self.assertRaisesRegex(InvalidInput, "differs from its immutable"):
                verify_materialized(source, bound)

    def test_archive_never_extracts_unlisted_paths_links_or_duplicates(self):
        cases = [("../outside", tarfile.REGTYPE), ("deployment.json", tarfile.SYMTYPE),
                 ("deployment.json", tarfile.REGTYPE)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (name, kind) in enumerate(cases):
                content = io.BytesIO()
                with tarfile.open(fileobj=content, mode="w") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "../outside" if kind == tarfile.SYMTYPE else ""
                    archive.addfile(member, io.BytesIO(b""))
                    if index == 2:
                        archive.addfile(member, io.BytesIO(b""))
                destination = root / str(index)
                with self.assertRaises(InvalidInput):
                    unpack_bundle(content.getvalue(), destination)
                self.assertFalse(destination.exists())
            self.assertFalse((root.parent / "outside").exists())

    def test_wrong_bootstrap_identity_leaves_no_materialized_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = bundle_bytes(manifest(root / "source"))
            value = store()
            value.context = dataclasses.replace(context(), account_id="999999999999")
            bound = ConfigurationBinding(reference().uri, hashlib.sha256(content).hexdigest())
            with patch.object(value, "read", return_value=content):
                with self.assertRaisesRegex(InvalidInput, "account_id differs from bootstrap"):
                    value.fetch(root / "fetched", bound)
            self.assertFalse((root / "fetched").exists())


class VersionedState(unittest.TestCase):
    def test_version_uri_roundtrip_and_mutable_uri_rejection(self):
        self.assertEqual(VersionedObject.parse(reference().uri), reference())
        for uri in ("s3://example-artifacts/a", "s3://example-artifacts/a?versionId=null",
                    "s3://example-artifacts/a?versionId=a&versionId=b",
                    "s3://example-artifacts/a?versionId=a#fragment"):
            with self.assertRaises(InvalidInput):
                VersionedObject.parse(uri)
        self.assertEqual(state_bucket("s3://example-artifacts/example/repo/state", "example/repo"), "example-artifacts")
        with self.assertRaises(InvalidInput):
            state_bucket("s3://example-artifacts/another/repo/state", "example/repo")

    def test_read_uses_frozen_version_even_when_current_changes(self):
        value = store()
        value.cloud.call.return_value = {"VersionId": "old"}
        selected = value.current("configuration/current.tar")
        value.cloud.call.return_value = {"VersionId": "new"}

        def aws(args, **kwargs):
            self.assertEqual(args[args.index("--version-id") + 1], "old")
            self.assertEqual(args[args.index("--expected-bucket-owner") + 1], "123456789012")
            Path(args[-1]).write_bytes(b"old contents")
            return json.dumps({"VersionId": "old"})

        with patch("deployment_state.run", side_effect=aws):
            self.assertEqual(value.read(selected), b"old contents")

    def test_wrong_version_digest_or_namespace_cannot_be_materialized(self):
        value = store()
        other = VersionedObject("example-artifacts", "other/repo/state/execution.json", "1")
        with patch("deployment_state.run") as aws:
            with self.assertRaisesRegex(InvalidInput, "another deployment"):
                value.read(other)
            aws.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "record.json"
            with patch.object(value, "read", return_value=b"changed"):
                with self.assertRaisesRegex(InvalidInput, "digest differs"):
                    value.download(ConfigurationBinding(reference().uri, "1" * 64), destination)
            self.assertFalse(destination.exists())

    def test_upload_returns_its_own_version_without_reading_current(self):
        value = store()
        with patch("deployment_state.run", return_value=json.dumps({"VersionId": "uploaded"})):
            bound = value.put("configuration/current.tar", b"configuration")
        self.assertEqual(VersionedObject.parse(bound.uri).version_id, "uploaded")
        self.assertEqual(bound.sha256, hashlib.sha256(b"configuration").hexdigest())
        value.cloud.call.assert_not_called()

    def test_promotion_checks_prior_version_then_uses_conditional_write(self):
        value = store()
        value.cloud.call.return_value = {"VersionId": "previous", "ETag": '"previous-tag"'}
        def binding(name, content, condition):
            return ConfigurationBinding(reference(name).uri, hashlib.sha256(content).hexdigest())
        with (tempfile.TemporaryDirectory() as temporary,
              patch("accepted_image.AcceptedImage.inspect") as inspect,
              patch("deployment_state.Cloud"), patch.object(value, "put", side_effect=binding) as put):
            accepted, deployment, evidence = promotion_inputs(temporary)
            value.promote(accepted, deployment, evidence, "previous")
            inspect.assert_called_once()
            arguments = put.call_args.args
            self.assertEqual(arguments[0], "acceptance/current.json")
            self.assertEqual(arguments[2], ["--if-match", '"previous-tag"'])
            first = json.loads(arguments[1])
            stored = dict(first["record"]["image"])
            stored.pop("qualification_url")
            self.assertEqual(stored, {key: val for key, val in accepted["image"].items() if key != "qualification_url"})
            evidence_uri = first["record"]["image"]["qualification_url"]
            checksum = accepted["image"]["qualification_sha256"]
            self.assertEqual(VersionedObject.parse(evidence_uri).key, f"example/repo/state/qualification/{checksum}.json")
            self.assertEqual(put.call_args_list[0].args[1], evidence.read_bytes())
            value.promote(accepted, deployment, evidence, "previous")
            self.assertNotEqual(first["revision"], json.loads(put.call_args.args[1])["revision"])
            put.reset_mock()
            with self.assertRaisesRegex(InvalidInput, "changed before promotion"):
                value.promote(accepted, deployment, evidence, "older")
            put.assert_not_called()
            value.promote(accepted, deployment, evidence)
            self.assertEqual(put.call_args.args[2], ["--if-none-match", "*"])

    def test_evidence_digest_and_selected_fields_must_match_before_upload(self):
        value = store()
        with (tempfile.TemporaryDirectory() as temporary,
              patch("accepted_image.AcceptedImage.inspect") as inspect,
              patch.object(value, "put") as put):
            for field, wrong in (("qualification_sha256", "0" * 64), ("ami_id", "ami-22222222222222222")):
                accepted, deployment, evidence = promotion_inputs(temporary)
                accepted["image"][field] = wrong
                with self.assertRaisesRegex(InvalidInput, "differs from its qualification evidence"):
                    value.promote(accepted, deployment, evidence)
            inspect.assert_not_called()
            put.assert_not_called()

    def test_acceptance_digest_covers_envelope_and_outputs_original_record(self):
        value = store()
        accepted = {"schema_version": 1, "image": {"example": True}}
        content = canonical({"schema_version": 1, "revision": "1" * 32, "record": accepted})
        bound = ConfigurationBinding(reference("acceptance/current.json").uri, hashlib.sha256(content).hexdigest())
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "accepted.json"
            with patch.object(value, "read", return_value=content), patch("accepted_image.AcceptedImage.parse"):
                value.fetch_accepted(destination, bound)
            self.assertEqual(read_json(destination), accepted)
            self.assertEqual(read_json(str(destination) + ".binding.json"), bound.as_dict())


if __name__ == "__main__":
    unittest.main()
