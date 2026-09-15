#!/usr/bin/env python3
"""Bake only recipe-stable identity and installed content into the image."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from inventory import inventory, packages, sha

recipe = Path("/opt/ami-example-recipe")
identity = json.loads((recipe / "recipe.json").read_text())
locked_xenomai = json.loads((recipe / "images/xenomai-cobalt/inputs.lock.json").read_text())["xenomai"]
xenomai = {key: locked_xenomai[key] for key in ("version", "core", "prefix")}
if xenomai["core"] != "cobalt" or xenomai["prefix"] != "/usr/xenomai":
    raise ValueError("image recipe must install Xenomai Cobalt at /usr/xenomai")
for option, value in (("--core", xenomai["core"]), ("--version", xenomai["version"])):
    actual = subprocess.check_output([xenomai["prefix"] + "/bin/xeno-config", option], text=True).strip()
    if actual != value:
        raise ValueError(f"installed Xenomai {option} differs from locked input: {actual}")
xenomai_files = {}
for path in sorted(Path(xenomai["prefix"]).rglob("*")):
    name = str(path.relative_to(xenomai["prefix"]))
    if path.is_symlink():
        xenomai_files[name] = {"symlink": str(path.readlink())}
    elif path.is_file():
        xenomai_files[name] = {"sha256": sha(path), "mode": oct(path.stat().st_mode & 0o7777)}
if not xenomai_files:
    raise ValueError("installed Xenomai tree is empty")
release = Path("/var/lib/ami-example/kernel-release").read_text().strip()
package_text = packages()
payload = {str(path): sha(path) for path in sorted(Path(f"/lib/modules/{release}").rglob("*"))
           if path.is_file() and not path.is_symlink() and ".ko" in path.name}
payload[f"/boot/vmlinuz-{release}"] = sha(f"/boot/vmlinuz-{release}")
payload[f"/boot/System.map-{release}"] = sha(f"/boot/System.map-{release}")
normalized_paths = ["/etc/default/grub.d/99-ami-example.cfg", "/etc/apt/sources.list.d/ami-example.sources",
                    "/usr/local/bin/runner-image-env", "/usr/local/bin/ami-example-smoke",
                    "/usr/local/bin/ami-example-guest-report", "/etc/fstab", "/boot/grub/grub.cfg",
                    "/etc/security/limits.d/99-xenomai.conf", "/etc/systemd/system.conf.d/99-xenomai.conf",
                    "/etc/ld.so.conf.d/xenomai.conf", "/etc/udev/rules.d/99-xenomai.rules"]
with tempfile.TemporaryDirectory(prefix="ami-example-initramfs-") as temporary:
    subprocess.run(["unmkinitramfs", f"/boot/initrd.img-{release}", temporary], check=True)
    initramfs_content = {}
    for path in sorted(Path(temporary).rglob("*")):
        name = str(path.relative_to(temporary))
        if path.is_symlink():
            initramfs_content[name] = {"symlink": str(path.readlink())}
        elif path.is_file():
            initramfs_content[name] = {"sha256": sha(path), "mode": oct(path.stat().st_mode & 0o7777)}
image = {"schema_version": 1, "recipe_id": identity["recipe_id"], "kernel_release": release,
         "config_sha256": sha(f"/boot/config-{release}"), "payload_hashes": payload,
         "packages_sha256": hashlib.sha256(package_text.encode()).hexdigest(),
         "package_inventory": package_text,
         "snap_hashes": {p.name: sha(p) for p in sorted(Path("/var/lib/snapd/snaps").glob("*.snap"))},
         "normalized_configuration": {path: sha(path) for path in normalized_paths},
         "initramfs_sha256": sha(f"/boot/initrd.img-{release}"),
         "initramfs_content": initramfs_content,
         "xenomai": xenomai, "xenomai_files": xenomai_files,
         "toolchain": {"gcc": subprocess.check_output(["/usr/bin/gcc-13", "--version"], text=True).splitlines()[0],
                       "ld": subprocess.check_output(["/usr/bin/ld.bfd", "--version"], text=True).splitlines()[0]},
         "parent_inventory": json.loads(Path("/var/lib/ami-example/parent-inventory.json").read_text()),
         "runner_inventory": {key: value for key, value in inventory().items()
                              if key in ("runner_version", "runner_listener_sha256", "bootstrap_files")}}
Path("/var/lib/ami-example/packages.tsv").write_text(package_text)
Path("/etc/ami-example.json").write_text(json.dumps(image, sort_keys=True, indent=2) + "\n")
Path("/etc/ami-example.json").chmod(0o644)
