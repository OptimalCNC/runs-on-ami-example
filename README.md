# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

The repository separates three responsibilities:

- [RunsOn installation](runs-on/README.md) provisions the runner platform, its
  image-publishing destination, and the publisher role.
- [Image build, validation, and publishing](images/README.md) builds a disk,
  validates it in a VM, and publishes it as an AMI. Build and validation run on
  normal Linux hosts without AWS credentials.
- [RunsOn execution](execution/README.md) provides a Xenomai Cobalt example
  application and a workflow that builds and tests it on a custom image.

The image recipe builds Ubuntu 24.04 with a Dovetail-enabled Linux kernel,
Xenomai 3 Cobalt integration, and matching userspace development tools. The
Cobalt application test establishes functional execution without a latency target.

## Validate locally

Run the validation commands documented by the module you are changing:

- [Images](images/README.md#build-and-validate): Build the disk, then boot it in a
  fresh VM to build and run the Cobalt application.
- [RunsOn installation](runs-on/README.md#prerequisites): Python tests and
  isolated Terraform validation and mock tests.
- [Execution](execution/README.md): CMake build and CTest commands for a
  Cobalt SDK and kernel.

The [validation workflow](.github/workflows/validate.yml) runs installation
source checks and workflow linting as independent jobs.
Installed tools and generated files stay in each module's
ignored `.local/` directory.

For workflow changes, install and run the pinned actionlint from the repository
root on Linux x86-64. These commands require `curl`, `jq`, `sha256sum`, and `tar`:

```sh
(
  set -eu
  mkdir -p .github/.local/tools/bin
  curl --fail --location --silent --show-error \
    "$(jq -r '.actionlint.url' .github/tools.lock.json)" \
    --output .github/.local/tools/actionlint.tar.gz
  printf '%s  %s\n' "$(jq -r '.actionlint.sha256' .github/tools.lock.json)" \
    .github/.local/tools/actionlint.tar.gz | sha256sum --check
  tar -xzf .github/.local/tools/actionlint.tar.gz -C .github/.local/tools/bin actionlint
  .github/.local/tools/bin/actionlint -shellcheck= -config-file=.github/actionlint.yaml .github/workflows/*.yml
)
```

To build a complete disk on a Linux host, follow the [image guide](images/README.md).
Pull requests run the [image workflow](.github/workflows/image-build.yml) with
separate build and VM validation jobs on GitHub-hosted Ubuntu when its
image-related paths change. Weekly scheduled runs publish the validated disk;
manual dispatch can also request publication.

## Supply deployment configuration

For a new installation, follow the [RunsOn guide](runs-on/README.md) from
`runs-on/`. Keep its input configuration, secret files, and Terraform states in
the Git-ignored `runs-on/.local/` directory. Successful deployment exports:

| Contract | Consumer |
| --- | --- |
| `runs-on/.local/contracts/installation.yaml` | Installation setup and runner configuration: identity, environment, setup URL, and runtime configuration |
| `runs-on/.local/contracts/publishing.yaml` | Image publishing: destination, role authentication, and required ownership tags |

The [image module](images/README.md#publish-to-the-installations-target) consumes
the publishing contract through an explicit file path. Build produces a local
disk, Validate boots it in a fresh VM, and Publish uploads that artifact.

## Run on RunsOn

After GitHub App setup, run [Check RunsOn
installation](.github/workflows/runs-on-installation-smoke.yml) with the installed
RunsOn environment. The [installation guide](runs-on/README.md#finish-the-github-setup)
provides the dispatch command and acceptance criteria. The check uses a stock
image to prove EC2 launch, runner registration, and job execution.

[RunsOn execution](execution/README.md) builds and tests the Cobalt example
application on a custom image. Dispatch its workflow with the AMI ID, RunsOn
environment, and AWS region.

## Operate and reuse

[RunsOn installation](runs-on/README.md) owns deployment, permissions, exported
contracts, and installation removal. [Image build, validation, and publishing](images/README.md)
owns disk artifacts, VM validation, published AMIs, and their cleanup.
[RunsOn execution](execution/README.md) owns the example application and its
build-and-test workflow.

Repository code uses the [MIT license](LICENSE); the pinned Linux and Xenomai
sources retain their upstream licenses.
