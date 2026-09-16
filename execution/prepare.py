#!/usr/bin/env python3
"""Prepare immutable, nonsecret inputs for the Cobalt execution workflow."""
import argparse
import json
import os
from pathlib import Path
import sys

import yaml

from contract import ExecutionPlan


def prepare(*, installation=None, image=None, event=None, repository=None):
    if event is not None:
        document = json.loads(Path(event).read_text())
        if not isinstance(document, dict):
            raise ValueError("GitHub event must contain an object")
        return ExecutionPlan.from_inputs(document.get("inputs"), repository)
    return ExecutionPlan.parse(
        yaml.safe_load(Path(installation).read_text()),
        yaml.safe_load(Path(image).read_text()), repository)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--event", type=Path, help="GitHub workflow_dispatch event file")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"),
                        help="optionally check that OWNER/REPO belongs to this installation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, help="GitHub job output file")
    args = parser.parse_args()
    if args.event:
        if args.installation or args.image:
            parser.error("--event cannot be combined with --installation or --image")
        if not args.repository:
            parser.error("--event requires --repository or GITHUB_REPOSITORY")
    elif not (args.installation and args.image):
        parser.error("provide both --installation and --image, or --event")
    plan = prepare(installation=args.installation, image=args.image, event=args.event,
                   repository=args.repository)
    inputs = json.dumps(plan.as_inputs(), indent=2) + "\n"
    if len(inputs.encode()) > 65535:
        raise ValueError("execution inputs exceed GitHub's dispatch payload limit")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(inputs)
    if args.github_output:
        with args.github_output.open("a") as stream:
            stream.write(f"ami_id={plan.image.ami_id}\n"
                         f"environment={plan.installation.environment}\n"
                         f"region={plan.installation.region}\n")
    print(f"Prepared {plan.image.ami_id} for {plan.installation.name}: {args.output}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"Cannot prepare execution: {error}", file=sys.stderr)
        raise SystemExit(1)
