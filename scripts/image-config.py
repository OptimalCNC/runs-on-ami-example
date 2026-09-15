#!/usr/bin/env python3
"""Write deployment settings and optional controller image settings as JSON."""
import argparse

from example import load_bindings, load_deployment, match, write_json


def configuration(deployment, build=None):
    config = {"region": deployment.region, "role_arn": deployment.controller_role_arn, "environment": deployment.environment}
    if build is not None:
        build = match(build, r"[1-9][0-9]*-[1-9][0-9]*-(one|two|stock)", "build ID")
        config.update(build_id=build, controller_ami_id=deployment.controller_ami.id)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True, help="resolved deployment manifest")
    parser.add_argument("--build-id", help="include controller image settings for this execution")
    parser.add_argument("--output", required=True, help="configuration JSON destination")
    args = parser.parse_args(argv)
    deployment = (load_deployment(args.deployment) if args.build_id is not None
                  else load_bindings(args.deployment))
    write_json(args.output, configuration(deployment, args.build_id))


if __name__ == "__main__":
    main()
