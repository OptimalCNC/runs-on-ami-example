#!/bin/bash
set -euo pipefail
export LC_ALL=C TZ=UTC DEBIAN_FRONTEND=noninteractive
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
recipe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_dir="$recipe_dir/.build"
export TMPDIR="$build_dir/tmp"
mkdir -p "$TMPDIR"

# RunsOn's minimal-image tooling and the GitHub Actions runner's Ubuntu runtime.
# https://github.com/runs-on/runner-images-for-aws/blob/main/patches/ubuntu/files/minimal-install-target-tooling.sh
# https://github.com/actions/runner/blob/main/src/Misc/layoutbin/installdependencies.sh
apt-get update
apt-get -y --no-install-recommends install \
  ca-certificates \
  curl \
  git \
  jq \
  libicu74 \
  libkrb5-3 \
  liblttng-ust1t64 \
  libssl3t64 \
  netcat-openbsd \
  openssh-server \
  python3 \
  sudo \
  unzip \
  zip \
  zlib1g \
  zstd

# Resolve each stable release once so the asset and checksum belong together.
download_latest_release() {
  local repository=$1 pattern=$2 destination=$3
  local asset name url sha256
  asset=$(curl --fail --silent --show-error --location --retry 3 --proto '=https' --tlsv1.2 \
    "https://api.github.com/repos/$repository/releases/latest" |
    jq -er --arg pattern "$pattern" '
      [.assets[] | select(.name | test($pattern))]
      | if length == 1 then .[0] else error("expected one matching release asset") end
      | (.digest | capture("^sha256:(?<hash>[0-9a-f]{64})$").hash) as $sha256
      | [.name, .browser_download_url, $sha256] | @tsv
    ') || return
  IFS=$'\t' read -r name url sha256 <<< "$asset"
  curl --fail --location --retry 3 --proto '=https' --tlsv1.2 "$url" -o "$destination" || return
  printf '%s  %s\n' "$sha256" "$destination" | sha256sum --check --strict >&2 || return
  printf '%s\n' "$name"
}

runner_home=/home/runner
useradd --uid 1001 --user-group --create-home --home-dir "$runner_home" --shell /bin/bash runner
printf '%s\n' 'runner ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-ami-example-runner
chmod 0440 /etc/sudoers.d/90-ami-example-runner
archive="$build_dir/actions-runner.tar.gz"
download_latest_release actions/runner '^actions-runner-linux-x64-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$' "$archive"
tar -xzf "$archive" --no-same-owner -C "$runner_home"
chown --recursive runner:runner "$runner_home"
bootstrap="$build_dir/runs-on-bootstrap"
bootstrap_name=$(download_latest_release runs-on/bootstrap '^bootstrap-v[0-9]+\.[0-9]+\.[0-9]+-linux-x86_64$' "$bootstrap")
install -m 0755 "$bootstrap" "/usr/local/bin/runs-on-${bootstrap_name%-linux-x86_64}"
