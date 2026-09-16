# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

The repository separates three responsibilities:

- [RunsOn installation](runs-on/README.md) provisions the runner platform, its
  image-publishing destination, and the publisher role.
- [Image build and publishing](images/README.md) provides independent Build,
  Validate, and Publish operations. Build and Validate need no AWS credentials.
- [RunsOn execution](execution/README.md) consumes a published image and proves
  that it registers as a runner and builds and tests a Xenomai Cobalt application.

The image recipe builds Ubuntu 24.04 with a Dovetail-enabled Linux kernel,
Xenomai 3 Cobalt integration, and matching userspace development tools. The
Cobalt application test establishes functional execution without a latency target.

## Validate locally

Python 3.12 and Bash are required. These commands install the Python dependencies
and pinned validation tools without creating AWS resources:

```sh
python3 -m venv .tools/validation-venv
. .tools/validation-venv/bin/activate
python3 -m pip install -r images/requirements.txt -r runs-on/requirements.txt -r execution/requirements.txt
python3 scripts/install-tools.py --group validation
export PATH="$PWD/.tools/bin:$PATH"
export PACKER_PLUGIN_PATH="$PWD/.tools/plugins"
python3 scripts/validate-local.py --tools
```

The checks run the image, installer, and execution tests, validate locked inputs, and check
shell scripts, workflows, Packer configuration, and the installation's Terraform
blueprints. Terraform initialization uses the committed provider locks, and
these checks run without AWS credentials. Building the
[Cobalt application](tests/cobalt) additionally requires a C compiler, CMake
3.28+, and the Xenomai development installation; executing it requires the
Cobalt kernel. Local configuration checks do not establish either guest
bootability or Cobalt execution. Pull requests also run
the [image build and VM validation workflow](.github/workflows/image-build-validate.yml)
when its image-related paths change.

With a locally built Cobalt SDK, `python3 scripts/validate-local.py
--xenomai-prefix /path/to/SDK` also configures and links the application. It
does not run the Cobalt test on the host.

To build a complete disk and test Cobalt in a local QEMU/KVM guest, follow the
[image guide](images/README.md). Build and Validate require no AWS credentials.

## Supply deployment configuration

For a new installation, follow the [RunsOn guide](runs-on/README.md) from
`runs-on/`. Keep its input configuration, secret files, and Terraform states in
the Git-ignored `runs-on/.local/` directory. Successful deployment exports:

| Contract | Consumer |
| --- | --- |
| `runs-on/.local/contracts/installation.yaml` | RunsOn execution: installation identity, environment, setup URL, and runtime configuration |
| `runs-on/.local/contracts/publishing.yaml` | Image publishing: destination, role authentication, encryption key, and required ownership tags |

The [image module](images/README.md#publish-to-the-installations-target) consumes
the publishing contract through an explicit file path. Build and Validate
produce and test a local disk without AWS credentials; Publish uploads that
artifact only when requested.

## Run qualification

After GitHub App setup, run [Check RunsOn
installation](.github/workflows/runs-on-installation-smoke.yml) with the installed
RunsOn environment. The [installation guide](runs-on/README.md#finish-the-github-setup)
provides the dispatch command and acceptance criteria. The check uses a stock
image to prove EC2 launch, runner registration, and job execution.

[RunsOn execution](execution/README.md) provides custom-image qualification,
consuming `installation.yaml` and the image module's `published-image.yaml`.
Its workflow checks the actual EC2 image and kernel, then compiles and runs
the Cobalt application as the ordinary runner user.

## Operate and reuse

[RunsOn installation](runs-on/README.md) owns deployment, permissions, exported
contracts, and installation removal. [Image build and publishing](images/README.md)
owns disk artifacts, VM validation, published AMIs, and their cleanup.
[RunsOn execution](execution/README.md) owns the application workflow and its
runtime evidence.

Repository code uses the [MIT license](LICENSE); the pinned Linux and Xenomai
sources retain their upstream licenses.
