#!/usr/bin/env python3
"""AWS credential_process: deliver an assumed session directly to the calling CLI."""
import argparse
import json
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--source-profile", help="AWS profile used to assume the role; otherwise use normal AWS credential selection")
    parser.add_argument("--session-name", default="ami-example-local")
    parser.add_argument("--duration-seconds", type=int, default=21600)
    args = parser.parse_args()
    command = ["aws"]
    if args.source_profile:
        command += ["--profile", args.source_profile]
    response = subprocess.run([
        *command, "sts", "assume-role", "--role-arn", args.role_arn,
        "--role-session-name", args.session_name, "--duration-seconds", str(args.duration_seconds),
        "--output", "json", "--no-cli-pager",
    ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if response.returncode:
        raise SystemExit("Operator AssumeRole failed; verify the operator profile and controller trust.")
    credentials = json.loads(response.stdout)["Credentials"]
    # credential_process stdout is a private protocol pipe to AWS CLI, never a log.
    json.dump({"Version": 1, **credentials}, sys.stdout)


if __name__ == "__main__":
    main()
