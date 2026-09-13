# Plan 1 — Reproducible Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn

**Status:** Migrated implementation plan; the image build and cloud acceptance tests have not been executed.  
**Intended home:** A standalone example repository, independent of MetaNC and Orocos.  
**Suggested checked-in path:** `docs/plans/custom-ami-example.md`  
**Date:** 2026-09-12  
**Companion:** `02-xenomai-development-images-plan.md`, which can adopt the qualified image recipe and infrastructure established here.

## 1. Objective and boundary

Build a small, reproducible example demonstrating this complete path:

> A manually dispatched GitHub Actions workflow uses RunsOn to host an image-build controller, creates an Ubuntu 24.04 AMI containing Xenomai 3 Cobalt, and uses fresh RunsOn instances of that exact AMI to build and execute a small application requiring the Cobalt kernel.

The deliverable is a working example repository, not an AMI tied to one person's account. Another maintainer must be able to supply their own deployment configuration, reproduce the build, and obtain the same platform behavior.

The sole image payload is a compatible, pinned combination of Linux, Dovetail, and Xenomai 3 Cobalt kernel integration and userspace. The application establishes functional Cobalt execution without a latency requirement. The reusable boundary is image construction, fresh runner qualification, and resource cleanup.

## 2. Architecture and execution model

```text
Manual workflow_dispatch
          |
          v
Resolve and validate the checked-in inputs
          |
          v
RunsOn controller on a pinned, known-good stock AMI
          |
          | invokes Packer amazon-ebs
          v
Independent temporary EC2 builder
  Build/install Dovetail + Cobalt kernel and userspace; clean image
          |
          v
Candidate AMI + immutable result manifest
          |
          v
Externally controlled fresh EC2 boot probe
          |
          v
RunsOn smoke job A on the exact candidate AMI
          |
          v
RunsOn smoke job B on another fresh instance of the same AMI
          |
          v
Controller-side verification, reports, and cleanup
```

Keep three roles distinct: the **controller** runs Packer, the **builder** is the temporary machine Packer snapshots, and the **test runners** boot the finished candidate. Never snapshot the controller or a registered GitHub runner. The controller must remain launchable without the candidate image.

RunsOn documents deriving custom images from its base AMIs with Packer. Use that integration rather than building a runner bootstrap from scratch.[^runs-on-ami] Packer provides the `amazon-ebs` builder for the image-assembly step.[^packer]

For the example, target **RunsOn Flex**, whose per-job labels can select an exact `ami=` value. Fleet uses platform-owned runner definitions instead; do not claim the Flex example is portable to Fleet without a separate adapter.[^runs-on-labels]

Use a pinned public stock RunsOn Ubuntu 24.04 parent, one AWS account, one region, x86-64, and one exact on-demand EC2 runtime type for initial qualification. Start parent inventory with `t3.small` (2 vCPUs, 2 GiB RAM), Standard CPU credits, and a 30 GiB gp3 root volume for the selected 30 GiB parent. Configure the Packer builder type separately; candidate probes and smoke runners must accommodate the built AMI's root volume. A retained parent copy is optional and requires separate cost approval. Make these selections checked-in deployment settings. Disable warm pools, persistent volumes, and reusable workspaces for the freshness tests. Qualify the Cobalt image on the exact runtime type before claiming support.

## 3. Repository deliverables

```text
README.md
LICENSE
docs/plans/custom-ami-example.md
docs/operations.md
.github/workflows/build-and-test-image.yml
.github/workflows/validate.yml
images/xenomai-cobalt/
  inputs.lock.json
  kernel.config
  image.pkr.hcl
  build-kernel.sh
  provision.sh
  smoke.sh
images/common/
  packer-user-data.sh
  finalize-image.sh
  runner-image-env
scripts/
  validate-inputs.py
  probe-ami.py
  verify-run-results.py
  cleanup.py
  compare-builds.py
schemas/
  image-result.schema.json
infra/
  README.md
  main.tf
  variables.tf
  outputs.tf
  deployment.example.json
tests/cobalt/
  CMakeLists.txt
  main.c
```

The infrastructure files provision the example-specific roles, management access, and artifact/log destinations, or accept existing equivalents. They reference an existing RunsOn installation rather than implementing another runner service. Keep account IDs, subnet IDs, and role ARNs in deployment configuration, not in generic scripts. Select a license so the common implementation can be reused by the companion project.

Keep shell and Python logic in scripts; workflows should express triggers, permissions, dependencies, and artifact handoff. The validation workflow may run credential-free checks on pull requests. **Only manual dispatch starts image builds.**

## 4. Reproducibility contract

### Locked inputs

The build must consume, rather than discover, the following:

| Input | Required representation |
| --- | --- |
| Source and controller AMIs | Exact regional IDs, verified owners, architecture, boot mode, and source inventory |
| Linux with Dovetail and Xenomai sources | Compatible exact versions and commits, archive URLs, and SHA-256 values; preserve downloadable inputs |
| Kernel configuration | Full committed configuration and checked effective Dovetail/Cobalt configuration |
| Build/provisioning tools | Exact Packer/plugin/tool versions and verified downloads; compiler and binutils versions |
| OS additions | Fixed package-repository snapshot and explicit package selection; lock other repositories separately |
| Recipe | Exact Git commit plus hashes of the files that actually affect image content |
| Runner integration | Inherited agent/bootstrap versions and the RunsOn deployment version used for qualification |
| GitHub Actions | Reviewed full commit SHAs in executable workflows |

A pinned parent freezes its inherited package state; later installations must also be constrained. Ubuntu provides date-addressed package snapshots, but unrelated package repositories require their own locking or mirrored inputs.[^ubuntu-snapshot]

Separate input refresh from image building. No `most_recent = true`, floating branch checkout, unversioned installer, or unconstrained system upgrade belongs in the locked build path. A manual update may resolve newer inputs, but those values must be recorded and reviewed before the image build consumes them.

### What is compared

Use two distinct identifiers:

```text
recipe_id = hash(canonical content-affecting inputs and recipe files)
build_id  = identifier of this particular workflow execution
```

Include deployment settings in `recipe_id` when they change guest content or boot behavior. Keep run IDs, AMI IDs, snapshot IDs, and creation timestamps in the external result record. A unique AMI name may include `build_id`; the kernel release and baked recipe identity must not vary merely because a workflow was rerun.

The required guarantee is repeatable construction from immutable inputs, equivalent installed platform content, and successful cold-boot behavior. AMI IDs and disk snapshot bytes are not the reproducibility comparison target.

Normalize kernel timestamps, build user/host, local-version generation, and debug paths. Define a deliberate module-signing policy instead of generating new keys invisibly. Linux documents these sources of nondeterminism.[^kernel-reproducible] Never commit private signing keys.

For initial qualification, run two clean builds of the same locked inputs with compiled-output caches disabled. Compare kernel/module payload hashes, Xenomai libraries, headers and tools, effective configuration, package inventory, and normalized image configuration. Investigate differences and report the exact scope of any remaining nondeterminism. Claim byte-identical payload reproducibility only when that comparison passes; a passing boot test is not a substitute.

Retain the source archives and package inputs needed to repeat the build. An inventory written only after installation is provenance, not an input lock.

## 5. Implementation work packages

### A. Establish credentials and a known-good controller

Document installation of RunsOn for the example repository and the required AWS account/region configuration. Resolve and record a compatible stock RunsOn AMI before starting the build.

Use GitHub OIDC for short-lived controller credentials. Restrict trust to the actual repository identity and protected execution context, matching that repository's OIDC subject format.[^github-oidc] Grant only required image-management, probe, artifact, and cleanup permissions; scope mutation to owned resources where supported and restrict `iam:PassRole` to named profiles.

Packer provisioning and direct probes may use SSH over Session Manager. Provide the required controller plugin, guest agent and SSH service, instance profile, and outbound connectivity. RunsOn's Packer guide notes that SSH needs to be started for provisioning.[^packer][^runs-on-ami] Do not broaden SSH ingress merely to avoid configuring the management path.

**Exit:** The controller can launch and dispose of a stock-image builder; networking and authorization are proven before the Cobalt payload is built.

### B. Compile and install the Cobalt kernel and userspace

Build natively in the temporary Packer builder using the locked toolchain and package sources. Extract the pinned Linux source archive containing Dovetail and integrate the locked Xenomai Cobalt kernel sources using Xenomai's preparation procedure.[^xenomai-install]

Compile the prepared kernel with the `-xenomai-cobalt` suffix. Enable Dovetail, Cobalt, and embedded kernel configuration, and verify the expected effective settings. Record the exact output of `make kernelrelease`; do not infer it from a filename.

Install the kernel, matching modules, and required boot files. Build matching Xenomai userspace with Cobalt enabled and install its libraries, headers, and development tools under `/usr/xenomai`. Generate the initramfs and configure the bootloader to select the exact new kernel on normal startup. Keep signing and boot-mode settings consistent with the selected platform. Preserve root filesystem, console, ENA networking, and NVMe storage support needed on the selected EC2 target; Nitro uses ENA and NVMe interfaces.[^aws-nitro]

If a stock fallback kernel remains installed, it must not pass validation accidentally. The test compares the running release with the build's expected release.

**Exit:** The installed Cobalt kernel, modules, Xenomai development installation, and boot configuration are complete and their hashes are recorded.

### C. Finalize and capture the candidate

Write a root-owned in-image manifest containing stable recipe identity, kernel release, Xenomai identity, and expected installed content. Keep per-run cloud metadata in the external result manifest.

Provide a small image-owned `runner-image-env --github` command that exports image identity, `XENOMAI_ROOT=/usr/xenomai`, and the Xenomai tool path to subsequent job steps. It must not install software or export secrets. Use GitHub's environment/path files rather than assuming provisioning exports or login profiles are inherited by jobs.[^github-env]

Clean temporary keys, build credentials, checkout credentials, build directories, stale cloud-init instance state, and machine identity using a procedure qualified for the pinned parent image. Preserve the runner binaries and bootstrap resources needed for a fresh launch. Verify the image has no registered-runner credentials or previous workspaces.

Let Packer create the AMI, then wait for it to become available. Do not reboot a running validation job to finish installation.

**Exit:** A machine-readable candidate manifest and a launchable AMI exist.

### D. Probe a fresh boot independently of RunsOn registration

An external controller launches the completed AMI with a probe-only management profile. The probe instance must not register as a GitHub runner.

Set explicit boot and connectivity deadlines. Collect EC2 status, console output, initialization logs, `uname -r`, `/proc/cmdline`, effective Dovetail/Cobalt configuration, Cobalt kernel availability, and the baked recipe identity. Check the actual AMI and instance type from the controller.

Terminate the probe whether it passes or fails. Its successful result is a prerequisite for scheduling the candidate through RunsOn.

**Exit:** The finished AMI boots the intended Cobalt kernel with working guest management and networking.

### E. Run deliberately simple tests through RunsOn

Pass the candidate ID and expected identity directly from the build outputs. An illustrative Flex job selection is:

```yaml
# Excerpt only. The executable workflow must also define checkout,
# outputs, deployment inputs, action pins, permissions, and cleanup.
needs: [build, probe]
runs-on: >-
  runs-on=${{ github.run_id }}/family=${{ needs.build.outputs.instance_type }}/cpu=${{ needs.build.outputs.vcpus }}/ami=${{ needs.build.outputs.ami_id }}/spot=false
env:
  EXPECTED_KERNEL_RELEASE: ${{ needs.build.outputs.kernel_release }}
  EXPECTED_RECIPE_ID: ${{ needs.build.outputs.recipe_id }}
```

Exact per-job image and instance selection is a Flex capability.[^runs-on-labels] Validate the AMI's region, owner, architecture, launch permissions, and encryption access before scheduling it.

The first candidate job must start with identity checks and then perform only these small tests:

| Test | Evidence |
| --- | --- |
| Kernel | `uname -r` exactly matches the expected compiled release; Dovetail/Cobalt configuration and the running Cobalt kernel interface are present |
| Image | Baked recipe identity agrees with the controller's expected value |
| Environment | Image activation makes `XENOMAI_ROOT` and Xenomai tools available in a later, separate step |
| Cobalt application | Checkout; configure/build `tests/cobalt` with the installed Xenomai development tools; execute its one CTest test successfully using the Cobalt kernel |
| Actions integration | Upload a report and log artifact successfully |

Job A writes a deliberately placed sentinel outside its checkout and records its location. Job B uses a separate fresh instance, checks that sentinel is absent before making changes, and repeats the smoke tests. Verify distinct instance IDs from the controller; do not grant test jobs broad EC2 permissions just to obtain those IDs.

Both jobs compile the small application against the baked Xenomai development installation and execute it under the already-running Cobalt kernel. The application starts and joins an Alchemy task and asserts that the task runs in Cobalt primary mode. Success has no latency threshold. Grant candidate jobs read-only repository access and no image-publishing credentials.

**Exit:** Both fresh RunsOn executions build and pass the Cobalt application test.

### F. Bound failures, verify cleanup, and repeat the build

Add an independent controller-side launch/registration deadline. A job that never acquires a runner cannot execute its own timeout handler. The controller must identify the run's resources, collect diagnostics, and abort outstanding work through the supported control-plane interface when its deadline expires.

Run finalization even after a failed probe or smoke job. Verify termination of builders, probe instances, and RunsOn test instances. By default, remove disposable PoC AMIs and unreferenced snapshots after reports are retained; offer an explicit manual retention input for debugging. Tag resources with owner, run ID, purpose, and expiry. Document an orphan sweep for cancellation or controller failure; cleanup must never delete resources outside this example.

Do not silently auto-cancel an older image build when a new manual build starts. Use unique build IDs and an explicit concurrency policy.

Repeat a complete clean build for the reproducibility comparison described above, including fresh-boot tests for both candidates.

**Exit:** Success, a controlled failure, and an interrupted run have useful diagnostics and bounded resource lifetime.

## 6. Result contract and acceptance

Publish a versioned `image-result.json` plus validation reports. Its fields must cover:

| Group | Required information |
| --- | --- |
| Source | Schema version, recipe ID, recipe commit, input-lock digest, parent AMI identity |
| Payload | Kernel release, Xenomai source/version identity, configuration digest, kernel/module and Xenomai library/header/tool hashes, installed package inventory digest |
| Cloud | AWS account, region, AMI ID, snapshot IDs, architecture, boot mode, qualified instance type |
| Execution | Build ID, Packer/tool versions, observed agent/bootstrap versions, workflow reference |
| Validation | Direct-boot result, both RunsOn results, observed instance IDs, reproducibility comparison |
| Lifecycle | Creation time, retention/expiry policy, durable artifact locations |

Keep candidate creation and validation records distinguishable. A manifest emitted by Packer is not, by itself, a passed image qualification.

The example is complete when a maintainer can follow its README, manually build the Ubuntu 24.04 Cobalt AMI, build and execute the Cobalt application successfully on two fresh RunsOn instances, repeat the locked build with a meaningful comparison, and remove all example-owned resources. All infrastructure placeholders must be documented configuration inputs; committed executable recipes must contain concrete version pins.

## 7. Handoff to the companion plan

Publish an identified commit or release of the working example. The companion project can adopt the Cobalt image recipe, Packer orchestration, boot probing, fresh RunsOn validation, environment adapter, result schema, reproducibility comparison, and cleanup implementation from that revision.

Prefer a small, attributed source copy with a recorded upstream revision for the first integration. Do not create a generic image framework or a floating runtime dependency on this repository. Application-specific development environments and acceptance logic belong to the adopting project.

**Plan 2 depends on this repository's proven mechanism, not on this example's AMI, AWS account, or retained EC2 resources.**

## References

External interfaces checked on 2026-09-12. Version pins are implementation inputs to be resolved and committed before running the example.

[^runs-on-ami]: RunsOn, “Building a custom AMI with Packer.” https://runs-on.com/docs/guides/building-custom-ami/
[^runs-on-labels]: RunsOn, “Job labels,” including the Flex/Fleet distinction and `ami` selection. https://runs-on.com/docs/runners/labels/
[^packer]: HashiCorp, Packer Amazon EBS builder and Session Manager connection options. https://developer.hashicorp.com/packer/integrations/hashicorp/amazon/latest/components/builder/ebs
[^kernel-reproducible]: Linux kernel documentation, “Reproducible builds.” https://docs.kernel.org/kbuild/reproducible-builds.html
[^ubuntu-snapshot]: Ubuntu Snapshot Service. https://snapshot.ubuntu.com/
[^aws-nitro]: AWS, “Instances built on the AWS Nitro System.” https://docs.aws.amazon.com/ec2/latest/instancetypes/ec2-nitro-instances.html
[^github-oidc]: GitHub, “Configuring OpenID Connect in Amazon Web Services.” https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws
[^github-env]: GitHub, “Workflow commands for GitHub Actions,” environment files and path additions. https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands
[^xenomai-install]: Xenomai 3, “Installing Xenomai 3.x,” including Cobalt kernel preparation and userspace configuration. https://v3.xenomai.org/installation/index.html
