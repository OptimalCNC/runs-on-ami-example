#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# Customize the project's development tools here. RunsOn is installed separately.
# The Cobalt recipe uses the compiler, kernel, and boot tools in this list.
apt-get update
apt-get -y --no-install-recommends install \
  binutils \
  build-essential \
  cmake \
  cpio \
  g++-13 \
  gcc-13 \
  grub-common \
  grub2-common \
  initramfs-tools \
  make \
  ninja-build \
  xz-utils
