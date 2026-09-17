# Build, validate, and publish a Cobalt image

This module builds a bootable Ubuntu 24.04 disk containing the pinned Linux,
Dovetail, and Xenomai 3 Cobalt payload, validates it in a fresh VM, then publishes
it as an EC2 AMI. All three commands run on a normal Linux host. Building and VM
validation need no AWS credentials; publishing uploads the completed disk.

- `build/` owns the Packer recipe, provisioning scripts, and disk construction.
- `validate/` owns the QEMU boot and Cobalt application execution.
- `publish/` owns the AWS upload, AMI registration, and publication cleanup.

The commands share the disk contract in `contracts.py` and the tool installer.

Start from the repository checkout:

```sh
cd images
```

## Prerequisites

Use an x86-64 Linux host with Python 3.12. Build and validation use only Python's
standard library. The host needs neither a Cobalt kernel nor a RunsOn
installation. Building uses KVM by default and requires read/write
access to `/dev/kvm`; `--accelerator tcg` selects slower software emulation when
hardware virtualization is unavailable. On Ubuntu 24.04, install the build tools:

```sh
sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
  qemu-system-x86 qemu-utils ovmf cloud-image-utils xorriso \
  openssh-client
python3 install-tools.py
export PATH="$PWD/.local/tools/bin:$PATH"
export PACKER_PLUGIN_PATH="$PWD/.local/tools/plugins"
```

The tool installer downloads checksum-verified Packer and its QEMU plugin using
the versions in [inputs.lock.json](build/xenomai-cobalt/inputs.lock.json). Build also
downloads the pinned Ubuntu cloud disk and public kernel, userspace, and package
inputs. It creates no AWS resources.

The default build VM uses 4 CPUs and 8 GiB of memory; leave memory for the host as
well. Its root disk is 16 GiB, with a separate disposable 16 GiB build disk.
Allow storage for those sparse disks, downloaded inputs, and build logs. Actual
host disk consumption depends on the build's written data.

Publishing additionally needs AWS CLI v2, an authorized AWS login or GitHub OIDC
session, and [AWS Labs coldsnap](https://github.com/awslabs/coldsnap). The tool
installer's `publish` group installs AWS CLI pinned in [tools.lock.json](publish/tools.lock.json).
Install Rust 1.94.1 or newer with Cargo, then build coldsnap into the same local
tool directory. Publishing uses PyYAML to read the installation's YAML target;
install its Python dependency in a virtual environment. On Ubuntu, coldsnap's
native dependencies need a C/C++ toolchain and CMake:

```sh
sudo apt-get install --yes --no-install-recommends python3-venv build-essential cmake pkg-config
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r publish/requirements.txt
python3 install-tools.py --group publish
cargo install --locked coldsnap --version 0.12.0 --root .local/tools
```

Make `aws` and `coldsnap` available on `PATH` before publishing.

## Build and validate

Create a finalized raw disk and its JSON manifest in a new output directory:

```sh
python3 -m build --output .local/build --cpus 4 --memory-mib 8192
```

The [Packer QEMU builder](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu)
boots the pinned source disk with UEFI, compiles the Cobalt kernel and userspace,
installs the GitHub runner and RunsOn bootstrap, and finalizes the root disk.
The disposable build disk and temporary login material are removed when the
build finishes. Follow progress in `.local/build/packer.log`.

Success produces `.local/build/disk.raw` and `.local/build/build.json`. The version
2 manifest records the disk path, digest, size, kernel release, and Xenomai version.
Keep it with the disk; its disk path resolves relative to the manifest. Use a new
output directory for another build.

Boot the completed image in a fresh VM and run the Cobalt application as the
image's `runner` user:

```sh
python3 -m validate --build .local/build/build.json \
  --output .local/validation --timeout-seconds 600
```

Validation uses a disposable QCOW2 overlay, checks the booted kernel and Xenomai
version, then builds and runs the Cobalt application with a 20-second execution
timeout. The example application is supplied for this VM run. The command must
succeed before publishing. Logs are written to `.local/validation`; validation
needs the same QEMU, OVMF, cloud-init seed, and SSH host tools used by the build.

The [image workflow](../.github/workflows/image-build.yml) runs separate `build`
and `validate` jobs on GitHub-hosted `ubuntu-24.04` for relevant pull requests,
manual dispatches, and its weekly schedule. The build job uploads a compressed
sparse disk bundle; the validation job downloads it and boots the VM on its own
runner. After validation succeeds, scheduled runs publish automatically; manual
dispatches publish when requested. A separate `clean` job runs after each
`publish` job, including failed publish jobs, and skips runs without publication.
Cleanup installs only the AWS CLI and Python dependencies; it needs no disk bundle.

Every successful build retains its disk bundle for 1 day so later jobs can
consume it. Build and VM validation logs are retained for 7 days. Download and
extract the disk bundle to obtain `build.json` and `disk.raw`. Build and
validation jobs need no AWS credentials.

## Publish to the installation's target

Obtain `publishing.yaml` from the RunsOn installation. Its destination account,
region, publisher role, unencrypted destination, and required tags are the publishing
contract. The [installation permissions guide](../runs-on/PERMISSIONS.md#publishing-access)
describes local role profiles and GitHub OIDC authentication.

Publish the selected built artifact using an existing authorized AWS profile:

```sh
python3 -m publish \
  --build .local/build/build.json \
  --target ../runs-on/.local/contracts/publishing.yaml \
  --output .local/publication \
  --profile your-authorized-login
```

The command assumes the contract's publisher role when needed and verifies the
resulting account and role. An existing session for that publisher role is also
accepted. For GitHub Actions, select the matching repository entry in
`authentication.github.repositories` from the version 3 target contract. Obtain
temporary credentials through OIDC in that entry's protected environment, then
omit `--profile`. Use its exact subject, including immutable repository/account
identifiers where present; Build and Validate do not need that publishing authority.

To publish through `image-build.yml`, set the authorized GitHub environment's
`PUBLISHING_TARGET` variable to the complete exported contract, then dispatch
with publication enabled. For an environment named `production`:

```sh
gh variable set PUBLISHING_TARGET --env production \
  < ../runs-on/.local/contracts/publishing.yaml
gh workflow run image-build.yml -f publish_image=true -f environment=production
```

The publication job reads its role, account, and region from that contract,
checks that it authorizes the repository and environment, and authenticates
through GitHub OIDC. It downloads the same disk bundle used by validation.
Without `publish_image=true`, manual dispatch runs only build and validation.
Publication records are retained as workflow artifacts for 90 days; keep a copy
with the target contract for the lifetime of the AWS resources.

Publication uploads the raw disk through EBS direct APIs without encryption,
waits for its snapshot, registers a UEFI x86-64 AMI with ENA
support, and checks the resulting identity. Successful publication
writes `published-image.json` with `status: available`, the AMI and snapshot IDs,
source disk digest, and target identity for diagnostics and cleanup. Each version
has a unique `ubuntu2404-xenomai-cobalt-<digest>-<publication>` name, as required
by AWS. The AMI and its snapshot also carry the exact stable `Name` tag
`ubuntu2404-xenomai-cobalt` for cleanup. The
[execution workflow](../execution/README.md) selects `image=ubuntu2404-xenomai-cobalt`
through the owner and name pattern in [`.github/runs-on.yml`](../.github/runs-on.yml), so new
publications require no workflow changes. A new version becomes eligible for
execution as soon as it is available; VM validation must finish before publishing.

EBS encryption by default must be disabled in the destination account and region:
AWS cannot create unencrypted snapshots while it is enabled. Publishing checks
this setting before uploading and verifies that the snapshot and AMI are
unencrypted. It does not change the account setting. RunsOn omits explicit
runner-volume encryption settings, leaving encryption to the source image and
regional EBS defaults. Unencrypted publication does not make the AMI public or
grant additional launch access.

The current upload includes zero blocks, so a sparse 16 GiB raw disk still
transfers its full logical 16 GiB. Publication
incurs [EBS direct API and snapshot storage charges](https://aws.amazon.com/ebs/pricing/),
plus applicable source network charges. It creates no EC2 build
instance or S3 staging bucket. Images remain in the contract's account and region;
sharing or copying them into another account or region requires separate AMI
and snapshot access and lifecycle management.

## Outputs and cleanup

The separate `clean` job keeps the newest available publication and its one
backing snapshot for this installation's exact `Name` tag
`ubuntu2404-xenomai-cobalt`. It deregisters older matching images and deletes
their snapshots. Different names, including names with an additional suffix,
do not match. Installation ownership is identified by resource tags. Cleanup
also runs after failed publication, retaining the newest existing available image.
The GitHub workflow serializes publishing runs through cleanup; run local
publication and cleanup commands serially for the same target as well.

Keep operation results with their corresponding artifacts and publishing target:

| Output | Purpose |
| --- | --- |
| `build/disk.raw`, `build/build.json` | Final disk, its digest and size, and expected kernel and Xenomai versions |
| `build/packer.log`, `build/serial.log` | Build diagnostics |
| `validation/*.log` | VM boot, application build, and execution diagnostics |
| `publication/published-image.json` | Publication progress, AMI and snapshot identity, and cleanup state |
| `publication/upload.log` | Snapshot upload diagnostics |

`.local/` and `.venv/` are Git-ignored. Once their logs or artifacts are no
longer needed, local build outputs, tool downloads, and caches can be
removed independently of AWS resources. Retain publication records and the
matching target contract while their AMIs or snapshots exist.

Retirement records deletion intent on each old snapshot before deregistering its
image, so a later cleanup can retry unfinished snapshot deletion. If retirement
fails, the `clean` job fails independently and the new image remains available.
Run cleanup after local publication, or retry it without rebuilding or publishing:

```sh
python3 -m publish --prune \
  --target ../runs-on/.local/contracts/publishing.yaml \
  --profile your-authorized-login
```

Automatic retention covers only publications with the exact stable `Name` tag,
Cobalt family tag, and expected versioned AMI name. Use their saved records to
clean up publications from before this naming scheme. GitHub artifact expiry and
deletion of local files do not remove AWS resources.

Delete a published image and its backing snapshot using its record:

```sh
python3 -m publish \
  --cleanup .local/publication/published-image.json \
  --target ../runs-on/.local/contracts/publishing.yaml \
  --profile your-authorized-login
```

Cleanup verifies the target, ownership and artifact tags, and publication
identity before deregistering the recorded AMI and deleting its
snapshot. It updates the supplied record to `status: deleted`. Individual image
retention belongs to this module; removing local files does not remove an AMI.

Publication creates `published-image.json` before uploading and updates that
same record as resources are created. An interrupted publication preserves the
record and attempts to clean up its created resources. If it reports
`cleanup-needed`, retry the cleanup command above. Cleanup also accepts legacy
YAML records: `published-image.yaml` and `publication-state.yaml` retained from
earlier publications.

Version 1 and 2 publishing targets remain supported only for cleanup of existing
encrypted publications, including verification of their original encryption key.
Retain those targets with their publication records. New publication requires a
version 3 target exported after updating the installation. Existing AMIs and
snapshots cannot be decrypted in place; publish the built disk again to create an
unencrypted replacement. Before updating the installation, stop runner jobs and
migrate or retire snapshots and volumes that use its legacy key. The installer
blocks bootstrap updates, deployment apply, and removal while those dependencies
remain; a successful deployment update retires the key. See the
[installation update instructions](../runs-on/README.md#updates-exports-and-removal).
