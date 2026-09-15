#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
recipe=/opt/ami-example-recipe
[[ "$EUID" -eq 0 && -f "$recipe/recipe.json" && -f "$recipe/source-inventory.json" ]]
mkdir -p /var/lib/ami-example
python3 "$recipe/images/common/inventory.py" --expect "$recipe/source-inventory.json" > /var/lib/ami-example/parent-inventory.json
# The parent can retain its original partition size on a larger Packer root disk.
[[ "$(findmnt --noheadings --output FSTYPE --target /)" == ext4 ]]
root_device=$(readlink -f "$(findmnt --noheadings --output SOURCE --target /)")
[[ -b "$root_device" && "$(lsblk --nodeps --noheadings --output TYPE "$root_device")" == part ]]
root_partition=$(cat "/sys/class/block/${root_device##*/}/partition")
root_disk_name=$(lsblk --nodeps --noheadings --output PKNAME "$root_device")
[[ "$root_partition" =~ ^[1-9][0-9]*$ && "$root_disk_name" =~ ^[A-Za-z0-9._-]+$ ]]
root_disk="/dev/$root_disk_name"
[[ "$(lsblk --nodeps --noheadings --output TYPE "$root_disk")" == disk ]]
if growth_output=$(growpart "$root_disk" "$root_partition" 2>&1); then
  printf '%s\n' "$growth_output"
else
  growth_status=$?
  printf '%s\n' "$growth_output" >&2
  [[ "$growth_status" -eq 1 && "$growth_output" == NOCHANGE:* ]] || exit "$growth_status"
fi
# Also complete an earlier partition-only resize; resize2fs is safe to repeat.
resize2fs "$root_device"
df --block-size=1 --output=source,fstype,size,avail /
if [[ "${STOCK_ONLY:-false}" == true ]]; then
  systemctl is-active ssh
  curl --fail --head --proto '=https' --tlsv1.2 https://snapshot.ubuntu.com/
  exit 0
fi
# Only the plain Ubuntu source is used for the small image.
python3 - <<'PY'
import json
from pathlib import Path
parent = json.loads(Path('/var/lib/ami-example/parent-inventory.json').read_text())
if parent['runner_version'] is not None or parent['bootstrap_files']:
    raise SystemExit('select a plain Ubuntu 24.04 source AMI, without an inherited runner tool bundle')
PY
# Nitro exposes the second EBS disk as NVMe. Require exactly one empty disk
# besides the root, including no partition table, before making a filesystem.
build_device=$(python3 - "$root_disk" <<'PY'
import json, subprocess, sys
disks = json.loads(subprocess.check_output(
    ['lsblk', '--json', '--paths', '--output', 'NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS', '--bytes'], text=True))['blockdevices']
extra = [d for d in disks if d['type'] == 'disk' and d['name'] != sys.argv[1]]
if len(extra) != 1:
    raise SystemExit('expected one disposable build disk besides root')
disk = extra[0]
if disk.get('children') or disk['fstype'] or any(disk['mountpoints']) or int(disk['size']) != 16 * 1024**3:
    raise SystemExit('build disk must be an empty 16 GiB device')
signatures = json.loads(subprocess.check_output(['wipefs', '--json', disk['name']], text=True))
if signatures['signatures']:
    raise SystemExit('build disk already has a filesystem or partition table')
print(disk['name'])
PY
)
mkfs.ext4 -m 0 "$build_device"
mkdir -p /mnt/ami-example-build
mount -o noatime "$build_device" /mnt/ami-example-build
mkdir -p /mnt/ami-example-build/inputs /mnt/ami-example-build/tmp
export TMPDIR=/mnt/ami-example-build/tmp
# Stop timers before touching apt; never upgrade the whole parent distribution.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer || true
systemctl stop apt-daily.service apt-daily-upgrade.service
systemctl mask apt-daily.service apt-daily-upgrade.service unattended-upgrades.service
python3 "$recipe/images/common/install-packages.py" "$recipe/images/xenomai-cobalt/inputs.lock.json" /mnt/ami-example-build/inputs
python3 "$recipe/images/common/install-runner.py" "$recipe/images/xenomai-cobalt/inputs.lock.json" /mnt/ami-example-build/inputs
# Cobalt grants non-root kernel access to this group through the boot command line.
python3 - "$recipe/images/xenomai-cobalt/inputs.lock.json" <<'PY'
import grp
import json
import subprocess
import sys

gid = json.load(open(sys.argv[1]))["xenomai"]["allowed_group_gid"]
try:
    group = grp.getgrnam("xenomai")
except KeyError:
    subprocess.run(["groupadd", "--gid", str(gid), "xenomai"], check=True)
else:
    if group.gr_gid != gid:
        raise ValueError("existing xenomai group has a different GID")
subprocess.run(["usermod", "--append", "--groups", "xenomai", "runner"], check=True)
PY
mkdir -p /etc/security/limits.d /etc/systemd/system.conf.d
cat > /etc/security/limits.d/99-xenomai.conf <<'EOF'
@xenomai - memlock unlimited
@xenomai - rtprio 99
EOF
# RunsOn starts its runner from a system service, which need not open a PAM session.
# These defaults take effect on the new AMI's first boot and cover that ancestry.
cat > /etc/systemd/system.conf.d/99-xenomai.conf <<'EOF'
[Manager]
DefaultLimitMEMLOCK=infinity
DefaultLimitRTPRIO=99
EOF
install -m 0755 "$recipe/images/common/runner-image-env" /usr/local/bin/runner-image-env
install -m 0755 "$recipe/images/xenomai-cobalt/smoke.sh" /usr/local/bin/ami-example-smoke
install -m 0755 "$recipe/images/common/guest-report.py" /usr/local/bin/ami-example-guest-report
bash "$recipe/images/xenomai-cobalt/build-kernel.sh"
# Keep the application toolchain; remove tools used only to build the image and
# the distribution kernel, whose replacement is installed directly in /boot.
python3 - "$recipe/images/xenomai-cobalt/inputs.lock.json" <<'PY'
import json, subprocess, sys
lock = json.load(open(sys.argv[1]))['os']
installed = subprocess.check_output(['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Status}\n'], text=True)
kernels = [line.split('\t')[0] for line in installed.splitlines()
           if line.endswith('\tinstalled') and line.startswith(('linux-aws', 'linux-image-', 'linux-modules-', 'linux-headers-'))]
subprocess.run(['apt-get', '-y', 'purge', '--auto-remove', *lock['build_only_packages'], *kernels], check=True)
PY
update-grub
python3 "$recipe/images/common/write-image-manifest.py"
# Transfer immutable inputs back over Packer's existing SSM/SSH channel.
tar -C /mnt/ami-example-build/inputs -cf /mnt/ami-example-build/inputs.tar .
chmod 0644 /mnt/ami-example-build/inputs.tar
