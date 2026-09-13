# Xenomai Cobalt example infrastructure

This Terraform root provisions the example's controller OIDC role, separate
builder/probe management profiles, a security group with no inbound rules,
and a versioned, encrypted S3 bucket for inputs and diagnostics. It references
an existing RunsOn Flex installation and existing networking for the Ubuntu
24.04 Xenomai Cobalt image build and runner qualification.

It creates no EC2 instances, AMIs, VPCs, NAT gateways, VPC endpoints, or RunsOn
installation. Terraform is pinned to 1.16.0 and the AWS provider to 6.10.0;
commit `.terraform.lock.hcl` with the implementation.

## Configuration and existing resources

The separate [`operator/`](operator/) Terraform root can create the dedicated
programmatic user before networking or parent images exist. Supply
`account_id`, `region`, and the exact future `controller_role_arn` in its
ignored `example.auto.tfvars.json`, then initialize, review, and apply that
root using administrative credentials. Its only resources are an IAM user,
an assume-controller policy, and its attachment. The same policy is the
user's permissions boundary. It creates no access keys or console login.

For local credentials, use the ignored project directory `.aws-local/` with
mode `0700` and `config` and `credentials` files with mode `0600`. Create the
access key outside Terraform and write it directly to the credentials file,
so it never enters Terraform state. Select those files with
`AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE`. The controller profile
uses [`scripts/local-credentials.py`](../scripts/local-credentials.py) as its
`credential_process`, passing the exact controller ARN and an `ami-example-`
session name. The helper assumes the role using `ami-example-operator` and
delivers temporary credentials to AWS CLI in memory, without the CLI's normal
`~/.aws/cli/cache` files. Invoke the configured profile; the helper's stdout is
the credential protocol and must never be run directly or logged.
The recipe copies only locked files; artifact uploads select `artifacts/`.
Keep local credentials outside both paths.

Create an uncommitted `infra/example.auto.tfvars.json` with the actual values:

```json
{
  "account_id": "123456789012",
  "region": "us-east-1",
  "repository": "owner/runs-on-ami-example",
  "environment": "ami-build",
  "name_prefix": "ami-example",
  "vpc_id": "vpc-0123456789abcdef0",
  "vpc_cidr": "10.0.0.0/16",
  "subnet_id": "subnet-0123456789abcdef0",
  "source_ami_id": "ami-0123456789abcdef0",
  "controller_ami_id": "ami-0123456789abcdef0",
  "artifact_bucket_name": "a-globally-unique-example-bucket"
}
```

These IDs illustrate the format and cannot qualify a deployment. Source and
controller IDs must be regional, owner-verified Ubuntu 24.04 x86-64 AMIs with
the chosen exact boot mode. Both may use the same pinned public RunsOn parent
directly. An account-owned copy is optional retention protection against the
publisher withdrawing its image; copying requires separate cost approval.
The example supports the AWS-managed EBS encryption key in that same account.

Optional `existing_oidc_provider_arn`, `existing_controller_role_arn`,
`existing_builder_profile_name`, `existing_probe_profile_name`,
`existing_artifact_bucket`, and `existing_security_group_id` suppress creation
of their equivalents. Existing resources are referenced without changing
their policies. Provide the equivalent access and protections yourself:

| Resource | Required behavior |
| --- | --- |
| Controller role | Exact repository/environment OIDC trust, 6-hour maximum session, and the policy exported as `controller_policy_json` |
| Builder profile | The exported `management_policy_json` for regional SSM registration, messaging, and the two required documents; no image-publishing credentials |
| Probe profile | The same SSM policy and `s3:PutObject` only beneath `<repository>/ssm/` in the artifact bucket |
| Bucket | Private, versioned, SSE-S3 encrypted, HTTPS-only, and a documented input/report retention policy |
| Management security group | No inbound rules; HTTPS egress and DNS resolution |
| RunsOn environment | Common tag `ami-example:runs-on-repository=<repository>`, fresh ephemeral runners, no image publishing role for test guests |

Set `operator_user_arn` to the user exported by `operator/` to add that exact
user to controller trust alongside GitHub OIDC. Keep `instance_type`,
`builder_instance_type`, and `root_volume_gib` aligned with `deployment.json`.
The controller's launch policy pairs the probe type with the probe profile
and the builder type with the builder profile, and enforces on-demand
purchasing, IMDSv2, ownership tags, and encrypted gp3 volumes up to that size.

The example uses `t3.small` (2 vCPUs, 2 GiB RAM) for inventory probes,
controllers, and smoke runners. `parent_root_volume_gib=30` applies to parent
inventory and controller boots. Kernel compilation has its own
`builder_instance_type`; the example's `c7i.large` and `root_volume_gib=80`
are provisional settings for a later, separately approved build. The
candidate inherits that root size, so candidate probes and smoke runners use
it too. Root sizes are checked against the selected AMI's snapshot before
launch. Direct burstable probes explicitly use Standard CPU credits; EC2
provides no IAM condition for constraining that setting.

Set `runs_on` to `null` while capturing the initial parent inventory. Before
building or dispatching jobs, replace it with the installed values, such as
`{"environment":"ami-example","version":"3.3.1","bootstrap_version":"0.1.12"}`.
The service and bootstrap versions are independent. Verify the bootstrap
selected by the installed service against the captured parent inventory;
these example values do not establish that RunsOn is installed or compatible.

Use the exported role/profile, bucket, and security-group values in
`deployment.json`. Configure its `private` boolean to match the selected subnet
and RunsOn environment. Private instances need an existing NAT path or
equivalent working connectivity to SSM, Ubuntu Snapshot, the locked Linux,
Dovetail and Xenomai download locations, and GitHub. SSM endpoints alone do
not provide access to all download sources.
Public-subnet probes/builders receive public IPv4 addresses when `private` is
false; their security group still has no SSH ingress.

## Trust and authorization

The OIDC subject is exactly
`repo:<owner>/<repository>:environment:<environment>`, with audience
`sts.amazonaws.com`. This is the default GitHub subject for jobs using an
environment. If the organization customizes OIDC subjects, adapt the trust
policy to that actual subject before applying it. Enforce allowed branches
in the GitHub environment; an environment subject does not also contain a
branch component.

The local operator can only assume the configured controller role with an
`ami-example-` session name. It cannot administer IAM, change its boundary,
or perform image operations directly. The controller profile becomes usable
after the controller role and its explicit user trust have been provisioned.

The controller can launch only the locked parents or example-owned candidate
images through the configured subnet/security group. Image, snapshot,
instance, volume, and key-pair mutations require ownership tags where the AWS
API supports them. `iam:PassRole` is restricted to the two management roles
and EC2. Test instances are adopted for cleanup only when both their exact
candidate AMI and the existing RunsOn ownership marker agree.

EC2 image and snapshot ARNs have an empty account component; account identity
and ownership tags supply the corresponding restrictions. Regional EC2 list
operations and SSM command-status reads require `Resource = "*"` because
those actions do not support individual resource ARNs. Newly allocated IDs
require ARN suffix wildcards, constrained by the configured subnet, launch
settings, or ownership tags. S3 object access and listing are restricted to
the repository prefix; bucket ownership, region, and versioning are checked
with the bucket metadata APIs. Snapshot reference checks remain in cleanup
because IAM has no condition expressing whether an AMI still references one.

Builder/probe policies grant SSM registration and association polling only
for owned EC2 instance ARNs in the selected account and region. The SSM and legacy
EC2 message-channel APIs require wildcard resources and are region-restricted.
Document reads name only `AWS-RunShellScript` and
`AWS-StartPortForwardingSession`. These profiles receive no Parameter Store,
patching, package-distribution, or image-publishing permissions.

Packer creates the candidate with ownership tags at creation time. Its later
tagging step replaces the temporary builder purpose with `candidate`. The
controller creates a tagged SSH key pair and stores its private key only in a
local, mode-0600 file outside the uploaded recipe; the file and key pair are
removed after the build. SSM SSH sessions use role-session names beginning
`ami-example-`. Local inventory capture using an assumed role should use that
same session-name prefix.

The pinned Packer plugin tags network interfaces during launch and uses
`AWS-StartPortForwardingSession` for SSH. `CreateImage` creates its tagged
snapshots; the controller needs no separate `CreateSnapshot` or `CopyImage`
grant. If parent retention is selected later, it belongs to a separately
reviewed bootstrap operation scoped to the public source and retained-parent
tags. Public-parent inventory requires no copy permissions.
The controller can update an owned AMI's description but cannot change its
launch permissions.

The watchdog has GitHub `actions:write` only to cancel a run that exceeds its
deadline. Smoke jobs have `contents:read`, no OIDC permission, and no image
management role. The cleanup workflow checks out the trusted default branch
and selects resources by live tags rather than consuming artifact-provided
deletion lists.

## Review and apply

The separate RunsOn stack uses the vendor template pinned in
[`runs-on/template-lock.json`](runs-on/template-lock.json) and the reviewed
[`runs-on/policy-overlay.json`](runs-on/policy-overlay.json). Its settings are
in [`runs-on/deployment.json`](runs-on/deployment.json). With Python 3.12 and
PyYAML 6.0.1 installed, `python3 scripts/prepare-runs-on.py` prepares the
template and resource inventory under `.work/runs-on/` without deploying.
License and notification inputs stay in the ignored `.aws-local/` directory.
The account's [`resources.json`](resources.json) records trial costs,
physical resource IDs, retention, and teardown, including the vendor buckets
that survive stack deletion.
After resource/cost approval, use
`python3 scripts/runs-on-stack.py --profile <admin-profile> plan` to upload
the versioned template and prepare its CloudFormation change set, then use
`apply` to execute that reviewed set. The `inventory` command refreshes
physical IDs in `infra/resources.json`. The helper keeps secret request
files under `.aws-local/` with mode `0600` and removes them after each call.

Local initialization and validation do not provision resources:

```sh
terraform -chdir=infra init -backend=false -lockfile=readonly
terraform -chdir=infra validate
```

Once account/network inputs are available, prepare a concrete reviewable plan:

```sh
terraform -chdir=infra plan -out=example.tfplan
terraform -chdir=infra show example.tfplan
```

Review the resource changes and the cost stages in
[operations](../docs/operations.md#cost-approval-and-execution-stages). Obtain
cost approval before applying that exact saved plan:

```sh
terraform -chdir=infra apply example.tfplan
terraform -chdir=infra output -json
```

Use a protected Terraform state location for a shared deployment. The default
local state contains infrastructure metadata and is ignored by Git. The
artifact bucket deliberately has `force_destroy=false`: destroying the
Terraform root will not silently discard retained build evidence. Clean up
temporary build resources first, review any retained source AMI separately,
and explicitly decide what to do with retained artifacts before teardown.
