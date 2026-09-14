#!/usr/bin/env python3
"""Inspect deployment choices or generate explicit deployment input files."""
import argparse
import dataclasses

from deployment_config import (assemble_bindings, assemble_manifest, bootstrap_values, foundation_inputs,
                               inspect, load_spec, management_inputs)
from example import load_bindings, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("selection", "terraform", "inspect", "bindings"):
        command = commands.add_parser(name)
        command.add_argument("--spec", required=True)
        command.add_argument("--output", required=True)
        if name == "selection":
            command.add_argument("--parent", choices=("source", "controller"), required=True)
        if name == "terraform":
            command.add_argument("--layer", choices=("foundation", "management"), required=True)
            command.add_argument("--foundation")
            command.add_argument("--observations")
        if name == "inspect":
            command.add_argument("--subnet")
        if name == "bindings":
            for option in ("foundation", "management", "observations", "runs-on-inventory"):
                command.add_argument("--" + option, required=True)
    command = commands.add_parser("bootstrap")
    command.add_argument("--bindings", required=True)
    command.add_argument("--output", required=True)
    command = commands.add_parser("manifest")
    for option in ("bindings", "source-parent", "controller-parent", "output"):
        command.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        write_json(args.output, bootstrap_values(load_bindings(args.bindings)))
        return
    if args.command == "manifest":
        print(assemble_manifest(args.bindings, args.source_parent, args.controller_parent, args.output))
        return
    spec = load_spec(args.spec)
    if args.command == "selection":
        result = spec.values[args.parent + "_ami"]
    elif args.command == "inspect":
        result = inspect(spec, args.subnet)
    elif args.command == "terraform":
        if args.layer == "foundation":
            result = foundation_inputs(spec)
        else:
            if not args.foundation or not args.observations:
                parser.error("management inputs require --foundation and --observations")
            result = management_inputs(spec, read_json(args.foundation), read_json(args.observations))
    else:
        result = dataclasses.asdict(assemble_bindings(spec, read_json(args.foundation), read_json(args.management),
                                                     read_json(args.observations), read_json(args.runs_on_inventory)))
    if args.command == "bindings":
        result["parent_selections"] = {role: spec.values[role + "_ami"] for role in ("source", "controller")}
    write_json(args.output, result)


if __name__ == "__main__":
    main()
