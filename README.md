# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

This example builds a small Ubuntu 24.04 AMI with Xenomai 3 Cobalt and uses it as a
RunsOn Flex runner. It starts from a plain Canonical Ubuntu image and installs
the runner user, GitHub Actions agent, and matching RunsOn bootstrap described
in the [RunsOn Linux AMI guide](https://runs-on.com/docs/guides/building-custom-ami/#linux-amis).
A temporary Packer builder compiles a Dovetail-enabled
Linux kernel with Cobalt integration and matching Xenomai userspace tools,
libraries, and headers. Fresh RunsOn jobs then build and run a small Cobalt
application that requires the running Cobalt kernel.

A single-build dispatch runs an independent EC2 boot probe and two fresh
RunsOn jobs, then retains the accepted AMI for a separate application dispatch.
The optional two-build qualification compares the installed
kernel, modules, Xenomai payload, configuration, packages, and unpacked
initramfs content. The application test checks functional execution; there is
no latency target.

The image build controller uses a separately pinned stock RunsOn AMI. It is
never snapshotted. The example targets Ubuntu 24.04, x86-64, one exact Nitro
runtime type, on-demand capacity, and an unsigned kernel with Secure Boot
disabled. The candidate root defaults to 16 GiB. Compilation and downloads use
a separate 16 GiB EBS disk that is excluded from the AMI and deleted with the
builder. The image keeps Python, Pip, venv, jq, Git, build essentials with GCC/G++,
CMake, Ninja, and the Cobalt SDK. Kernel build tools, the stock kernel, package
caches, and build inputs are removed before snapshotting.
Choose parent images and Packer settings in the deployment specification;
the [RunsOn configuration](.github/runs-on.yml) gives Cobalt jobs a 16 GiB root
and the stock controller a 30 GiB root. The infrastructure supports
installing [RunsOn Flex](https://runs-on.com/docs/) or importing an existing
installation; Fleet needs a separate adapter.

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
and both Packer modes. Terraform initialization uses the committed provider
lock and local validation runs without AWS credentials. Building the
[Cobalt application](tests/cobalt) requires the Xenomai development installation;
executing it requires the Cobalt kernel. Local configuration checks do not
establish either guest bootability or Cobalt execution. On pull requests,
only this validation workflow runs.

With a locally built Cobalt SDK, `python3 scripts/validate-local.py
--xenomai-prefix /path/to/SDK` also configures and links the application. It
does not run the Cobalt test on the host.

## Supply deployment configuration

The repository contains reusable image recipes, infrastructure definitions,
version locks, schemas, and neutral examples. Each deployment has its own
configuration and generated records. Start with the neutral
[deployment specification](examples/deployment.spec.json) and follow the
[infrastructure guide](infra/README.md) to create them in an ignored `.deployment/` directory or an external directory.

| Record | Purpose |
| --- | --- |
| `spec.json` | Operator choices: repository, AWS account/region, parent AMIs, instance sizes, networking, RunsOn settings, and retention |
| `bindings.json` | Resolved resource identities from Terraform outputs, RunsOn stack outputs, or explicit imports |
| `parents/` | Captured parent identities and their hashed inventories |
| `manifest.json` | Resolved deployment input combining the specification, bindings, and verified parent evidence |
| `plans/`, `state/`, `runs/` | Reviewed infrastructure plans, current image selection, and execution evidence |

Image commands take the resolved manifest through `--deployment PATH`. Paths inside
configuration records resolve relative to their containing record, so a
standalone command can use a deployment outside the checkout. Keep credentials,
license files, and notification addresses in private operator storage.

Bootstrap in dependency order: provision or import the foundation roles and
artifact store; install or import RunsOn; configure management access using
the resulting network; capture the selected parents; then resolve the
manifest. This prevents image builds from depending on resources that only a
later installation step can create.

For GitHub execution, configure the protected environment selected by the
repository variable `AMI_DEPLOYMENT_ENVIRONMENT` (default `ami-build`) and its
allowed branches. Set its `AMI_CONTROLLER_ROLE_ARN`, `AMI_REGION`, and
`AMI_STATE_URI` variables to the controller role, AWS region, and private
versioned S3 state prefix. Publish the deployment bundle using
`scripts/deployment-state.py`; workflows fetch it using short-lived OIDC
credentials and freeze its exact version and digest for the run attempt.
The [infrastructure guide](infra/README.md#review-and-apply) owns these commands.

The RunsOn environment must propagate the common EC2 tag
`ami-example:runs-on-repository=<owner>/<repo>`. Disable pools, sticky disks,
persistent workspaces, and custom provisioning hooks. Its test-runner role
must not have image-publishing permissions. The RunsOn configuration selects
the image, instance type, vCPU count, disk, and on-demand capacity. Job labels
select the runner, region, environment, and a unique routing key. Set the
custom image's owner and name pattern for your repository before dispatching.

Enable the repository variable `AMI_EXAMPLE_CLOUD_ENABLED=true` after the
[resource and cost review](docs/operations.md#cost-approval-and-execution-stages).
It enables cloud launches. Automatic orphan cleanup runs whenever the three
bootstrap variables are configured, including while launches are disabled.
Cleanup skips when all three variables are absent and rejects partial
configuration. Install the cleanup workflow and its protected environment
configuration on the default branch
before dispatching a build.

## Run the approved qualification

The image build workflow starts only through manual
dispatch. First choose `stage=stock` to prove the controller, tagged temporary
builder, SSH over Session Manager, source inventory, egress, and disposal. This
stage creates no candidate AMI or snapshot.

After the stock stage passes, choose `stage=single`, `retain=true`, and
`fault=none` for an approved single-build qualification. It builds one
candidate with compilation caches disabled. The candidate must pass the
direct boot probe before RunsOn can schedule its smoke jobs. Job A places a sentinel outside its checkout;
job B checks it is absent. Both begin by checking the exact running kernel and
baked identity, activate the image's `/usr/xenomai` development environment
for a later step, and build and run the Cobalt application's one CTest test.
The application starts and joins an Alchemy task and verifies that the task
runs in Cobalt primary mode; a stock kernel cannot satisfy it.

Each candidate job first grants the preconfigured `xenomai` group access to
the two Cobalt RTDM memory devices using the upstream `0660` mode. Identity
checks, compilation, and application execution then run as the ordinary runner
user.

To qualify an already retained candidate, dispatch **Qualify retained Cobalt
image** on `main` with the immutable versioned S3 URI of its candidate
`image-result.json`. It verifies the original build identity and configured
recipe, then runs the same probe and two fresh jobs without recompiling. New
instances and reports belong to the qualification dispatch; the image keeps
its original build identity and expiry.

Download the final qualified `image-result.json` and record the accepted image:

```sh
python3 scripts/accepted_image.py accept \
  --deployment .deployment/manifest.json \
  --result /path/to/final/image-result.json \
  --record .deployment/state/accepted-image.json
```

Promote the record to the private deployment state using
`scripts/deployment-state.py promote`, then manually dispatch **Run Cobalt
application**. Promotion verifies the live image and requires the previous
selection version, or an explicit empty initial selection. The workflow
freezes the accepted record for its run attempt. The application selects
`image=cobalt`, whose owner and name pattern resolve the newest matching AMI.
Admission requires that image to be the accepted one; qualify and promote each
rebuild before running the application. Runtime checks also verify the actual
AMI identity. It compiles and executes the Cobalt test,
retains its report, and terminates the runner. Admission rejects an expired image. The application job has read-only
repository access; separate control jobs use the controller's OIDC role.

```sh
gh workflow run run-cobalt-application.yml --ref main
```

For an independently approved reproducibility check, choose
`stage=qualification`, `retain=false`, and `fault=none`. It runs two clean
builds sequentially and requires both candidates to pass the same checks.

Each build publishes `image-result.json`. Final results use `status=qualified`
only after both fresh RunsOn jobs and controller verification pass; cleanup
must also pass before the comparison accepts them. The final `reproducibility-*`
artifact contains both result records and `reproducibility.json`. A comparison
reports every differing required field and separately reports variation in
raw initramfs container bytes. AMI IDs and disk snapshot bytes are outside the
comparison target.

Reports and downloaded Linux, Dovetail, Xenomai, and package inputs are retained
in the configured versioned S3 bucket. GitHub artifacts provide convenient
reports for 14 days; private deployment bundles and accepted-image selections
remain in S3.
By default, candidate instances, AMIs, and unreferenced snapshots are removed
after evidence is retained. `retain=true` keeps candidate images only until
the deployment's explicit expiry; instances still terminate.

## Operate and reuse

Scripts expose standalone commands with explicit arguments and JSON files.
Workflows supply execution IDs, coordinate commands and artifact transfers, and
write GitHub outputs and environment files. Image operations do not read workflow
files or GitHub execution context. Each command's `--help` lists its inputs;
the GitHub monitoring utilities additionally require an explicit repository,
run attempt, and job plan.

[Operations](docs/operations.md) owns cost estimates, parent/input refresh,
failure drills, deadlines, artifacts, and orphan cleanup.
[Infrastructure](infra/README.md) owns IAM trust, networking, reuse of existing
resources, and Terraform inputs. The
[implementation plan](docs/plans/custom-ami-example.md) records the accepted
architecture and acceptance criteria.

After live qualification passes, record the reviewed Git commit in any
companion project's source-copy attribution. Copy the orchestration, probe,
result, environment, comparison, and cleanup code from that revision under
the [MIT license](LICENSE). The pinned Linux and Xenomai sources retain their
upstream licenses.
