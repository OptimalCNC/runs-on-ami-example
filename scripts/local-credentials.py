#!/usr/bin/env python3
"""AWS credential_process: deliver an assumed session directly to the calling CLI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--session-name", default="ami-example-local")
    parser.add_argument("--duration-seconds", type=int, default=21600)
    args = parser.parse_args()
    private = Path(__file__).resolve().parents[1] / ".aws-local"
    env = dict(os.environ, AWS_CONFIG_FILE=str(private / "config"),
               AWS_SHARED_CREDENTIALS_FILE=str(private / "credentials"))
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        env.pop(name, None)
    response = subprocess.run([
        "aws", "--profile", "ami-example-operator", "sts", "assume-role", "--role-arn", args.role_arn,
        "--role-session-name", args.session_name, "--duration-seconds", str(args.duration_seconds),
        "--output", "json", "--no-cli-pager",
    ], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if response.returncode:
        raise SystemExit("Operator AssumeRole failed; verify the operator profile and controller trust.")
    credentials = json.loads(response.stdout)["Credentials"]
    # credential_process stdout is a private protocol pipe to AWS CLI, never a log.
    json.dump({"Version": 1, **credentials}, sys.stdout)


if __name__ == "__main__":
    main()
