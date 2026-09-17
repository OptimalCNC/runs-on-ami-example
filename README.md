# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

The repository separates three responsibilities:

- [RunsOn installation](runs-on/README.md) provisions the runner platform, its
  image-publishing destination, and the publisher role.
- [Image build and publishing](images/README.md) provides independent Build,
  Validate, and Publish operations. Build and Validate need no AWS credentials.
- [RunsOn execution](execution/README.md) provides a Xenomai Cobalt example
  application and a workflow that builds and tests it on a custom image.

The image recipe builds Ubuntu 24.04 with a Dovetail-enabled Linux kernel,
Xenomai 3 Cobalt integration, and matching userspace development tools. The
Cobalt application test establishes functional execution without a latency target.

## Validate locally

Python 3.12 and Bash are required. These commands install the Python dependencies
and pinned validation tools without creating AWS resources:

```sh
python3 -m venv .github/.local/validation-venv
. .github/.local/validation-venv/bin/activate
python3 -m pip install -r images/requirements.txt -r runs-on/requirements.txt
python3 images/install-tools.py
python3 runs-on/install-tools.py --terraform-only
python3 .github/install-tools.py
python3 check.py --tools
```

The checks run the image and installer tests, validate locked inputs, and check
shell scripts, workflows, Packer configuration, and the installation's Terraform
blueprints. Terraform initialization uses the committed provider locks, and
these checks run without AWS credentials. Building the
[Cobalt application](execution/cobalt) additionally requires a C compiler, CMake
3.28+, and the Xenomai development installation; executing it requires the
Cobalt kernel. Local configuration checks do not establish either guest
bootability or Cobalt execution. Pull requests also run
the [image build and VM validation workflow](.github/workflows/image-build-validate.yml)
when its image-related paths change.

The image and installation modules own their tools and checks; run
`python3 check.py` from their directories. The root `check.py` delegates to both and
adds workflow linting with `--tools`. Installed tools and generated files stay
in the owning module's ignored `.local/` directory; `.github/` owns CI tooling.

See [Execution](execution/README.md) for the CMake build and CTest commands.
Building requires a Cobalt SDK; running the test requires the Cobalt kernel.

To build a complete disk and test Cobalt in a local QEMU/KVM guest, follow the
[image guide](images/README.md). Build and Validate require no AWS credentials.

## Supply deployment configuration

For a new installation, follow the [RunsOn guide](runs-on/README.md) from
`runs-on/`. Keep its input configuration, secret files, and Terraform states in
the Git-ignored `runs-on/.local/` directory. Successful deployment exports:

| Contract | Consumer |
| --- | --- |
| `runs-on/.local/contracts/installation.yaml` | Installation setup and runner configuration: identity, environment, setup URL, and runtime configuration |
| `runs-on/.local/contracts/publishing.yaml` | Image publishing: destination, role authentication, encryption key, and required ownership tags |

The [image module](images/README.md#publish-to-the-installations-target) consumes
the publishing contract through an explicit file path. Build and Validate
produce and test a local disk without AWS credentials; Publish uploads that
artifact only when requested.

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
contracts, and installation removal. [Image build and publishing](images/README.md)
owns disk artifacts, VM validation, published AMIs, and their cleanup.
[RunsOn execution](execution/README.md) owns the example application and its
build-and-test workflow.

Repository code uses the [MIT license](LICENSE); the pinned Linux and Xenomai
sources retain their upstream licenses.
