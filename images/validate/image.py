#!/usr/bin/env python3
"""Boot a finalized image locally and run its Cobalt application as runner."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from contracts import BuiltImage
from .vm import create_seed, firmware_paths


ROOT = Path(__file__).resolve().parents[2]
GUEST_DIRECTORY = "/tmp/ami-example-validation"


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("VM validation deadline exceeded")
    return seconds


@contextmanager
def running_vm(command: list[str], log: Path):
    """Own the process until it has stopped, including on interruption."""
    with log.open("wb") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def qemu_command(overlay: Path, seed: Path, variables: Path, code: Path,
                 serial_log: Path, port: int, accelerator: str, cpus: int, memory_mib: int) -> list[str]:
    return [
        "qemu-system-x86_64", "-machine", "q35", "-accel", accelerator,
        "-cpu", "host" if accelerator == "kvm" else "max", "-smp", str(cpus), "-m", str(memory_mib),
        "-display", "none", "-monitor", "none", "-serial", f"file:{serial_log}", "-no-reboot",
        "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,file={variables}",
        "-blockdev", json.dumps({"driver": "qcow2", "node-name": "image", "file": {
            "driver": "file", "filename": str(overlay)}}),
        "-device", "virtio-blk-pci,drive=image,bootindex=1",
        "-blockdev", json.dumps({"driver": "raw", "node-name": "seed", "read-only": True,
                                  "file": {"driver": "file", "filename": str(seed)}}),
        "-device", "virtio-blk-pci,drive=seed",
        "-netdev", f"user,id=network,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-device", "virtio-net-pci,netdev=network",
    ]


def ssh_command(key: Path, known_hosts: Path, port: int) -> list[str]:
    return ["ssh", "-T", "-i", str(key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2", "runner@127.0.0.1"]


def wait_for_ssh(ssh: list[str], process, deadline: float, log):
    while True:
        if process.poll() is not None:
            raise RuntimeError("QEMU exited before SSH became available; see qemu.log and serial.log")
        result = subprocess.run([*ssh, "true"], stdout=log, stderr=log,
                                timeout=min(10, remaining(deadline)))
        if result.returncode == 0:
            return
        time.sleep(min(1, remaining(deadline)))


def run_guest(image: BuiltImage, ssh: list[str], temporary: Path, deadline: float, log):
    archive = temporary / "test.tar"
    with tarfile.open(archive, "w") as stream:
        for name in ("CMakeLists.txt", "main.c"):
            stream.add(ROOT / "execution/cobalt" / name, arcname=f"cobalt/{name}")
        stream.add(Path(__file__).resolve().parent / "validate-guest.sh", arcname="validate-guest.sh")
    with archive.open("rb") as stream:
        subprocess.run([*ssh, f"mkdir -m 0700 {GUEST_DIRECTORY} && tar -xf - -C {GUEST_DIRECTORY}"],
                       stdin=stream, stdout=log, stderr=log, check=True, timeout=remaining(deadline))
    command = shlex.join(["bash", f"{GUEST_DIRECTORY}/validate-guest.sh",
                          image.kernel_release, image.xenomai_version, GUEST_DIRECTORY])
    subprocess.run([*ssh, command], stdout=log, stderr=log, check=True, timeout=remaining(deadline))


def validate(image: BuiltImage, output: Path, *, accelerator: str, cpus: int,
             memory_mib: int, timeout_seconds: int):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("validation output directory must be empty")
    for tool in ("qemu-system-x86_64", "qemu-img", "cloud-localds", "ssh", "ssh-keygen"):
        if shutil.which(tool) is None:
            raise ValueError(f"required local tool is missing: {tool}")
    if accelerator == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise ValueError("KVM is unavailable; enable /dev/kvm access or explicitly choose --accelerator tcg")
    code, original_variables = firmware_paths()
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="cobalt-validation-") as directory:
        temporary = Path(directory)
        key = temporary / "ssh-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        seed = create_seed(temporary, "runner", key.with_suffix(".pub").read_text(),
                           "cobalt-validation-" + uuid.uuid4().hex)
        variables = temporary / "OVMF_VARS.fd"
        shutil.copyfile(original_variables, variables)
        overlay = temporary / "disk.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                        "-b", str(image.disk_path), str(overlay)], check=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        command = qemu_command(overlay, seed, variables, code, output / "serial.log", port,
                               accelerator, cpus, memory_mib)
        ssh = ssh_command(key, temporary / "known_hosts", port)
        with running_vm(command, output / "qemu.log") as process, (output / "ssh.log").open("wb") as log:
            wait_for_ssh(ssh, process, deadline, log)
            run_guest(image, ssh, temporary, deadline, log)


def positive_integer(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--accelerator", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--cpus", type=positive_integer, default=2)
    parser.add_argument("--memory-mib", type=positive_integer, default=2048)
    parser.add_argument("--timeout-seconds", type=positive_integer, default=300)
    args = parser.parse_args()
    image = BuiltImage.load(args.build)
    validate(image, args.output, accelerator=args.accelerator, cpus=args.cpus,
             memory_mib=args.memory_mib, timeout_seconds=args.timeout_seconds)
    print(f"Cobalt VM validation passed; logs: {args.output}")


def cli():
    def interrupt(signum, frame):
        raise KeyboardInterrupt("VM validation interrupted")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print("VM validation interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
