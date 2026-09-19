#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
workspace=${1:?workspace required}
ssh_username=${2:?SSH user required}
# The parent can retain its original partition size on a larger root disk.
# Attach the Ubuntu root first, then the blank disposable build disk.
growpart /dev/vda 1 || test "$?" -eq 1
resize2fs /dev/vda1
mkfs.ext4 -m 0 /dev/vdb
mkdir -p "$workspace"
mount -o noatime /dev/vdb "$workspace"
chown "$ssh_username:" "$workspace"
