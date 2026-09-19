#!/usr/bin/env python3
"""Boot a finalized image locally and run its Cobalt application as runner."""
import argparse
from contextlib import contextmanager
import json
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


ROOT = Path(__file__).resolve().parents[2]
GUEST_DIRECTORY = "/tmp/ami-example-validation"


def create_seed(destination: Path, public_key: str) -> Path:
    """Authorize temporary SSH access without changing image groups or limits."""
    user_data = destination / "user-data"
    user_data.write_text("#cloud-config\n" + json.dumps({
        "users": [{"name": "runner", "ssh_authorized_keys": [public_key.strip()]}],
    }) + "\n")
    metadata = destination / "meta-data"
    metadata.write_text(json.dumps({"instance-id": "cobalt-validation"}) + "\n")
    seed = destination / "seed.img"
    subprocess.run(["cloud-localds", str(seed), str(user_data), str(metadata)], check=True)
    return seed


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


def ssh_command(key: Path, port: int) -> list[str]:
    return ["ssh", "-T", "-i", str(key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "runner@127.0.0.1"]


def wait_for_ssh(ssh: list[str], process, deadline: float, log):
    while True:
        if process.poll() is not None:
            raise RuntimeError("QEMU exited before SSH became available; see qemu.log and serial.log")
        result = subprocess.run([*ssh, "true"], stdout=log, stderr=log,
                                timeout=min(10, remaining(deadline)))
        if result.returncode == 0:
            return
        time.sleep(min(1, remaining(deadline)))


def run_guest(ssh: list[str], temporary: Path, deadline: float, log):
    archive = temporary / "test.tar"
    with tarfile.open(archive, "w") as stream:
        for name in ("CMakeLists.txt", "main.c"):
            stream.add(ROOT / "execution/cobalt" / name, arcname=f"cobalt/{name}")
        stream.add(Path(__file__).resolve().parent / "validate-guest.sh", arcname="validate-guest.sh")
    with archive.open("rb") as stream:
        subprocess.run([*ssh, f"mkdir -m 0700 {GUEST_DIRECTORY} && tar -xf - -C {GUEST_DIRECTORY}"],
                       stdin=stream, stdout=log, stderr=log, check=True, timeout=remaining(deadline))
    command = shlex.join(["bash", f"{GUEST_DIRECTORY}/validate-guest.sh",
                          GUEST_DIRECTORY])
    subprocess.run([*ssh, command], stdout=log, stderr=log, check=True, timeout=remaining(deadline))


def validate(image: Path, output: Path, *, accelerator: str, cpus: int,
             memory_mib: int, timeout_seconds: int):
    image = image.resolve(strict=True)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="cobalt-validation-") as directory:
        temporary = Path(directory)
        key = temporary / "ssh-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        seed = create_seed(temporary, key.with_suffix(".pub").read_text())
        variables = temporary / "OVMF_VARS.fd"
        shutil.copyfile("/usr/share/OVMF/OVMF_VARS_4M.fd", variables)
        overlay = temporary / "disk.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
                        "-b", str(image), str(overlay)], check=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        command = qemu_command(overlay, seed, variables, Path("/usr/share/OVMF/OVMF_CODE_4M.fd"), output / "serial.log", port,
                               accelerator, cpus, memory_mib)
        ssh = ssh_command(key, port)
        with running_vm(command, output / "qemu.log") as process, (output / "ssh.log").open("wb") as log:
            wait_for_ssh(ssh, process, deadline, log)
            run_guest(ssh, temporary, deadline, log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True, help="Raw disk to boot and validate")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--accelerator", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    validate(args.image, args.output, accelerator=args.accelerator, cpus=args.cpus,
             memory_mib=args.memory_mib, timeout_seconds=args.timeout_seconds)
    print(f"Cobalt VM validation passed; logs: {args.output}")


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt("VM validation interrupted")

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print("VM validation interrupted.", file=sys.stderr)
        raise SystemExit(130)
