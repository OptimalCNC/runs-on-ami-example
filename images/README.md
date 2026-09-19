# Build, validate, and publish a Cobalt image

This module builds a bootable Ubuntu 24.04 disk containing the pinned Linux,
Dovetail, and Xenomai 3 Cobalt payload, validates it in a fresh VM, then publishes
it as an EC2 AMI. All three commands run on a normal Linux host. Building and VM
validation need no AWS credentials; publishing uploads the completed disk.

For on-demand or scheduled automation, see
[GitHub Actions workflows](#github-actions-workflows).

- [build/](build/) owns VM configuration, disk construction, and finalization.
- [recipes/](recipes/README.md) owns the base image definition, ordered
  installation scripts, and their inputs.
- [validate/](validate/) owns the QEMU boot and Cobalt application execution.
- [publish/](publish/README.md) owns the AWS upload, AMI registration, and
  publication cleanup, with its own configuration, recovery guide, and local checks.

Build produces a raw disk file; validation and publishing take its path.

## Prerequisites

Use an x86-64 Ubuntu 24.04 host with Python 3.12. The Python commands use only the
standard library. Build and validation require QEMU (`qemu-system-x86_64` and
`qemu-img`), OVMF firmware, `cloud-localds`, and OpenSSH client tools (`ssh` and
`ssh-keygen`). Building also needs `xorriso`. Install these system packages and
make their commands available on `PATH`:

```sh
sudo apt install \
  qemu-system-x86 qemu-utils ovmf cloud-image-utils xorriso \
  openssh-client
```

Building also requires [Packer](https://developer.hashicorp.com/packer/install)
1.16.0 and its QEMU plugin 1.1.6, as declared in the
[Packer template](build/image.pkr.hcl). With Packer on `PATH`, install the plugin:

```sh
packer init build/image.pkr.hcl
```

The host needs neither a Cobalt kernel nor a RunsOn installation. Build and
validation use KVM by default and require read/write access to `/dev/kvm`;
`--accelerator tcg` selects slower software emulation when hardware
virtualization is unavailable.

The default build VM uses 4 CPUs and 8 GiB of memory; leave memory for the host as
well. Its root disk is 16 GiB, with a separate disposable 16 GiB build disk.
Allow storage for those sparse disks, downloaded inputs, and build logs. Actual
host disk consumption depends on the build's written data.

For publishing tools and AWS access, follow the
[publisher prerequisites](publish/README.md#prerequisites).

## Build and validate

Create a finalized raw disk in a new output directory:

```sh
python3 -m build.image --recipe recipes --output .local/build --cpus 4 --memory-mib 8192
```

`--recipe` selects an [installation recipe](recipes/README.md). Its
[recipe.json](recipes/recipe.json) defines the source disk and ordered scripts;
[build/image.json](build/image.json) configures disk sizes, the SSH user, and
initial cloud-init settings. The default recipe uses the latest released Ubuntu
24.04 cloud disk, installs the runner and development tools, and builds the
pinned Cobalt kernel and SDK.

Follow progress in `.local/build/packer.log`. A failed step stops the build;
success produces the finalized `.local/build/disk.raw`. Use a new output
directory for another build.

Boot the completed image in a fresh VM and run the Cobalt application as the
image's `runner` user:

```sh
python3 -m validate.image --image .local/build/disk.raw \
  --output .local/validation --timeout-seconds 600
```

Validation uses a disposable QCOW2 overlay, then builds and runs the Cobalt
application with a 20-second execution timeout. The application verifies that a
real-time task runs in Cobalt primary mode at the required priority. Logs are
written to `.local/validation`. The command must succeed before publishing.

For publisher changes, its [local checks](publish/README.md#local-checks) exercise
configuration, publication, retention, and recovery without a VM or AWS access.

## Publish to AWS

Create `.local/publishing.json` using the
[publisher configuration and authentication instructions](publish/README.md#inputs).
Use `ubuntu2404-xenomai-cobalt` as the stable image name.

Publish the validated disk locally:

```sh
python3 -m publish.publish --config .local/publishing.json \
  --image .local/build/disk.raw --output .local/publication
```

## GitHub Actions workflows

The [image workflow](../.github/workflows/image-build.yml) runs separate `build`,
`validate`, `publish`, and `clean` jobs on GitHub-hosted `ubuntu-24.04`, installing
the host tools it needs. Use it as a reference for on-demand or weekly image
builds and publication. The build job uploads a compressed sparse disk bundle,
which validation boots on its own runner. Publication consumes the same bundle
after validation succeeds.

The separate [repository validation workflow](../.github/workflows/validate.yml)
runs the publisher's [local checks](publish/README.md#local-checks) and workflow
linting on pull requests, pushes to `main`, and manual dispatches.

To adapt the image workflow to another repository, copy it into `.github/workflows/`
and keep `images/` and `execution/cobalt/` at their existing paths; VM validation
builds the Cobalt application from that checkout. Enable GitHub Actions and put
the workflow on the repository's default branch for manual and scheduled runs.
Install and authenticate GitHub CLI (`gh`) to use the examples below.

Create the chosen GitHub environment in repository settings. Configure its
`PUBLISHING_CONFIG` variable with the [publishing configuration](#publish-to-aws)
and `PUBLISHING_ROLE_ARN` with the IAM role authorized for GitHub OIDC. The
[RunsOn installation guide](../runs-on/README.md#updates-exports-and-removal)
explains how its exported account, region, ownership tags, and publisher role
supply these values. For an environment named `production`:

```sh
gh variable set PUBLISHING_CONFIG --env production < .local/publishing.json
gh variable set PUBLISHING_ROLE_ARN --env production \
  --body arn:aws:iam::123456789012:role/image-publisher
```

The workflow obtains temporary credentials through OIDC before calling the
publisher. AWS checks the repository and environment against the role's trust.

| Trigger in `image-build.yml` | Behavior |
| --- | --- |
| Pull request changing `images/**`, `execution/cobalt/**`, or `.github/workflows/image-build.yml` | Build and validate only. |
| Manual (`workflow_dispatch`) | Build and validate; publish and clean when `publish_image=true`. The `environment` input defaults to `production`. |
| Schedule (`0 0 * * 1`) | Every Monday at 00:00 UTC, build, validate, publish, and clean from the default branch using `production`. |

To run on demand, select the workflow in GitHub's **Actions** tab and choose
**Run workflow**, or use GitHub CLI from this checkout:

```sh
# Build and validate only.
gh workflow run image-build.yml

# Build, validate, publish, and clean.
gh workflow run image-build.yml -f publish_image=true -f environment=production
```

For a different interval, edit the workflow's `on.schedule` cron expression
(UTC). Scheduled runs use the `production` fallback in both the `publish` and
`clean` job environments; change both if your publishing environment has another
name. On forks, enable the scheduled workflow in the Actions tab after
configuring publishing.

The [execution workflow](../execution/README.md) selects
`image=ubuntu2404-xenomai-cobalt` through the owner and name pattern in
[`.github/runs-on.yml`](../.github/runs-on.yml). A new publication becomes eligible
for execution as soon as it is available; VM validation must finish first.

## Outputs and cleanup

The examples write to the Git-ignored `.local/` directory. The image workflow
uploads the disk as a compressed bundle and retains these artifacts:

| Output under `.local/` | Purpose | GitHub retention |
| --- | --- | --- |
| `build/disk.raw` | Final raw disk | 1 day |
| `build/packer.log`, `build/serial.log` | Build diagnostics | 7 days |
| `validation/*.log` | VM boot, application build, and execution diagnostics | 7 days |
| `publication/published-image.json` | Publication identity and cleanup state | 90 days |
| `publication/upload.log` | Snapshot upload diagnostics | 90 days |

After each `publish` job, a separate `clean` job keeps the newest available
matching publication and its one backing snapshot, removing older versions.
It also runs after failed publication and skips runs without publication.
The workflow serializes publishing runs through cleanup.

For local publication, run the publisher's
[prune command](publish/README.md#inputs) afterward; the same guide provides
deletion of individual publications. Follow its
[failure and recovery instructions](publish/README.md#failure-and-recovery)
when publication or cleanup fails or is interrupted.

Keep publication records while their AMIs or snapshots exist. GitHub artifact
expiry and deletion of local files do not remove AWS resources.
