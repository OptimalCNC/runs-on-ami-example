#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
recipe=/opt/ami-example-recipe
[[ "$EUID" -eq 0 && -f "$recipe/recipe.json" ]]
source_dir=/mnt/ami-example-build/linux
xenomai_dir=/mnt/ami-example-build/xenomai
userspace_build=/mnt/ami-example-build/xenomai-build
output=/var/lib/ami-example
lock="$recipe/images/xenomai-cobalt/inputs.lock.json"
mkdir -p "$source_dir" "$xenomai_dir" "$userspace_build" "$output"
eval "$(python3 - "$lock" <<'PY'
import json, shlex, sys
lock = json.load(open(sys.argv[1]))
k, x = lock['kernel'], lock['xenomai']
for name, value in {'SOURCE_URL': k['url'], 'SOURCE_SHA256': k['sha256'],
                    'SOURCE_DATE_EPOCH': k['source_date_epoch'], 'KERNEL_RELEASE': k['release'],
                    'XENOMAI_URL': x['url'], 'XENOMAI_SHA256': x['sha256'],
                    'XENOMAI_VERSION': x['version'], 'XENOMAI_PREFIX': x['prefix'],
                    'XENOMAI_GID': x['allowed_group_gid']}.items():
 print(f'{name}={shlex.quote(str(value))}')
PY
)"
archive=/mnt/ami-example-build/inputs/linux-dovetail.tar.gz
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 "$SOURCE_URL" -o "$archive"
printf '%s  %s\n' "$SOURCE_SHA256" "$archive" | sha256sum --check --strict
tar -xzf "$archive" --strip-components=1 -C "$source_dir"
archive=/mnt/ami-example-build/inputs/xenomai.tar.gz
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 "$XENOMAI_URL" -o "$archive"
printf '%s  %s\n' "$XENOMAI_SHA256" "$archive" | sha256sum --check --strict
tar -xzf "$archive" --strip-components=1 -C "$xenomai_dir"
# The pinned Linux tree already contains Dovetail; integrate this exact Cobalt source.
"$xenomai_dir/scripts/prepare-kernel.sh" --linux="$source_dir" --arch=x86_64
cd "$source_dir"
export SOURCE_DATE_EPOCH
export KBUILD_BUILD_TIMESTAMP KBUILD_BUILD_USER=ami-example KBUILD_BUILD_HOST=builder KBUILD_BUILD_VERSION=1
KBUILD_BUILD_TIMESTAMP=$(date -u -d "@$SOURCE_DATE_EPOCH" '+%a %b %d %T UTC %Y')
export CCACHE_DISABLE=1
export LD=ld.bfd HOSTLD=ld.bfd
# Optional host tools must not change the resolved configuration of this C-only kernel.
kernel_make=(make CC=gcc-13 HOSTCC=gcc-13 LD=ld.bfd HOSTLD=ld.bfd RUSTC=/bin/false BINDGEN=/bin/false PAHOLE=/dev/null)
unset CCACHE_DIR KBUILD_OUTPUT LOCALVERSION
export KCFLAGS="-ffile-prefix-map=$source_dir=/usr/src/linux -fdebug-prefix-map=$source_dir=/usr/src/linux -ffile-prefix-map=$xenomai_dir=/usr/src/xenomai -fdebug-prefix-map=$xenomai_dir=/usr/src/xenomai"
export KCPPFLAGS="$KCFLAGS"
cp "$recipe/images/xenomai-cobalt/kernel.config" .config
"${kernel_make[@]}" olddefconfig
"${kernel_make[@]}" syncconfig
python3 "$recipe/images/common/check-kernel-config.py" "$recipe/images/xenomai-cobalt/kernel.config" .config
release=$("${kernel_make[@]}" -s kernelrelease)
[[ "$release" == "$KERNEL_RELEASE" ]]
printf '%s\n' "$release" > "$output/kernel-release"
"${kernel_make[@]}" -j"$(nproc)" bzImage modules
"${kernel_make[@]}" INSTALL_MOD_STRIP=1 modules_install
rm -f "/lib/modules/$release/build" "/lib/modules/$release/source"
install -m 0644 arch/x86/boot/bzImage "/boot/vmlinuz-$release"
install -m 0644 System.map "/boot/System.map-$release"
install -m 0644 .config "/boot/config-$release"
depmod "$release"
update-initramfs -c -k "$release"
# Build the matching Cobalt SDK, retaining its application headers and libraries.
cd "$xenomai_dir"
./scripts/bootstrap
cd "$userspace_build"
export CFLAGS="-O2 -g -ffile-prefix-map=$xenomai_dir=/usr/src/xenomai -fdebug-prefix-map=$xenomai_dir=/usr/src/xenomai -ffile-prefix-map=$userspace_build=/usr/src/xenomai-build -fdebug-prefix-map=$userspace_build=/usr/src/xenomai-build"
export CXXFLAGS="$CFLAGS"
"$xenomai_dir/configure" --prefix="$XENOMAI_PREFIX" --with-core=cobalt \
  --enable-smp --disable-registry --disable-doc-build CC=gcc-13 CXX=g++-13
make -j"$(nproc)"
make install-strip
[[ "$("$XENOMAI_PREFIX/bin/xeno-config" --core)" == cobalt ]]
[[ "$("$XENOMAI_PREFIX/bin/xeno-config" --version)" == "$XENOMAI_VERSION" ]]
printf '%s/lib\n' "$XENOMAI_PREFIX" > /etc/ld.so.conf.d/xenomai.conf
ldconfig
# Select the exact menu entry even when a newer stock kernel remains installed.
cat > /etc/default/grub.d/99-ami-example.cfg <<EOF
GRUB_DEFAULT="Advanced options for Ubuntu>Ubuntu, with Linux $release"
GRUB_SAVEDEFAULT=false
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=1
GRUB_RECORDFAIL_TIMEOUT=1
GRUB_CMDLINE_LINUX="\${GRUB_CMDLINE_LINUX:-} xenomai.allowed_group=$XENOMAI_GID"
EOF
update-grub
grep -F "Ubuntu, with Linux $release" /boot/grub/grub.cfg
