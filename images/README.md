# Build, validate, and publish a Cobalt image

This module builds a bootable Ubuntu 24.04 disk containing the pinned Linux,
Dovetail, and Xenomai 3 Cobalt payload. Build and Validate run locally without
AWS credentials. Publish uploads the completed artifact to the destination
exported by [RunsOn installation](../runs-on/README.md), producing an AMI for
later execution through RunsOn.

Each operation has its own command and YAML result. Validation uses the built
disk without changing it; publication uploads that same disk without rebuilding.
Start from the repository checkout:

```sh
cd images
```

## Prerequisites

Use an x86-64 Linux host with Python 3.12, hardware virtualization, and read/write
access to `/dev/kvm`. On Ubuntu 24.04, install the VM tools and Python environment:

```sh
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
  qemu-system-x86 qemu-utils ovmf cloud-image-utils xorriso \
  openssh-client python3-venv
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 ../scripts/install-tools.py --group image --directory .local/tools
export PATH="$PWD/.local/tools/bin:$PATH"
export PACKER_PLUGIN_PATH="$PWD/.local/tools/plugins"
```

The tool installer downloads checksum-verified Packer and its QEMU plugin using
the versions in [inputs.lock.json](xenomai-cobalt/inputs.lock.json). Build also
downloads the pinned Ubuntu cloud disk and public kernel, userspace, and package
inputs. It creates no AWS resources.

The default build VM uses 4 CPUs and 8 GiB of memory; leave memory for the host as
well. Its root disk is 16 GiB, with a separate disposable 16 GiB build disk.
Allow storage for those sparse disks, downloaded inputs, logs, and validation's
temporary overlay. Actual host disk consumption depends on the build's written
data. The default validation VM uses 2 CPUs and 2 GiB of memory.

Publishing additionally needs AWS CLI v2, an authorized AWS login or GitHub OIDC
session, and [AWS Labs coldsnap](https://github.com/awslabs/coldsnap). Install
Rust 1.94.1 or newer with Cargo, then build coldsnap into the same local tool
directory. On Ubuntu, its native dependencies need a C/C++ toolchain and CMake:

```sh
sudo apt-get install --yes --no-install-recommends build-essential cmake pkg-config
cargo install --locked coldsnap --version 0.12.0 --root .local/tools
```

Make `aws` and `coldsnap` available on `PATH` before publishing.

## Build

Create a finalized raw disk and its manifest in a new output directory:

```sh
python3 build.py --output .local/build --cpus 4 --memory-mib 8192
```

The [Packer QEMU builder](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu)
boots the pinned source disk with UEFI, compiles the Cobalt kernel and userspace,
installs the GitHub runner and RunsOn bootstrap, and finalizes the root disk.
The disposable build disk and temporary login material are removed when the
build finishes. Follow progress in `.local/build/packer.log`.

Success produces `.local/build/disk.raw` and `.local/build/build.yaml`. The
manifest records the disk digest, recipe identity, pinned source, and boot/runtime
requirements. Keep it with `image-manifest.json` and the disk; paths in the YAML
resolve relative to the manifest. Use a new output directory for another build.

Both Build and Validate default to KVM and require access to `/dev/kvm`.
`--accelerator tcg` explicitly selects slow software emulation when hardware
virtualization is unavailable. There is no silent fallback from KVM to TCG.

## Validate with QEMU/KVM

Boot the completed image and run the functional Cobalt application test:

```sh
python3 validate.py \
  --build .local/build/build.yaml \
  --output .local/validation
```

Validation boots the complete disk through its UEFI firmware and bootloader,
using a disposable QEMU overlay and temporary SSH access. It checks the running
kernel and baked image identity, then builds and runs the
[Cobalt application](../tests/cobalt). The application starts an Alchemy task
and verifies Cobalt primary-mode execution. Validation also verifies that the
input disk's digest is unchanged.

The default deadline is 300 seconds. Use `--timeout-seconds`, `--cpus`, or
`--memory-mib` to select appropriate guest limits; software emulation may need
more time. Results and serial, QEMU, SSH, and guest evidence are written to the
selected empty output directory. `validation.yaml` identifies the exact disk
and reports `status: passed` only after the checks succeed.

This establishes boot and functional Cobalt execution on the selected VM.
EC2 launch, RunsOn bootstrap integration, runner registration, and job scheduling
belong to the separate [RunsOn execution module](../execution/README.md). Functional validation does not
measure real-time latency.

The [Build and validate image workflow](../.github/workflows/image-build-validate.yml)
runs the same commands on standard GitHub-hosted `ubuntu-24.04` for relevant
pull requests and manual dispatches. It checks the KVM API and records host
memory and disk availability. One [successful hosted
run](https://github.com/OptimalCNC/runs-on-ami-example/actions/runs/35004588489)
built the disk in 18 minutes 41 seconds with a 4-CPU, 8 GiB guest and completed
validation in 47 seconds with a 2-CPU, 2 GiB guest. Validation booted through
UEFI into `6.12.90-cip24-xenomai-cobalt` with Xenomai 3.3.3, passed the Cobalt
application test as the ordinary runner user, and confirmed an unchanged disk
digest. The host had 4 CPUs, approximately 15 GiB of memory, and 86 GiB of
initially available disk space; this single measurement does not establish fit
on a 14 GiB disk or guarantee those timings. Community projects such as
[mkosi](https://github.com/systemd/mkosi/blob/main/.github/workflows/ci.yml) use
GitHub-hosted Linux for complete-disk VM boot tests.

For a manual run that retains a compressed disk artifact:

```sh
gh workflow run image-build-validate.yml -f retain_image=true
```

The workflow retains reports for 7 days and, when requested on a successful
manual run, the compressed disk bundle for 1 day. It has no publishing step and
needs no AWS credentials. Download and extract the retained bundle to obtain
`build.yaml`, `disk.raw`, and `image-manifest.json` for later publication.

## Publish to the installation's target

Obtain `publishing.yaml` from the RunsOn installation. Its destination account,
region, publisher role, encryption key, and required tags are the publishing
contract. The [installation permissions guide](../runs-on/PERMISSIONS.md#publishing-access)
describes local role profiles and GitHub OIDC authentication.

Publish the selected built artifact using an existing authorized AWS profile:

```sh
python3 publish.py \
  --build .local/build/build.yaml \
  --target ../runs-on/.local/contracts/publishing.yaml \
  --output .local/publication \
  --profile your-authorized-login
```

The command assumes the contract's publisher role when needed and verifies the
resulting account and role. An existing session for that publisher role is also
accepted. For GitHub Actions, obtain temporary credentials through OIDC in the
contract's protected environment, then omit `--profile`. Use the exact subject
from the contract, including immutable repository/account identifiers where
present; Build and Validate do not need that publishing authority.

Publication uploads the raw disk through EBS direct APIs with the installation's
encryption key, waits for its snapshot, registers a UEFI x86-64 AMI with ENA
support, and checks the resulting identity. It does not run Build or Validate;
choose the artifact whose validation evidence you accept. Successful publication
writes `published-image.yaml` with `status: available`, the AMI and snapshot IDs,
source disk digest, baked-manifest digest (`artifact.manifest_sha256`), target
identity, and compatibility requirements. [RunsOn execution](../execution/README.md)
checks the guest manifest against that digest before accepting its kernel and
SDK evidence. Availability is an AWS publication result; execution establishes
runtime qualification.

The current upload includes zero blocks to preserve encrypted snapshot data, so
a sparse 16 GiB raw disk still transfers its full logical 16 GiB. Publication
incurs [EBS direct API and snapshot storage charges](https://aws.amazon.com/ebs/pricing/),
plus applicable KMS requests and source network charges. It creates no EC2 build
instance or S3 staging bucket. Images remain in the contract's account and region;
sharing or copying them into another account or region requires separate AMI,
snapshot, and customer-managed key access and lifecycle management.

## Outputs and cleanup

Keep operation results with their corresponding artifacts and publishing target:

| Output | Purpose |
| --- | --- |
| `build/disk.raw`, `build/build.yaml` | Final disk and its digest, recipe, and compatibility contract |
| `build/image-manifest.json`, `build/parent-inventory.json`, `build/packer.log` | Payload identity, source inventory, and build evidence |
| `validation/validation.yaml` and neighboring logs | VM result tied to the disk digest, with boot and Cobalt evidence |
| `publication/published-image.yaml` | Available AMI and backing snapshot tied to the built artifact |
| `publication/publication-state.yaml`, `publication/upload.log` | Publication progress and recovery information |

`.local/` and `.venv/` are Git-ignored. Once their evidence or artifacts are no
longer needed, local build/validation outputs, tool downloads, and caches can be
removed independently of AWS resources. Retain publication records and the
matching target contract while their AMIs or snapshots exist.

Delete a published image and its backing snapshot using its record:

```sh
python3 publish.py \
  --cleanup .local/publication/published-image.yaml \
  --target ../runs-on/.local/contracts/publishing.yaml \
  --profile your-authorized-login
```

Cleanup verifies the target, ownership and artifact tags, publication identity,
and encryption key before deregistering the recorded AMI and deleting its
snapshot. It updates the supplied record to `status: deleted`. Individual image
retention belongs to this module; removing local files does not remove an AMI.

An interrupted publication preserves `publication-state.yaml` and attempts to
clean up its created resources. If the record reports `cleanup-needed`, recover
with the same cleanup command using that state file instead:

```sh
python3 publish.py \
  --cleanup .local/publication/publication-state.yaml \
  --target ../runs-on/.local/contracts/publishing.yaml \
  --profile your-authorized-login
```

Retire image publications before removing their installation-owned encryption
key. The installer checks for dependent snapshots and volumes during its own
removal procedure.
