# Operating the Ubuntu 24.04 Xenomai Cobalt example

This document owns the transition from local implementation to an approved,
qualified AWS deployment, and the operation of its disposable resources.
The image payload is a Dovetail-enabled Linux kernel with Xenomai 3 Cobalt
integration and matching userspace development tools in `/usr/xenomai`.
Local checks cannot establish EC2 bootability, SSM networking, RunsOn
registration, IAM correctness in a particular account, or reproducibility of
compiled payloads. Those claims require the live stages below.

## Cost approval and execution stages

Keep resource inventories, approvals, regional estimates, retention deadlines,
and cleanup ownership in the deployment's private `state/` and `runs/`
records. Generate observed infrastructure identities from Terraform and
CloudFormation outputs. Record each execution's evidence and cleanup result
without editing reusable source files.

Before any billable action, review the actual account, region, exact instance
type, parent AMI size, existing network path, retention, and expected duration.
Present the resulting Terraform plan and resource/cost list to the maintainer
for approval. An initial approval for local code or a read-only plan does not
authorize Terraform apply, AMI copies, probes, or workflow dispatches.

| Stage | Resources to approve | Expected lifetime and cleanup |
| --- | --- | --- |
| Supporting infrastructure | IAM/OIDC roles, SSM profiles, no-ingress security group, versioned S3 bucket; reuse existing equivalents where selected | Persistent until explicit teardown; IAM and security groups have no direct usage charge, S3 data/requests do |
| Parent inventory | Launch directly from pinned public RunsOn AMIs; one short EC2 inventory probe, or two if the controller differs | Probes terminate in `finally`, delete their root volumes, and are tagged for orphan sweeping; no parent copy or new snapshot is needed |
| Stock qualification | One RunsOn controller plus one temporary Packer builder, each with a root EBS volume | Terminated when the job completes; no candidate AMI is created |
| Single-build qualification | One controller, one builder, one direct probe, two fresh RunsOn smoke instances, and one candidate AMI/snapshot set | Instances terminate after testing; candidate retention follows the explicit deployment policy |
| Retained-image qualification | One direct probe and two fresh RunsOn smoke instances from an existing retained candidate | Verifies the immutable candidate record before launching; terminates new instances and preserves the original image expiry |
| Accepted-image application | One fresh RunsOn instance from the frozen accepted-image selection | Compiles and runs the Cobalt test, retains evidence, then terminates; does not extend image retention |
| Full qualification | Two sequential repetitions, each with one controller, one builder, one direct probe, two separate RunsOn smoke instances, and one candidate AMI/snapshot set | Probe and tests are short; candidates deleted after reports unless explicitly retained |
| Failure drills | One controlled probe failure and one manually interrupted build, in separate approved dispatches | Verify diagnostic retention and both independent cleanup paths |

A preliminary planning allowance is **US$5–15 for a two-build qualification**
when an existing RunsOn installation/network is available and each build
finishes within a few hours. This is an illustrative allowance, not a current
regional price quote or a spending cap. Recalculate before approval using
[AWS Pricing Calculator](https://calculator.aws/) and the repository's GitHub
Actions plan:

```text
EC2 = sum(instance hours) × the selected instance's on-demand hourly price
EBS = sum(provisioned GiB × attached hours / monthly billing hours) × gp3 price
Snapshots = retained used GiB × retained fraction of a month × snapshot price
Artifacts = retained S3 GiB, object requests, versions, and GitHub artifact storage
Other = public IPv4 hours, existing NAT processing/egress, and applicable GitHub runner minutes
```

The independent GitHub-hosted watchdog remains running while the image builds;
include its minutes, especially for private repositories. Any optionally
retained parent snapshots and S3 inputs continue to cost after candidate cleanup. RunsOn
installation/licensing and any new NAT gateway or VPC endpoint need a separate
resource plan if no suitable installation/network exists; this Terraform root
does not create them. Independent manual dispatches can overlap and multiply
the estimate. They never automatically cancel an older build.

For another execution, use a new manual dispatch or re-run all jobs. Partial
reruns are rejected when earlier job outputs refer to another execution, and
comparison artifacts are selected for the current run attempt only.

Enable `AMI_EXAMPLE_CLOUD_ENABLED=true` only after approving the chosen scope.
Keep the protected GitHub environment restricted to reviewed branches. The
hourly cleanup schedule relies on that environment being able to run
unattended after the initial approval. Requiring a new manual reviewer for
every cleanup job would prevent it from enforcing the intended expiry.

## Lock the parent images

Resolve exact regional stock RunsOn AMI IDs and owners manually. The build has
no `most_recent` lookup. Use the pinned public image directly, record its
exact AMI boot mode (`uefi`, `uefi-preferred`, or `legacy-bios`), and ensure
Secure Boot is disabled. A `uefi-preferred` parent requires a UEFI-capable
qualified instance type; the controller verifies that every actual boot uses
UEFI, including the separately pinned controller.
Use the same account's AWS-managed EBS key for encrypted builder/probe volumes
and candidate snapshots. A scheduled future deprecation date is accepted;
an already deprecated image is rejected. A public parent can be withdrawn by
its publisher. If longer-term parent availability is needed, obtain separate
approval for a copy and retain it without disposable build ownership tags.

The parent must already contain the GitHub runner at
`/home/runner/bin/Runner.Listener`, its clean unregistered runner home, SSH,
the SSM agent, and `/usr/local/bin/runs-on-bootstrap-[v]<version>` matching
`runs_on.bootstrap_version`. Record that bootstrap version separately from
the service's `runs_on.version`; they have independent version numbers.
GitHub's agent freshness requirements still apply;
refresh the reviewed parent and lock together when needed. The image input lock
selects an immutable Ubuntu snapshot. Choose a compatible parent whose
packages do not require downgrades; installations refuse downgrades and
any unreviewed package-state change.

After provisioning or importing the foundation, RunsOn installation, and
management access, resolve their actual identities into `bindings.json`.
Inventory capture takes bindings and a parent selection directly; it does not
require the final manifest. Generate a selection from the specification; it
contains the exact parent `id` and `owner`.

```sh
python3 scripts/install-tools.py --group cloud
export PATH="$PWD/.tools/bin:$PATH"
python3 scripts/deployment-config.py selection \
  --spec .deployment/spec.json --parent source \
  --output .deployment/source-selection.json
python3 scripts/probe-ami.py --capture-inventory \
  --bindings .deployment/bindings.json \
  --selection .deployment/source-selection.json \
  --build-id 123456789-1-stock \
  --output .deployment/parents/source --execute
```

Use controller credentials with an `ami-example-` role-session name. Review
the selected parent disk size, probe instance type, network path, and expected
duration before launching. Direct burstable probes request Standard CPU
credits. Calculate compute, root-volume, public-IPv4, and report-storage costs
for the actual region using the rates reviewed for this deployment.

Capture writes a `parent.json` identity and `inventory.json` evidence into
the requested output directory. Use a unique build ID for every capture.
Generate the controller selection with `--parent controller` and capture it
into a separate directory, or reuse the same parent record when source and
controller select the same clean image. A registered controller's runtime
workspace is not clean-parent evidence.

The inventory contains package versions, runner version and binary digest,
bootstrap paths and digests, and registration, workspace, and Secure Boot
checks. Resolve the deployment manifest only after these records exist;
resolution verifies their hashes and selected identities. The manifest uses
relative references to the captured inventories. Keep those files together
when moving a deployment; publishing snapshots the complete input bundle.
For separate source/controller records, resolve and validate it before
performing read-only cloud admission:

```sh
python3 scripts/deployment-config.py manifest \
  --bindings .deployment/bindings.json \
  --source-parent .deployment/parents/source/parent.json \
  --controller-parent .deployment/parents/controller/parent.json \
  --output .deployment/manifest.json
python3 scripts/validate-inputs.py --deployment .deployment/manifest.json
python3 scripts/preflight.py --deployment .deployment/manifest.json \
  --output .deployment/runs/preflight.json
```

AMI state, owners, architecture, boot mode, encryption key access, instance
type/vCPUs, VPC/security group, profiles, and artifact-bucket ownership are
checked before launches. SSM/HTTPS connectivity and actual launch authority
are then established by the approved stock stage.

## Refresh immutable inputs

Input refresh is a separate reviewed change. Recipe locks belong to source;
operator choices and resolved parent evidence belong to the deployment. Update the compatible Linux,
Dovetail, and Xenomai source pins and checksums together, plus Ubuntu
snapshot/package versions/checksums, tools, and workflow release tags in
source. Refresh selected parent identities and inventories in the deployment
records as needed. Use upstream checksum
metadata and the signed Ubuntu package indexes; do not resolve new values
during an image build. Package indexes and every downloaded `.deb`, including
dependencies, are retained with the source archives in `inputs.tar` in S3.
Dependencies are selected from that single signed immutable snapshot against
the pinned parent's package state. Third-party apt sources are removed before
installation. Inherited snap files are hashed in the parent and payload
inventories, and snap refresh is held before provisioning and remains held in
the candidate. No unconstrained system upgrade runs.

[`images/xenomai-cobalt/inputs.lock.json`](../images/xenomai-cobalt/inputs.lock.json)
owns the exact source versions. The Linux source archive already includes
Dovetail. The build integrates the matching Xenomai sources using
`prepare-kernel.sh` and builds Cobalt userspace for `/usr/xenomai`.

`kernel.config` is a full resolved configuration for the prepared kernel,
with Dovetail, Cobalt, ENA, NVMe, filesystem, EFI, container, and embedded-config
support. The builder resolves and synchronizes Kconfig using the pinned
compiler, then compares every effective setting with the committed file. Any
generated change stops the build. Optional Rust/bindgen and BTF tooling is
disabled while configuring this C-only kernel, so unrelated installed tools
cannot change the configuration. Update `kernel.config` and its lock digest
together during refresh.

Kernel timestamps, user/host, build number and debug paths are fixed; automatic
local-version generation and module signing are disabled. No private signing
key is created or committed. The compiled release comes from `make
kernelrelease` and uses the `-xenomai-cobalt` suffix. The bootloader selects
that exact release; retaining a stock
fallback kernel cannot satisfy the boot checks.

The recipe identity hashes the input lock, the explicitly listed recipe
files, and guest-content/boot-affecting deployment fields. Run IDs, retention,
timestamps and candidate resource IDs remain external. Compiler and binutils
versions, effective configuration, kernel/module and Xenomai library/header/tool
payload hashes, package inventory, boot configuration, and unpacked initramfs
file content/modes are compared across the two clean builds. Raw initramfs
container variation is reported separately;
disk snapshot bytes are not compared. Investigate every required-field
difference before claiming payload reproducibility. The runner acceptance
test builds the small Cobalt application and proves it can execute using the
Cobalt kernel. It does not measure latency.

S3 source retention is finite and configured through
`artifact_retention_days` (365 by default) for the `reports/` prefix.
The separate `state/` prefix is retained for deployment selection and recovery;
retire its old versions explicitly after all dependent resources are gone. Choose any optional parent-copy
retention and preserved source/package retention to cover the required
reproduction period. If
the upstream snapshot service becomes unavailable, its preserved signed
indexes and packages are the material needed to restore the same repository;
such a restore is an explicit operations task, not an automatic floating
fallback in the build.

## Deadlines and failure drills

The deployment defaults to a 15-minute direct-boot deadline, a 15-minute
launch/registration deadline per runnable controller/smoke job, and a 4-hour
independent deadline per candidate. Registration time starts only after that
job's prerequisites succeed. Packer has a 90-minute subprocess bound; its job
has a 105-minute timeout. Retained-image qualification has a 45-minute
independent deadline inside a 60-minute watchdog job. These are runtime limits,
not hard AWS billing caps.

The Packer bound covers preparation, compilation, input transfer, and AWS
snapshot availability. Size the builder and choose deadlines for the selected
parent, image payload, and regional conditions.

The independent watchdog reports job failures and deadline violations. Its
workflow calls GitHub's cancellation API when monitoring fails or a deadline
expires; an ordinary upstream job failure leaves finalization running.
A job that never acquires a runner does not need to run a timeout handler.
The default-branch `workflow_run` cleanup reacts when the cancelled workflow
completes, and an hourly sweep removes expired example-owned orphans if a
controller or finalizer disappeared. GitHub scheduling delays, credential
outages or disabled workflows can delay that sweep; keep an operational
owner able to run the same cleanup command directly.

After normal qualification, approve and run both drills:

1. Dispatch `stage=qualification`, `fault=probe-identity`, `retain=false`.
   The controller deliberately expects the wrong kernel in the direct probe.
   The probe must fail, terminate, retain console/initialization evidence, and
   prevent either candidate smoke job from running. Finalization must dispose
   of owned candidates and snapshots.
2. Dispatch a fresh build and cancel it while its builder is active. Confirm
   the separate cleanup workflow runs from the default branch, the temporary
   builder/probe/test instances reach `terminated`, and no disposable AMI,
   detached volume, snapshot or key pair remains for that run. Verify the
   hourly sweep too, or invoke its expired-resource path after a deliberately
   short test expiry in a reviewed deployment change.

Record exact execution IDs and evidence for success, controlled failure,
and interruption in the deployment's private records. Report which drills
were actually completed for that deployment.

## Artifacts and cleanup

The result schema is [image-result.schema.json](../schemas/image-result.schema.json).
Packer emits a creation record with `status=candidate`; controller verification
produces the qualification record. Reproducibility is a separate validation
field populated only after both builds finish. The S3 layout is:

```text
<repository>/reports/<build-id>/          Packer logs, inputs, creation record
<repository>/reports/<build-id>/probe/    Direct boot observations and diagnostics
<repository>/reports/ssm/<build-id>/      SSM stdout/stderr and initialization logs
<repository>/reports/<build-id>/final/    Qualification and cleanup results
<repository>/reports/comparisons/         Reproducibility results
<repository>/reports/cleanup/             Live inventories and cleanup diagnostics
<repository>/state/configuration/current.tar Current deployment bundle
<repository>/state/acceptance/current.json Current accepted-image selection
<repository>/state/executions/<run>/<attempt>/ Saved execution context
```

The build and final `artifact-index.json` files record exact S3 object versions
for their reports. Versioned locations include `?versionId=...`; retrieve a
specific version with `aws s3api get-object --bucket BUCKET --key KEY
--version-id VERSION LOCAL_FILE`, using the decoded version ID from the index.
The input archive's versioned location is also in the image result.

Review cleanup using the saved cleanup context. It contains the owning
repository, account, region, and artifact bucket and remains usable without
parent inventories or an accepted image. A workflow saves this context when
it fetches the deployment bundle. Standalone cleanup can take the bindings
file, from which it reads only these four fields, or a saved minimal context.
The following commands only review resources:

```sh
python3 scripts/cleanup.py --cleanup-context .deployment/bindings.json \
  --run-id 123456789 --output .deployment/runs/cleanup-review
python3 scripts/cleanup.py --cleanup-context .deployment/bindings.json \
  --expired --output .deployment/runs/expiry-review
```

Apply cleanup to that explicit scope after its resource list is understood:

```sh
python3 scripts/cleanup.py --cleanup-context .deployment/bindings.json \
  --run-id 123456789 --apply --output .deployment/runs/cleanup
python3 scripts/cleanup.py --cleanup-context .deployment/bindings.json \
  --expired --apply --output .deployment/runs/expiry
```

Cleanup verifies account identity and live owner/build/purpose tags, retains
resource and console diagnostics, terminates compute, and verifies
termination. AMIs and unreferenced snapshots are deleted only after durable
diagnostic retention succeeds. Compute is still terminated if S3 is
unavailable; image/snapshot evidence is preserved for recovery. Snapshots
referenced by any remaining account AMI are never deleted. Missing or malformed
expiry tags are reported through inventory review rather than guessed.

Each workflow freezes the deployment bundle's exact S3 version and SHA-256
before launching runners, then saves an explicit execution record. An
application run also freezes the accepted-image record's exact version and
digest. Later jobs fetch those exact objects; publishing a new deployment or
promoting a new image does not change an existing run attempt.

Bundles contain the manifest and parent evidence and remain in the private,
versioned S3 state prefix. Current deployment and accepted-image pointers are
operator state, not public workflow artifacts. Promotion verifies the live
AMI against the supplied qualification evidence and requires the previous
accepted-selection version, or an explicit empty initial selection, before a
conditional update.

After creating an accepted-image record from a qualified result, promote the
initial selection with that exact qualification file:

```sh
python3 scripts/deployment-state.py promote \
  --record .deployment/state/accepted-image.json \
  --qualification /path/to/final/image-result.json \
  --deployment .deployment/manifest.json \
  --state-root s3://example-artifacts/example/image-runners/state \
  --account-id 123456789012 --region us-east-1 \
  --repository example/image-runners --empty \
  --output .deployment/state/promotion.json
```

For replacement, pass `--previous-version VERSION` from the reviewed current
selection instead of `--empty`. Substitute this deployment's actual identity
and state prefix for the neutral example values. Promotion rejects concurrent
selection changes; refresh and review the current selection before retrying.

Completed-run recovery uses the saved context and the exact attempt's GitHub
jobs. Historical cleanup retains the original resource ownership and runtime
selection when current deployment settings change. Keep previous state
objects, required permissions, and cleanup access for every live execution;
after rotating account, region, role, or state prefix, operate the old scope
through its saved context until its resources have been cleared.

Test instances initially receive the RunsOn common ownership marker. The
monitor and application verifier add the build's owner, build ID, purpose and
expiry only after matching the explicitly selected instance, an owned candidate
AMI, and its creation time. Instances without
that required marker are preserved and reported as a configuration failure.
Cleanup never removes the RunsOn service, retained source AMIs, or supporting
Terraform infrastructure. Teardown of those resources is a separate reviewed
action.
