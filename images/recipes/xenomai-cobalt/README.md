# Xenomai Cobalt

This recipe integrates Cobalt into the pinned Dovetail Linux sources, installs
the kernel and matching SDK, and configures Cobalt access. Source
pins and installation settings live in `inputs.sh`; `kernel.config` defines the
kernel configuration.

## Installation

From the recipe root, run:

```bash
sudo bash ./xenomai-cobalt/provision.sh
```

`provision.sh` runs the complete Cobalt installation in this order:

1. Installs the kernel and SDK build dependencies and configures Cobalt access
   permissions.
2. Calls `build-kernel.sh`, which downloads, verifies, and extracts both source
   archives, integrates Cobalt into Dovetail, builds and installs the kernel and
   modules, generates the initramfs, and builds and installs the matching SDK.
3. Removes the build-only dependencies and stock kernels and updates GRUB.

## Resulting environment

The next boot uses the Cobalt kernel. The SDK's headers, libraries, and tools are
installed under `/usr/xenomai`. The `xenomai` group has GID 4242 and receives
kernel access through `xenomai.allowed_group` and the Cobalt device
rules. PAM and systemd limits allow locked memory and real-time priority 99.

Downloads, source trees, and compilation outputs stay in this directory's
`.build/` folder and can be removed after installation.
