#!/usr/bin/env python3
"""Pack, publish, or fetch private configuration and accepted-image state."""
import argparse
import hashlib
import json
from pathlib import Path

from deployment_state import (ConfigurationBinding, StateStore, VersionedObject,
                              bundle_bytes, state_bucket)
from example import CleanupContext, load_cleanup_context, read_json, require, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack", help="capture one resolved manifest and its exact inventories")
    pack.add_argument("--deployment", required=True, type=Path)
    pack.add_argument("--output", required=True, type=Path)
    download = commands.add_parser("download", help="download an immutable execution record")
    download.add_argument("--cleanup-context", required=True, type=Path)
    download.add_argument("--uri", required=True)
    download.add_argument("--sha256", required=True)
    download.add_argument("--output", required=True, type=Path)
    for name in ("publish", "fetch", "fetch-accepted", "promote"):
        command = commands.add_parser(name)
        command.add_argument("--account-id", required=True)
        command.add_argument("--region", required=True)
        command.add_argument("--repository", required=True)
        command.add_argument("--output", required=True, type=Path)
        if name in ("fetch", "fetch-accepted"):
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--state-root")
            source.add_argument("--uri")
            command.add_argument("--sha256")
        else:
            command.add_argument("--state-root", required=True)
        if name == "publish":
            command.add_argument("--bundle", required=True, type=Path)
        if name == "promote":
            command.add_argument("--record", required=True, type=Path)
            command.add_argument("--deployment", required=True, type=Path)
            command.add_argument("--qualification", required=True, type=Path)
            previous = command.add_mutually_exclusive_group(required=True)
            previous.add_argument("--previous-version")
            previous.add_argument("--empty", action="store_true")
    args = parser.parse_args()
    if args.command == "pack":
        content = bundle_bytes(args.deployment)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(content)
        print(json.dumps({"bundle_path": str(args.output.resolve()), "sha256": hashlib.sha256(content).hexdigest()}))
        return
    if args.command == "download":
        binding = ConfigurationBinding.parse({"uri": args.uri, "sha256": args.sha256})
        StateStore(load_cleanup_context(args.cleanup_context)).download(binding, args.output)
        return
    binding = None
    if getattr(args, "uri", None):
        require(args.sha256 is not None, "exact-version fetch requires --sha256")
        binding = ConfigurationBinding.parse({"uri": args.uri, "sha256": args.sha256})
        bucket = VersionedObject.parse(binding.uri).bucket
    else:
        require(getattr(args, "sha256", None) is None, "--sha256 requires --uri")
        bucket = state_bucket(args.state_root, args.repository)
    context = CleanupContext.parse({"repository": args.repository, "account_id": args.account_id,
                                    "region": args.region, "artifact_bucket": bucket})
    store = StateStore(context)
    if args.command == "publish":
        result = store.publish(args.bundle).as_dict()
        write_json(args.output, result)
    elif args.command == "fetch":
        result = store.fetch(args.output, binding)
    elif args.command == "fetch-accepted":
        result = store.fetch_accepted(args.output, binding)
    else:
        result = store.promote(read_json(args.record), args.deployment, args.qualification, args.previous_version).as_dict()
        write_json(args.output, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
