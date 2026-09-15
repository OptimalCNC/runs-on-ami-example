# Xenomai Cobalt example infrastructure

The infrastructure connects an AWS deployment to the reusable image recipe.
Terraform provisions or imports the foundation identities and artifact store,
then configures image-management access after the RunsOn network is known.
The RunsOn adapter prepares and manages the pinned vendor CloudFormation
stack. A deployment may also import an existing compatible installation.

Terraform and AWS provider versions are pinned in the source, with provider
lock files committed beside their roots. Operator inputs, Terraform state,
resolved resource identities, and cloud reports belong to the deployment.

## Configuration and existing resources

Keep operator files in an ignored `.deployment/` directory or an external
private directory. Its conventional layout is:

```text
.deployment/
  spec.json
  bindings.json
  manifest.json
  parents/
  plans/
  state/
    accepted-image.json
  runs/
  archive/
```

Copy [examples/deployment.spec.json](../examples/deployment.spec.json) to
`.deployment/spec.json` and replace its neutral values with your choices.
The specification records choices: account, region, repository, parent AMIs,
instance types, disk sizes, RunsOn installation settings, network selection,
and retention. Bindings record actual resource identities exported by
Terraform and CloudFormation or supplied through an explicit import. The
resolved manifest combines those inputs with verified parent evidence and
is the sole deployment input to image commands. Relative paths resolve from
the containing configuration file; none of these records needs to be tracked
by Git.

The Terraform roots have separate responsibilities:

| Root | Purpose |
| --- | --- |
| `foundation/` | Network-independent controller/OIDC identity, builder/probe roles and profiles, versioned artifact store, and the selected AWS-managed EBS key identity |
| `operator/` | Optional dedicated local operator user, assume-controller policy, and permissions boundary; creates no access key or console login |
| `infra/` | Management networking and image-operation policies, using foundation outputs and the selected RunsOn or existing network |

The tracked [foundation](foundation/foundation.tfvars.json.example),
[management](management.tfvars.json.example), and
[operator](operator/operator.tfvars.json.example) examples show the Terraform
input formats. Generate foundation and management inputs from the specification
and observed outputs using `scripts/deployment-config.py`. Supply real
values through explicit `-var-file` paths and keep plans and state in the
private deployment directory. Foundation outputs are inputs to RunsOn
preparation and management provisioning; copy neither account IDs nor role
ARNs into reusable policy files.

An existing-resource import must provide the same contracts as provisioned
resources. Importing a reference does not update that resource's policies:

| Resource | Required behavior |
| --- | --- |
| Controller role | Exact repository/environment OIDC trust, 6-hour maximum session, and the generated controller policy |
| Builder profile | Regional SSM registration/messaging and the required management documents; no image-publishing credentials |
| Probe profile | SSM management and `s3:PutObject` only beneath `<repository>/reports/ssm/` in the artifact bucket |
| Bucket | Private, versioned, encrypted, HTTPS-only, with an explicit retention policy for inputs, reports, and deployment state |
| Management security group | No inbound rules; HTTPS egress and DNS resolution |
| RunsOn installation | Common tag `ami-example:runs-on-repository=<repository>`, fresh ephemeral runners, and no image-publishing role for test guests |

Choose exact regional Ubuntu 24.04 x86-64 parent IDs, verified publishers, and
boot modes in the specification. Use plain Canonical Ubuntu for the source
and a stock RunsOn image for the controller. An account-owned copy is an optional retention choice. Source
inventory records the clean parent; a registered controller is not an
inventory source.

Select the runtime and builder instance types separately. Parent inventory
and controller disks must fit their selected parents; candidate probes and
smoke runners must fit the built AMI's root disk. Direct burstable probes use
Standard CPU credits. The default candidate root is 16 GiB; compilation uses an
additional disposable 16 GiB disk. Parent probes and the stock controller use
30 GiB roots. The observed RunsOn service and bootstrap versions are independent
identities. The controller inventory and image's locked bootstrap must match
the selected bootstrap version.

Top-level `private` selects management-instance public-IP behavior;
the supported RunsOn policy uses public service subnets and requires
`runs_on.private` to be `false`. Private management instances still need
working outbound access to SSM, Ubuntu
Snapshot, the locked Linux/Dovetail/Xenomai downloads, and GitHub. SSM
endpoints alone do not provide all download connectivity. Management access
uses Session Manager and requires no inbound SSH rule.

Use AWS credential profiles or explicitly selected credential/config files
outside the source and artifact directories. Keep license and notification
files private as well. The optional local operator assumes the controller
role through `scripts/local-credentials.py`; configure the helper's explicit
source profile, role ARN, and session name as a `credential_process`. Its
stdout is the credential protocol and must never be logged. Create any
long-lived access key outside Terraform so it does not enter Terraform state.

## Trust and authorization

The controller's OIDC trust matches the actual repository subject plus
`:environment:<environment>`, with audience `sts.amazonaws.com`. Select the
protected GitHub environment through the repository variable
`AMI_DEPLOYMENT_ENVIRONMENT` (default `ami-build`), matching the specification.
The RunsOn service environment is a separate setting. Read the
repository's subject configuration with
`gh api repos/<owner>/<repository>/actions/oidc/customization/sub`. Preserve an
immutable `sub_claim_prefix` when supplied, for example
`repo:<owner>@<owner-id>/<repository>@<repository-id>`. Otherwise use the
repository's name-based prefix. Enforce allowed branches in the protected
GitHub environment; an environment subject does not include a branch claim.

The local operator can only assume its configured controller with an
`ami-example-` session name. It cannot administer IAM, modify its boundary,
or perform image operations directly. The controller profile becomes usable
after the role and its explicit operator trust have been provisioned.

The controller can launch only selected parents or owned candidate images
through the configured subnet/security group. Image, snapshot, instance,
volume, and key-pair mutations require ownership tags where supported.
`iam:PassRole` is restricted to the two management roles and EC2. Test
instances are adopted for cleanup only when their exact selected image and
the preconfigured RunsOn ownership marker agree.

EC2 image and snapshot ARNs have an empty account component; account checks
and ownership tags supply the corresponding restrictions. Regional EC2
listing and SSM command-status APIs require wildcard resources. Launch
permissions for newly allocated IDs are constrained by subnet, profile,
instance type, IMDSv2, ownership tags, encrypted gp3 disks, and size. S3 access
uses the repository prefix, with bucket ownership, region, and versioning
checked before use. Cleanup separately checks AMI snapshot references.

Builder/probe policies allow regional SSM management, including
`AWS-RunShellScript` and `AWS-StartPortForwardingSession`, without image
publishing, Parameter Store, or package-distribution access. Packer tags
network interfaces at launch and creates tagged candidate snapshots through
`CreateImage`; it needs no general `CreateSnapshot` or `CopyImage` grant.
The controller can change an owned AMI's description, but not its launch
permissions.

The controller stores temporary SSH private keys in mode-0600 files outside
uploaded recipes and removes both local files and cloud key pairs after the
build. SSM SSH sessions use role-session names beginning `ami-example-`.
Retained parent copies, if selected, use a separately reviewed bootstrap
operation and do not receive disposable-build ownership tags.

Watchdogs use GitHub `actions:write` for bounded cancellation. Smoke jobs
have `contents:read` and no OIDC or image-management role. Recovery runs
trusted default-branch code, consumes the saved execution context, and
selects resources by live identity and ownership checks.

## Review and apply

For an existing deployment, preserve a backup of Terraform state and follow
[`state-migration.json`](state-migration.json) before planning the split roots.
Do not initialize an empty foundation state against resources already
managed by the old root. Review state moves and the resulting plans without
changing live resources; applying infrastructure changes remains a separate
action.

Migrate existing checked-in deployment records into private files before
removing them from source. Supply a legacy checkout or its preserved archive:

```sh
python3 scripts/migrate-deployment.py \
  --source-dir /path/to/legacy-checkout \
  --output /path/to/private-deployment
```

The migration preserves original bytes, emits the specification, bindings,
and manifest, and verifies their parity. Supply `--name-prefix` when the
existing prefix cannot be recovered from the legacy Terraform inputs.
The accepted-image record is preserved locally without promotion. This
command performs no cloud operation or Terraform state application.

Bootstrap follows the dependencies below. Save each plan and its outputs
under the private deployment directory; review resource and cost changes
before applying an exact plan.

1. Provision or import the foundation identities and artifact store. The
   optional operator user can be created once its intended controller role
   is known.
2. Install RunsOn using the foundation outputs, or import a compatible
   existing installation. Record the stack's actual network and runner
   resources in bindings.
3. Provision management access using the foundation and selected network,
   then refresh the bindings with those outputs.
4. Capture the selected source and controller parents through the standalone
   inventory command described in [operations](../docs/operations.md#lock-the-parent-images).
5. Resolve the specification, bindings, and parent records into the manifest,
   then validate and publish the deployment bundle.

For a new deployment, generate the foundation inputs and save an explicit
Terraform plan. The commands below use local state in `.deployment/`; use a
protected backend instead when multiple operators share ownership.

```sh
mkdir -p .deployment/plans .deployment/state
python3 scripts/deployment-config.py terraform \
  --spec .deployment/spec.json --layer foundation \
  --output .deployment/plans/foundation.tfvars.json
terraform -chdir=infra/foundation init -lockfile=readonly
terraform -chdir=infra/foundation plan \
  -state="$PWD/.deployment/state/foundation.tfstate" \
  -var-file="$PWD/.deployment/plans/foundation.tfvars.json" \
  -out="$PWD/.deployment/plans/foundation.tfplan"
terraform -chdir=infra/foundation show "$PWD/.deployment/plans/foundation.tfplan"
```

After reviewing the plan and its resource scope, apply that saved plan and
export machine-readable outputs:

```sh
terraform -chdir=infra/foundation apply \
  -state="$PWD/.deployment/state/foundation.tfstate" \
  "$PWD/.deployment/plans/foundation.tfplan"
terraform -chdir=infra/foundation output \
  -state="$PWD/.deployment/state/foundation.tfstate" -json \
  > .deployment/state/foundation-outputs.json
```

The RunsOn vendor template and generic policy overlay remain in
[`runs-on/template-lock.json`](runs-on/template-lock.json) and
[`runs-on/policy-overlay.json`](runs-on/policy-overlay.json). Preparing the
rendered template performs no cloud mutation:

```sh
python3 scripts/prepare-runs-on.py \
  --config .deployment/spec.json \
  --foundation .deployment/state/foundation-outputs.json \
  --output .deployment/plans/runs-on
```

For a new AWS account, ensure the ECS service-linked role exists before
planning the stack. If it is absent, an administrator can explicitly create
it with `aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com`.
The stack helper checks this prerequisite instead of creating it during
application of a change set.

```sh
python3 scripts/runs-on-stack.py plan \
  --config .deployment/spec.json \
  --prepared .deployment/plans/runs-on/prepared.json \
  --output .deployment/plans/runs-on-change-set.json --profile admin
```

Review the saved evaluated changes and costs before executing the exact
change set:

```sh
python3 scripts/runs-on-stack.py apply \
  --change-set .deployment/plans/runs-on-change-set.json \
  --output .deployment/state/runs-on-apply.json --profile admin
```

Apply starts CloudFormation execution. Wait for successful stack creation or
update, using the returned stack ID, then capture its outputs:

```sh
python3 scripts/runs-on-stack.py inventory \
  --config .deployment/spec.json \
  --output .deployment/state/runs-on-outputs.json --profile admin
```

Choose the management subnet from the RunsOn inventory, or use the explicit
existing subnet in the specification. Inspect the selected network and AMIs
without creating resources, then generate the management inputs:

```sh
python3 scripts/deployment-config.py inspect \
  --spec .deployment/spec.json --subnet subnet-0123456789abcdef0 \
  --output .deployment/state/observations.json
python3 scripts/deployment-config.py terraform \
  --spec .deployment/spec.json --layer management \
  --foundation .deployment/state/foundation-outputs.json \
  --observations .deployment/state/observations.json \
  --output .deployment/plans/management.tfvars.json
terraform -chdir=infra init -lockfile=readonly
terraform -chdir=infra plan \
  -state="$PWD/.deployment/state/management.tfstate" \
  -var-file="$PWD/.deployment/plans/management.tfvars.json" \
  -out="$PWD/.deployment/plans/management.tfplan"
terraform -chdir=infra show "$PWD/.deployment/plans/management.tfplan"
```

Replace the example subnet with the actual selected ID. After reviewing and
applying that saved management plan, export outputs and assemble bindings:

```sh
terraform -chdir=infra apply \
  -state="$PWD/.deployment/state/management.tfstate" \
  "$PWD/.deployment/plans/management.tfplan"
terraform -chdir=infra output \
  -state="$PWD/.deployment/state/management.tfstate" -json \
  > .deployment/state/management-outputs.json
python3 scripts/deployment-config.py bindings \
  --spec .deployment/spec.json \
  --foundation .deployment/state/foundation-outputs.json \
  --management .deployment/state/management-outputs.json \
  --observations .deployment/state/observations.json \
  --runs-on-inventory .deployment/state/runs-on-outputs.json \
  --output .deployment/bindings.json
```

Capture parents and resolve the manifest using
[operations](../docs/operations.md#lock-the-parent-images). Both a new stack
and an imported installation must supply inspected RunsOn identity and
version evidence to bindings.

Plan and inventory records contain the exact stack identities and observed
resources, including vendor buckets that survive stack deletion. Keep cost
approvals, retention deadlines, and teardown evidence alongside those private
records. Apply consumes the saved change-set identity rather than rebuilding
a plan from mutable inputs.

The RunsOn service's preliminary `CreateFleet` checks have no type, profile,
tag, disk, or subnet attributes. Their regional resource grants are paired
with separate `RunInstances` permissions that constrain the actual launch.
The rendered overlay derives account, region, repository ownership, parent
images, encryption key, and role exclusions from deployment inputs.

Local Terraform initialization and validation provision no resources:

```sh
terraform -chdir=infra/foundation init -backend=false -lockfile=readonly
terraform -chdir=infra/foundation validate
terraform -chdir=infra init -backend=false -lockfile=readonly
terraform -chdir=infra validate
```

Publish the resolved deployment only after its configuration and resource
scope have been reviewed. The example below uses neutral identity values;
replace them with the deployment's actual account, region, and repository:

```sh
python3 scripts/deployment-state.py pack \
  --deployment .deployment/manifest.json \
  --output .deployment/state/deployment.tar
python3 scripts/deployment-state.py publish \
  --bundle .deployment/state/deployment.tar \
  --state-root s3://example-artifacts/example/image-runners/state \
  --account-id 123456789012 --region us-east-1 \
  --repository example/image-runners \
  --output .deployment/state/published.json
```

Generate the GitHub setup values from the resolved bindings:

```sh
python3 scripts/deployment-config.py bootstrap \
  --bindings .deployment/bindings.json \
  --output .deployment/state/github-settings.json
```

Configure `AMI_CONTROLLER_ROLE_ARN`, `AMI_REGION`, and `AMI_STATE_URI` in the
selected protected GitHub environment. The state URI is the prefix used by
`publish`, within the configured artifact bucket. Workflows authenticate
through OIDC, freeze the bundle's exact S3 version and SHA-256, and pass the
resolved manifest explicitly to commands. Keep deployment bundles and image
selection records in private versioned state; GitHub artifacts contain only
the reports selected by the workflow.


Use a protected Terraform backend for a shared deployment. The artifact
store has `force_destroy=false`; teardown requires an explicit decision about
retained reports, inputs, and deployment state. Clean up temporary build
resources first, then review retained images and supporting infrastructure.
Keep the state and credentials needed for cleanup until it is complete.
