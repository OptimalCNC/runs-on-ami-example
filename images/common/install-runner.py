#!/usr/bin/env python3
"""Install the locked GitHub runner and RunsOn bootstrap on plain Ubuntu."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit


def download_verified(spec, output):
    archive = output / Path(urlsplit(spec["url"]).path).name
    subprocess.run([
        "curl", "--fail", "--location", "--retry", "3", "--proto", "=https",
        "--tlsv1.2", "--output", str(archive), spec["url"],
    ], check=True)
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != spec["sha256"]:
        raise SystemExit(f"runner input checksum mismatch: {archive.name}")
    return archive


def main():
    if os.geteuid() != 0:
        raise SystemExit("runner installation requires root")
    lock = json.loads(Path(sys.argv[1]).read_text())["runner"]
    output = Path(sys.argv[2]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    home = Path("/home/runner")
    try:
        account = pwd.getpwnam("runner")
    except KeyError:
        subprocess.run([
            "useradd", "--uid", "1001", "--user-group", "--create-home",
            "--home-dir", str(home), "--shell", "/bin/bash", "runner",
        ], check=True)
        account = pwd.getpwnam("runner")
    if account.pw_uid != 1001 or account.pw_dir != str(home):
        raise SystemExit("runner must have UID 1001 and home /home/runner")
    if any((home / name).exists() for name in (".runner", ".credentials", ".credentials_rsaparams")):
        raise SystemExit("refusing to overwrite a registered runner")
    sudoers = Path("/etc/sudoers.d/90-ami-example-runner")
    sudoers.write_text("runner ALL=(ALL) NOPASSWD: ALL\n")
    sudoers.chmod(0o440)
    subprocess.run(["visudo", "--check", "--file", str(sudoers)], check=True)

    github_archive = download_verified(lock["github"], output)
    with tarfile.open(github_archive) as archive:
        archive.extractall(home, filter="data")
    subprocess.run([
        "chown", "--recursive", f"{account.pw_uid}:{account.pw_gid}", str(home),
    ], check=True)
    bootstrap_download = download_verified(lock["bootstrap"], output)
    bootstrap = Path(f"/usr/local/bin/runs-on-bootstrap-v{lock['bootstrap']['version']}")
    shutil.copyfile(bootstrap_download, bootstrap)
    bootstrap.chmod(0o755)

    # Exercise the installed runtime and entrypoint without registering the image.
    run_as_runner = ["runuser", "--user", "runner", "--"]
    expected = lock["github"]["version"]
    version = subprocess.check_output(
        [*run_as_runner, str(home / "bin/Runner.Listener"), "--version"], cwd=home, text=True,
    ).strip()
    if version != expected:
        raise SystemExit(f"installed runner version differs: {version}")
    startup = subprocess.check_output(
        [*run_as_runner, str(home / "run.sh"), "--version"], cwd=home, text=True,
    )
    if expected not in startup.splitlines():
        raise SystemExit("runner entrypoint did not report its installed version")
    subprocess.run([*run_as_runner, "sudo", "--non-interactive", "true"], check=True)
    (output / "runner-downloads.json").write_text(json.dumps({
        "github": {**lock["github"], "file": github_archive.name},
        "bootstrap": {**lock["bootstrap"], "file": bootstrap_download.name},
    }, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
