#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
recipe=/opt/ami-example-recipe
# The parent can retain its original partition size on a larger Packer root disk.
# Packer attaches the pinned Ubuntu root first, then the disposable build disk.
growpart /dev/vda 1 || test "$?" -eq 1
resize2fs /dev/vda1
mkfs.ext4 -m 0 /dev/vdb
mkdir -p /mnt/ami-example-build
mount -o noatime /dev/vdb /mnt/ami-example-build
mkdir -p /mnt/ami-example-build/inputs /mnt/ami-example-build/tmp
export TMPDIR=/mnt/ami-example-build/tmp
python3 "$recipe/common/install-packages.py" "$recipe/xenomai-cobalt/inputs.lock.json"
python3 "$recipe/common/install-runner.py" "$recipe/xenomai-cobalt/inputs.lock.json" /mnt/ami-example-build/inputs
# Cobalt grants non-root kernel access to this group through the boot command line.
python3 - "$recipe/xenomai-cobalt/inputs.lock.json" <<'PY'
import json
import subprocess
import sys

gid = json.load(open(sys.argv[1]))["xenomai"]["allowed_group_gid"]
subprocess.run(["groupadd", "--gid", str(gid), "xenomai"], check=True)
subprocess.run(["usermod", "--append", "--groups", "xenomai", "runner"], check=True)
PY
mkdir -p /etc/security/limits.d /etc/systemd/system.conf.d
cat > /etc/security/limits.d/99-xenomai.conf <<'EOF'
@xenomai - memlock unlimited
@xenomai - rtprio 99
EOF
mkdir -p /etc/udev/rules.d
cat > /etc/udev/rules.d/99-xenomai.rules <<'EOF'
KERNEL=="memdev-private", GROUP="xenomai", MODE="0660"
KERNEL=="memdev-shared", GROUP="xenomai", MODE="0660"
EOF
# RunsOn starts its runner from a system service, which need not open a PAM session.
# These defaults take effect on the new AMI's first boot and cover that ancestry.
cat > /etc/systemd/system.conf.d/99-xenomai.conf <<'EOF'
[Manager]
DefaultLimitMEMLOCK=infinity
DefaultLimitRTPRIO=99
EOF
bash "$recipe/xenomai-cobalt/build-kernel.sh"
# Keep the application toolchain; remove tools used only to build the image and
# the distribution kernel, whose replacement is installed directly in /boot.
python3 - "$recipe/xenomai-cobalt/inputs.lock.json" <<'PY'
import json, subprocess, sys
lock = json.load(open(sys.argv[1]))['os']
installed = subprocess.check_output(['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Status}\n'], text=True)
kernels = [line.split('\t')[0] for line in installed.splitlines()
           if line.endswith('\tinstalled') and line.startswith(('linux-aws', 'linux-virtual', 'linux-image-', 'linux-modules-', 'linux-headers-'))]
subprocess.run(['apt-get', '-y', 'purge', '--auto-remove', *lock['build_only_packages'], *kernels], check=True)
PY
update-grub
