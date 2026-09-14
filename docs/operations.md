# Operating the Ubuntu 24.04 Xenomai Cobalt example

This document owns the transition from local implementation to an approved,
qualified AWS deployment, and the operation of its disposable resources.
The image payload is a Dovetail-enabled Linux kernel with Xenomai 3 Cobalt
integration and matching userspace development tools in `/usr/xenomai`.
Local checks cannot establish EC2 bootability, SSM networking, RunsOn
registration, IAM correctness in a particular account, or reproducibility of
compiled payloads. Those claims require the live stages below.

## Cost approval and execution stages

The selected account's [resource inventory](../infra/resources.json) records
the current resource IDs, the approved trial scope and regional estimates,
retention deadlines, and cleanup ownership. Update its observed resources
and workflow IDs after each cloud stage.

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
| Single-build qualification | One controller, one builder, one direct probe, two fresh RunsOn smoke instances, and one candidate AMI/snapshot set | Instances terminate after testing; the approved trial retains the candidate for 24 hours |
| Retained-image qualification | One direct probe and two fresh RunsOn smoke instances from an existing retained candidate | Verifies the immutable candidate record before launching; terminates new instances and preserves the original image expiry |
| Accepted-image application | One fresh RunsOn instance from the committed accepted AMI | Compiles and runs the Cobalt test, retains evidence, then terminates; does not extend image retention |
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
refresh the reviewed parent and lock together when needed. The current build
uses Ubuntu's immutable `20260911T000000Z` snapshot. Choose a compatible parent
whose packages do not require downgrades; installations refuse downgrades and
any unreviewed package-state change.

After the supporting infrastructure and probe costs are approved, fill the
actual deployment fields. RunsOn need not be installed for this direct probe:
leave `runs_on` as `null` until the actual environment and version are known.
During initial inventory capture only, the two
inventory hashes may temporarily be 64 zeroes; the normal build rejects them
because they will not match the inventory files. Run with credentials for the
intended controller role and an `ami-example-` role-session name:

```sh
python3 scripts/install-tools.py --group cloud
export PATH="$PWD/.tools/bin:$PATH"
python3 scripts/probe-ami.py --capture-inventory source --build-id 1789171200-1-stock --execute
```

The initial inventory uses `instance_type=t3.small`, `vcpus=2`, and
`parent_root_volume_gib=30` for the selected 30 GiB public parent. Direct
burstable probes request Standard CPU credits. In `us-east-1`, prices checked
on 2026-09-13 give approximately $0.0073 for 15 minutes of compute, a baseline
gp3 root volume, and one public IPv4 address; report storage and requests are
additional. Retain those reports for the approved period, then remove all
their S3 versions under the repository prefix. This estimate authorizes no
additional probes, builds, or RunsOn deployment.

Use a unique numeric build ID for every capture. The full JSON inventory is in
`artifacts/<build-id>/probe/parent-inventory.json`, with initialization logs in
the bucket's `<repository>/ssm/<build-id>/` prefix. Copy the inventory to
`infra/source-inventory.json`. Capture the controller with
`--capture-inventory controller` and another build ID, or copy the inventory
when both AMIs are the same clean image. A registered controller's runtime
workspace and registration state are intentionally not the clean-parent
inventory source.

Calculate each file's SHA-256 with `sha256sum`, put it in the corresponding
deployment identity, and commit the deployment and both inventory files. The
inventory includes package versions, the runner version and binary digest,
bootstrap paths/digests, and checks for registration, workspaces and Secure
Boot. Validate the configuration locally, then perform the read-only cloud
admission check:

```sh
python3 scripts/validate-inputs.py --deployment infra/deployment.json
python3 scripts/preflight.py
```

AMI state, owners, architecture, boot mode, encryption key access, instance
type/vCPUs, VPC/security group, profiles, and artifact-bucket ownership are
checked before launches. SSM/HTTPS connectivity and actual launch authority
are then established by the approved stock stage.

## Refresh immutable inputs

Input refresh is a separate reviewed change. Update the compatible Linux,
Dovetail, and Xenomai source pins and checksums together, plus Ubuntu
snapshot/package versions/checksums, tools, action SHAs, parent identities,
and inventory files as needed. Use upstream checksum
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
`artifact_retention_days` (365 by default). Choose any optional parent-copy
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

The live Cobalt trial compiled the kernel in about 20 minutes on `c7i.large`,
then needed over 30 minutes for AWS snapshot preparation. The Packer bound
covers preparation, compilation, input transfer, and snapshot availability.

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

Record the exact workflow IDs and evidence for success, controlled failure,
and interruption. These live drills have not yet been executed for this
repository.

## Artifacts and cleanup

The result schema is [image-result.schema.json](../schemas/image-result.schema.json).
Packer emits a creation record with `status=candidate`; controller verification
produces the qualification record. Reproducibility is a separate validation
field populated only after both builds finish. The S3 layout is:

```text
<repository>/<build-id>/                 Packer logs, input archive, creation record
<repository>/<build-id>/probe/           Direct boot observations and EC2 diagnostics
<repository>/ssm/<build-id>/             Complete SSM stdout/stderr and initialization logs
<repository>/<build-id>/watchdog.json    Independent job/resource observations
<repository>/<build-id>/final/          Qualification and cleanup results
<repository>/executions/<run>/<attempt>/ Explicit job plans and selected image records
<repository>/comparisons/<run-attempt>/ Reproducibility results
<repository>/cleanup/                   Live resource inventories and cleanup diagnostics
```

The build and final `artifact-index.json` files record exact S3 object versions
for their reports. Versioned locations include `?versionId=...`; retrieve a
specific version with `aws s3api get-object --bucket BUCKET --key KEY
--version-id VERSION LOCAL_FILE`, using the decoded version ID from the index.
The input archive's versioned location is also in the image result.

Review cleanup without mutations:

```sh
python3 scripts/cleanup.py --run-id 123456789
python3 scripts/cleanup.py --expired
```

Apply cleanup to that explicit scope after its resource list is understood:

```sh
python3 scripts/cleanup.py --run-id 123456789 --apply
python3 scripts/cleanup.py --expired --apply
```

Cleanup verifies account identity and live owner/build/purpose tags, retains
resource and console diagnostics, terminates compute, and verifies
termination. AMIs and unreferenced snapshots are deleted only after durable
diagnostic retention succeeds. Compute is still terminated if S3 is
unavailable; image/snapshot evidence is preserved for recovery. Snapshots
referenced by any remaining account AMI are never deleted. Missing or malformed
expiry tags are reported through inventory review rather than guessed.

Each workflow saves its explicit execution record before launching runners.
Completed-run recovery reads that record and the exact attempt's GitHub jobs;
it does not inspect workflow files or retrieve configuration by commit.

Test instances initially receive the RunsOn common ownership marker. The
monitor and application verifier add the build's owner, build ID, purpose and
expiry only after matching the explicitly selected instance, an owned candidate
AMI, and its creation time. Instances without
that required marker are preserved and reported as a configuration failure.
Cleanup never removes the RunsOn service, retained source AMIs, or supporting
Terraform infrastructure. Teardown of those resources is a separate reviewed
action.
