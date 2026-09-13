# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

This example builds an Ubuntu 24.04 AMI with Xenomai 3 Cobalt and uses it as a
RunsOn Flex runner. A temporary Packer builder compiles a Dovetail-enabled
Linux kernel with Cobalt integration and matching Xenomai userspace tools,
libraries, and headers. Fresh RunsOn jobs then build and run a small Cobalt
application that requires the running Cobalt kernel.

A single-build dispatch runs an independent EC2 boot probe and two fresh
RunsOn jobs, then retains the accepted AMI for a separate application dispatch.
The optional two-build qualification compares the installed
kernel, modules, Xenomai payload, configuration, packages, and unpacked
initramfs content. The application test checks functional execution; there is
no latency target.

**Status: scoped IAM and stock RunsOn qualification passed in AWS. The Cobalt
kernel and SDK compiled, and the custom AMI is available. Its boot and RunsOn
qualification are pending. Retries are authorized within the recorded $20
ceiling in [infra/resources.json](infra/resources.json).** An AMI creation
record is not a qualification result.

The image build controller uses a separately pinned stock RunsOn AMI. It is
never snapshotted. The example targets Ubuntu 24.04, x86-64, one exact Nitro
runtime type, on-demand capacity, and an unsigned kernel with Secure Boot
disabled. Parent inventory starts with `t3.small`, 2 vCPUs, 2 GiB RAM, and a
30 GiB root volume. Kernel-builder sizing is configured separately; candidate
probes and smoke runners use the built AMI's configured root size. The runner installation is supplied by an existing
[RunsOn Flex installation](https://runs-on.com/docs/); Fleet needs a separate
adapter.

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

Start with [infra/deployment.example.json](infra/deployment.example.json) and
the [infrastructure guide](infra/README.md). Every `null` is a required
qualification input; `runs_on` may remain `null` during direct parent inventory.
Account IDs, AMIs, networking, IAM roles, inventory files,
and the RunsOn version are deployment settings; scripts do not discover them
from a floating image query.

Before cloud work, provide:

1. The GitHub repository and protected execution environment, normally
   `ami-build`, and the branches allowed to use it.
2. The AWS account and region, an existing RunsOn **Flex** installation and its
   exact version/environment, and its repository registration.
3. An existing VPC/subnet with working HTTPS egress, plus the selected stock
   Ubuntu 24.04 x86-64 RunsOn AMI. Pin its public AMI ID and publisher directly,
   and choose a controller image independently; both may use that same image.
4. Approval for the specific resources and estimated costs described in
   [operations](docs/operations.md#cost-approval-and-execution-stages).

The existing RunsOn environment must propagate the common EC2 tag
`ami-example:runs-on-repository=<owner>/<repo>`. Disable pools, sticky disks,
persistent workspaces, and custom provisioning hooks for this environment.
Its test-runner role must not have image-publishing permissions. The exact job
labels select AMI, region, environment, instance type, vCPU count, on-demand
capacity, and a unique routing key for each job.

Once approved, follow [parent inventory capture](docs/operations.md#lock-the-parent-images)
and commit `infra/deployment.json`, `infra/source-inventory.json`, and
`infra/controller-inventory.json`. They contain configuration and provenance,
not credentials. Run:

```sh
python3 scripts/validate-inputs.py --deployment infra/deployment.json
```

Use GitHub OIDC for execution credentials. Configure the protected environment
and its branch restrictions before enabling the repository variable
`AMI_EXAMPLE_CLOUD_ENABLED=true`. Leave that variable unset until cloud costs
and the deployment have been approved. It also enables automatic orphan
cleanup; the cleanup workflow and deployment configuration must be on the
default branch before a build is dispatched.

## Run the approved qualification

The image build workflow starts only through manual
dispatch. First choose `stage=stock` to prove the controller, tagged temporary
builder, SSH over Session Manager, source inventory, egress, and disposal. This
stage creates no candidate AMI or snapshot.

After the stock stage passes, choose `stage=single`, `retain=true`, and
`fault=none` for the approved trial recorded in
[infra/resources.json](infra/resources.json). It builds one candidate with
compilation caches disabled. The candidate must pass the direct boot probe before RunsOn
can schedule its smoke jobs. Job A places a sentinel outside its checkout;
job B checks it is absent. Both begin by checking the exact running kernel and
baked identity, activate the image's `/usr/xenomai` development environment
for a later step, and build and run the Cobalt application's one CTest test.
The application starts and joins an Alchemy task and verifies that the task
runs in Cobalt primary mode; a stock kernel cannot satisfy it.

To qualify an already retained candidate, dispatch **Qualify retained Cobalt
image** on `main` with the immutable versioned S3 URI of its candidate
`image-result.json`. It verifies the original build identity and configured
recipe, then runs the same probe and two fresh jobs without recompiling. New
instances and reports belong to the qualification dispatch; the image keeps
its original build identity and expiry.

Download the final qualified `image-result.json` and record the accepted image:

```sh
python3 scripts/accepted_image.py accept --result /path/to/final/image-result.json
```

Commit `accepted-image.json` to `main`, then manually dispatch **Run Cobalt
application**. It launches one fresh runner from that exact retained AMI,
compiles and executes the Cobalt test, retains its report, and terminates the
runner. Admission rejects an expired image. The application job has read-only
repository access; separate control jobs use the controller's OIDC role.

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
reports for 14 days.
By default, candidate instances, AMIs, and unreferenced snapshots are removed
after evidence is retained. `retain=true` keeps candidate images only until
the deployment's explicit expiry; instances still terminate.

## Operate and reuse

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
