# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

Start with the dedicated [RunsOn installation guide](runs-on/README.md) to
provision the runner platform and its image-publishing destination and role.
That module is the current installation path and includes a stock-image smoke
workflow.

The repository separates RunsOn installation, image build and publishing, and
execution through RunsOn. The independent [image module](images/README.md)
provides separate Build, Validate, and Publish operations. RunsOn execution
comes next: it will consume a published image and prove that it registers as a
runner and executes the intended job.

The previous combined EC2/Packer image pipeline is retired. Its build,
qualification, application, and scheduled cleanup workflows have been removed.
Legacy scripts and recorded results remain available for migration and recovery
of existing resources. The independent image recipe builds Ubuntu 24.04
with a Dovetail-enabled Linux kernel, Xenomai 3 Cobalt integration, and matching
userspace development tools. The Cobalt application test establishes functional
execution without a latency target.

## Validate locally

Python 3.12, a C compiler, CMake 3.28+, Bash, `unzip`, `dpkg-deb`, and OpenSSH
client tools are needed. These commands download development tools but do not
create AWS resources:

```sh
python3 -m venv .tools/validation-venv
. .tools/validation-venv/bin/activate
python3 -m pip install --require-hashes -r requirements-validation.txt
python3 scripts/install-tools.py --group validation
export PATH="$PWD/.tools/bin:$PATH"
export PACKER_PLUGIN_PATH="$PWD/.tools/plugins"
python3 scripts/validate-local.py --tools
```

The checks cover locked inputs, result schemas, ownership and cleanup rules,
registration deadlines, freshness and reproducibility decisions, workflows,
and Packer configuration. Terraform initialization uses the committed provider
lock and local validation runs without AWS credentials. Building the
[Cobalt application](tests/cobalt) requires the Xenomai development installation;
executing it requires the Cobalt kernel. Local configuration checks do not
establish either guest bootability or Cobalt execution. Pull requests also run
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

The former `.deployment/` specification, bindings, parent inventories, and
accepted-image records belong to the retired pipeline. Preserve existing
records and credentials needed to recover its resources; the
[legacy infrastructure reference](infra/README.md) describes their layout.

## Run the approved qualification

After GitHub App setup, run [Check RunsOn
installation](.github/workflows/runs-on-installation-smoke.yml) with the installed
RunsOn environment. The [installation guide](runs-on/README.md#finish-the-github-setup)
provides the dispatch command and acceptance criteria. The check uses a stock
image to prove EC2 launch, runner registration, and job execution.

Custom-image qualification will be provided by the separate RunsOn execution
module, consuming the image module's `published-image.yaml`. The retired pipeline's
single-build, retained-image, application, and reproducibility dispatches are
no longer available. Preserve their existing evidence as historical results;
new qualification must identify the actual artifact and runtime it tests.

## Operate and reuse

[RunsOn installation](runs-on/README.md) owns deployment, permissions, exported
contracts, and installation removal. [Image build and publishing](images/README.md)
owns disk artifacts, VM validation, published AMIs, and their cleanup. The legacy [operations
reference](docs/operations.md) retains result formats and standalone cleanup
commands for resources created by the retired image pipeline. Its scheduled
cleanup no longer runs; retain an operator and the saved cleanup context until
those resources have been retired.

The [legacy infrastructure reference](infra/README.md) describes the old
controller, builder, and probe infrastructure. The [original implementation
plan](docs/plans/custom-ami-example.md) records that retired architecture. These
references support migration and recovery while the RunsOn execution module is
implemented.

When reusing a qualified image recipe or application test, record its reviewed
Git commit and the associated qualification evidence in the companion project's
source-copy attribution. Repository code uses the [MIT license](LICENSE); the
pinned Linux and Xenomai sources retain their upstream licenses.
