#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
recipe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export TMPDIR="$recipe_dir/.build/tmp"
mkdir -p "$TMPDIR"
source "$recipe_dir/inputs.sh"
apt-get -y --no-install-recommends install "${BUILD_ONLY_PACKAGES[@]}"
# Cobalt grants non-root kernel access to this group through the boot command line.
groupadd --gid "$XENOMAI_GID" xenomai
usermod --append --groups xenomai runner
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
bash "$recipe_dir/build-kernel.sh"
# Keep the application toolchain; remove tools used only to build the image and
# the distribution kernel, whose replacement is installed directly in /boot.
installed=$(dpkg-query -W '-f=${binary:Package}\t${db:Status-Status}\n')
kernels=()
while IFS=$'\t' read -r package status; do
  if [[ $status == installed ]]; then
    case "$package" in
      linux-aws*|linux-virtual*|linux-image-*|linux-modules-*|linux-headers-*)
        kernels+=("$package")
        ;;
    esac
  fi
done <<< "$installed"
apt-get -y purge --auto-remove "${BUILD_ONLY_PACKAGES[@]}" "${kernels[@]}"
update-grub
