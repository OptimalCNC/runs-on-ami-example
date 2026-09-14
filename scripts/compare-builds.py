#!/usr/bin/env python3
"""Compare qualified builds by platform content, excluding cloud/run identity."""
import argparse
import dataclasses

from example import CobaltIdentity, read_json, require, write_json


@dataclasses.dataclass(frozen=True)
class QualifiedBuild:
    recipe_id: str
    build_id: str
    ami_id: str
    payload: dict

    @classmethod
    def parse(cls, value):
        require(value["schema_version"] == 1 and value["status"] == "qualified", "both builds must pass cloud qualification")
        require(value["lifecycle"]["cleanup"]["status"] == "passed", "cleanup must be verified first")
        probe = value["validation"]["direct_boot"]
        runners = value["validation"]["runs_on"]
        require(probe["status"] == "passed" and probe.get("terminated") is True, "direct boot proof is missing")
        require(len(runners) == 2 and {r.get("stage") for r in runners} == {"a", "b"}
                and all(r["status"] == "passed" for r in runners), "two passed RunsOn reports are required")
        require(len({probe["instance_id"], *(r["instance_id"] for r in runners)}) == 3, "fresh-instance proof is missing")
        identity = CobaltIdentity.parse(value["payload"].get("xenomai"))
        for observation in (probe, *runners):
            require(CobaltIdentity.parse(observation.get("guest", {}).get("xenomai")) == identity,
                    "qualified Cobalt identity differs")
        require(all(r.get("application") == {"name": "cobalt", "status": "passed"} for r in runners),
                "passed Cobalt application evidence is required")
        return cls(value["source"]["recipe_id"], value["execution"]["build_id"], value["cloud"]["ami_id"], value["payload"])


def compare(first, second):
    first, second = QualifiedBuild.parse(first), QualifiedBuild.parse(second)
    require(first.recipe_id == second.recipe_id, "recipes differ; this is not a reproducibility comparison")
    require(first.build_id != second.build_id, "two independent builds are required")
    require(first.ami_id != second.ami_id, "candidate AMI was reused")
    fields = ("kernel_release", "config_sha256", "payload_hashes", "xenomai", "xenomai_files", "packages_sha256", "snap_hashes", "normalized_configuration", "initramfs_content", "toolchain")
    differences = {}
    for field in fields:
        a, b = first.payload[field], second.payload[field]
        if a != b:
            if isinstance(a, dict) and isinstance(b, dict):
                differences[field] = {key: {"first": a.get(key), "second": b.get(key)} for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)}
            else:
                differences[field] = {"first": a, "second": b}
    raw_equal = first.payload["initramfs_sha256"] == second.payload["initramfs_sha256"]
    return {"schema_version": 1, "status": "passed" if not differences else "failed",
            "recipe_id": first.recipe_id, "build_ids": [r.build_id for r in (first, second)],
            "compared_fields": list(fields), "differences": differences,
            "raw_initramfs_equal": raw_equal,
            "scope": "Cobalt kernel/modules and userspace, effective config, packages, boot/image config, and unpacked initramfs files/modes",
            "excluded": ["AMI IDs", "snapshot IDs and disk bytes", "run IDs", "creation timestamps", "raw initramfs compression/container bytes"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("first")
    parser.add_argument("second")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    first, second = read_json(args.first), read_json(args.second)
    result = compare(first, second)
    write_json(args.output, result)
    for path, image in ((args.first, first), (args.second, second)):
        image["validation"]["reproducibility"] = result
        write_json(path, image)
    print(f"Reproducibility comparison: {result['status']}")
    require(result["status"] == "passed", f"payload differs; inspect {args.output}")


if __name__ == "__main__":
    main()
