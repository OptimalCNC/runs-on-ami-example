# Run a published image on RunsOn

This module proves that a published AMI boots on EC2, registers as a RunsOn
runner, and builds and runs the [Cobalt application](cobalt). It
consumes the installation's `installation.yaml` and the image publisher's
`published-image.yaml`. The workflow selects the exact AMI in that publication
record, so its result identifies the image actually qualified.
Routing application jobs by a stable image name is a separate concern.

Image construction, local VM validation, publishing, and AMI retention belong to
the [image module](../images/README.md). RunsOn infrastructure and its GitHub App
belong to the [installation module](../runs-on/README.md).

## Prerequisites and contracts

Complete the installation's GitHub App setup, authorize this repository, and
pass its stock-image smoke test. Publish the Cobalt disk using the
image module and retain its `published-image.yaml`. The publication must be
available in the installation's account and region, with compatible bootstrap
and boot requirements. Its `artifact.manifest_sha256` binds the expected baked
manifest to the publication record.

GitHub accepts manual dispatches when the
[Build and test on Xenomai Cobalt workflow](../.github/workflows/run-cobalt.yml)
exists on the repository's default branch.
Authenticate the GitHub CLI with permission to dispatch and inspect workflows
in that repository. The workflow uses `contents: read`; its jobs receive no
publisher credentials and request no OIDC token.

From the repository checkout, install the local preparation command's Python
dependency:

```sh
cd execution
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Use Python 3.12. Local preparation and workflow dispatch need no AWS credentials.
Keep the two input records at explicit file paths; this module does not read
Terraform state or the publisher's local build disk.

Run the module's local checks with `python3 check.py`. To also compile the
application against an installed Cobalt SDK, use
`python3 check.py --xenomai-prefix /usr/xenomai`; add `--cobalt-runtime` to
execute its CTest test on a running Cobalt kernel.

## Prepare and dispatch

Generate the workflow inputs from the two records:

```sh
python3 prepare.py \
  --installation ../runs-on/.local/contracts/installation.yaml \
  --image ../images/.local/publication/published-image.yaml \
  --output .local/dispatch-inputs.json
```

Preparation checks the records and their compatibility, then writes only the
normalized fields required for execution. The resulting JSON freezes the
selected installation, AMI, recipe, and artifact identities for the dispatch.
It contains no AWS credentials or installation secrets.

Dispatch the reviewed workflow revision, replacing `OWNER/REPO` with the
repository authorized for the installed App:

```sh
gh workflow run run-cobalt.yml \
  --repo OWNER/REPO --ref main --json < .local/dispatch-inputs.json
```

The manually dispatched workflow first checks its inputs on a GitHub-hosted
runner. Its Cobalt job then requests an on-demand `t3.small` with 2 CPUs and a
16 GiB root disk, using the exact AMI, region, and RunsOn environment from those
inputs. The job has a 15-minute timeout after it starts. Runner compute, root
storage, and public IPv4 usage incur AWS charges while allocated.

On the EC2 runner, checks compare the actual AMI, account, region, and instance
profile with the input records. They verify UEFI boot, the baked manifest's
digest and recipe, the running kernel and configuration, the Cobalt SDK, and
the runner environment. The ordinary `runner` user then compiles
`execution/cobalt` and runs its CTest test. The test starts an Alchemy task and
requires Cobalt primary-mode execution; a stock Linux kernel cannot pass it.

Follow the run in GitHub's Actions page. Local preparation or static test
success establishes no EC2 or RunsOn execution result; qualification requires
the dispatched job to execute successfully on the published image.

## Evidence and lifecycle

The workflow retains two artifacts for 7 days:

| Artifact | Purpose |
| --- | --- |
| `cobalt-inputs-RUN_ID-ATTEMPT` | The normalized installation and image identities used for this execution |
| `cobalt-execution-RUN_ID-ATTEMPT` | `execution.yaml`, guest evidence, build/test logs, and JUnit output |

Rerunning only a failed Cobalt job reuses the successful preparation job's input
artifact. Its attempt suffix can therefore differ from the execution artifact's.

Download the artifacts from the completed run's Actions page. A qualified result
requires both a successful workflow and `execution.yaml` with `status: passed`,
including the passing Cobalt test. A queued job or missing execution report is
not qualification. Keep the inputs and result together to identify the GitHub
execution, source revision, EC2 instance, selected AMI, recipe, and tested guest.

The source disk SHA-256 in the publication and execution records is provenance
for the uploaded artifact. It is not a measurement of the running instance's
disk, which changes as the guest boots and executes jobs. The baked manifest's
digest and the observed EC2 image identity connect guest evidence to that
publication. Functional Cobalt success does not establish a latency guarantee.

Cancel a queued or running execution from GitHub's Actions page or the CLI:

```sh
gh run cancel RUN_ID --repo OWNER/REPO
```

The job timeout starts after a runner accepts the job; it does not bound time
spent queued. RunsOn owns runner termination and applies the installation's
maximum runner lifetime. Workflow success and cancellation do not themselves
verify that the EC2 instance has terminated. If termination readback is needed,
use the instance ID from the execution evidence with an authorized operator
profile:

```sh
aws ec2 describe-instances \
  --profile your-operator-profile --region REGION \
  --instance-ids INSTANCE_ID \
  --query 'Reservations[].Instances[].State.Name' --output text
```

The published AMI remains available after execution. Retire it and its snapshot
using [image publication cleanup](../images/README.md#outputs-and-cleanup) when
no longer needed. Local dispatch inputs and downloaded execution evidence can
be retained independently of those AWS resources.
