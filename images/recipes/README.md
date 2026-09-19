# Ubuntu Cobalt installation recipe

These scripts install development tools, a GitHub Actions runner, RunsOn
bootstrap, and a Xenomai Cobalt kernel and SDK on Ubuntu.

## Requirements

Download the source image specified in [recipe.json](recipe.json) and verify it
against the SHA-256 checksum file at `source_image.sha256sums_url`. Use it to
prepare a fresh Ubuntu 24.04 x86-64 system with GRUB, UEFI boot, and Secure Boot disabled.

Copy this entire directory to a writable
filesystem that permits execution, with enough storage and memory to compile the
kernel. Its path must contain no whitespace or colons, as required by the kernel
build system. Scripts locate their files relative to themselves and keep temporary
files in local `.build/` directories.

## Installation

From this directory, run the scripts in the order listed in `recipe.json`:

```sh
sudo bash ./install-runs-on.sh &&
sudo bash ./install-tools.sh &&
sudo bash ./xenomai-cobalt/provision.sh
```

- `install-runs-on.sh` owns the runner account, runtime dependencies, and common utilities from [RunsOn's minimal image](https://github.com/runs-on/runner-images-for-aws/blob/main/patches/ubuntu/files/minimal-install-target-tooling.sh).
  It installs the latest stable GitHub Actions runner and RunsOn bootstrap, verifying downloads against their published SHA-256 digests.
- Customize project packages in `install-tools.sh`
- `./xenomai-cobalt/provision.sh` runs the complete Cobalt installation, see [xenomai-cobalt/README.md](xenomai-cobalt/README.md).
  Cobalt replaces the distribution kernel and installs its SDK under `/usr/xenomai`.
  Reboot will use the new kernel.

After installation finishes, the recipe directory, including its temporary build files, can be removed.
