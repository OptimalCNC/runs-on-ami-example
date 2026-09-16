# RunsOn installation

This module provisions a RunsOn Flex installation and an AMI publishing target
in one AWS account and region. Start here from a fresh checkout:

```sh
cd runs-on
```

The `bootstrap/` Terraform root creates the dedicated deployment role using an
existing authorized AWS login. The `deployment/` root assumes that role and
provisions RunsOn, public networking, and the image publisher role and encryption
key. `./install` connects the two roots and exports their consumer contracts.
GitHub App registration and installation, and notification email confirmation,
remain browser steps.

The blueprint pins the official [RunsOn Flex Terraform module
3.3.1](https://github.com/runs-on/terraform-aws-runs-on/tree/release/v3.3.1/modules/flex).
It uses a small Fargate control plane, two public subnets, and an S3 gateway
endpoint. Recurring costs include the
[Fargate control plane](https://aws.amazon.com/fargate/pricing/), one
[public IPv4 address](https://aws.amazon.com/vpc/pricing/), one
[KMS key](https://aws.amazon.com/kms/pricing/), and two
[Secrets Manager secrets](https://aws.amazon.com/secrets-manager/pricing/).
Use the linked pricing pages for rates in your region. Runner jobs, stored data,
logs, and API requests add to that baseline. The default maximum runner lifetime
is 60 minutes. Daily cost reporting uses a $5 notification threshold, which does
not enforce a spending cap.

## Prerequisites

Install AWS CLI v2, Terraform 1.16.0, and Python 3.12 with `venv` support on
Linux. Make `aws` and `terraform` available on `PATH`, then install the Python
dependency:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`./install` automatically uses `.venv/bin/python`. Terraform initialization
downloads the pinned module and AWS provider.

If you enable GitHub publishing and leave `publisher_github_subject_prefix`
empty, also install the GitHub CLI and authenticate it with access to read the
repository's Actions OIDC subject configuration. The installer uses this to
discover the correct publishing trust. Supplying the known subject prefix
explicitly makes this discovery unnecessary.

Have a RunsOn license, an email address for AWS notifications, and permission
to create and install a GitHub App on the intended organization or personal
account. The existing AWS identity needs the permissions described in
[PERMISSIONS.md](PERMISSIONS.md). Authenticate it using your normal AWS login
method, for example IAM Identity Center:

```sh
aws sso login --profile runs-on-admin
aws sts get-caller-identity --profile runs-on-admin
```

Create private configuration storage and copy the example:

```sh
install -d -m 700 .local
cp installation.example.yaml .local/config.yaml
```

Edit `.local/config.yaml` and create the referenced `license.txt` and
`notification-email.txt` files inside `.local/`. File paths in the configuration
resolve relative to that configuration file. Put only the license or email
address in its respective file. `.local/` and `.venv/` are Git-ignored; state and
plans can contain the license and email address, so keep private backups of the
whole `.local/` directory.

Set these configuration fields before deploying:

| Field | Meaning |
| --- | --- |
| `account_id`, `region` | Quoted AWS account ID and region that will own RunsOn and published images |
| `name` | Unique installation name: 3–24 lowercase letters, digits, or hyphens, beginning with a letter; used in resource names and ownership tags |
| `environment` | RunsOn `env` label used by workflow jobs |
| `github_organization` | GitHub organization or personal account where the App will be installed |
| `trusted_principal_arns` | Existing IAM user or role ARNs in `account_id` permitted to assume the deployment role |
| `publisher_principal_arns` | Existing local IAM users or roles permitted to publish images |
| `publisher_github_repository`, `publisher_github_environment` | Optional exact repository and protected GitHub environment permitted to publish through OIDC |
| `publisher_github_subject_prefix` | Optional known repository OIDC subject prefix; empty discovers the current prefix through GitHub |
| `existing_github_oidc_provider_arn` | Existing account-wide GitHub OIDC provider, when GitHub publishing is enabled and the provider already exists |

Choose at least one local publishing principal or a GitHub publishing repository.
For local publishing, its principal's AWS policy must also permit assuming the
publisher role. For GitHub publishing, configure the named environment's allowed
branches and approval rules in the repository settings. Trust uses the exact
repository/environment subject recorded in the publishing contract, including
immutable identifiers where GitHub uses them. Use IAM role ARNs in the
configuration, including their paths; an STS assumed-role session ARN is not a
role ARN.

## Bootstrap the deployment role

An existing authorized AWS identity performs the initial bootstrap. Review its
planned IAM resources, then apply them:

```sh
./install bootstrap --plan --profile runs-on-admin
./install bootstrap --profile runs-on-admin
```

Bootstrap creates the deployment role and its policies, including a permissions
boundary for workload roles. It also ensures that the account-wide ECS and Spot
service-linked roles exist. When GitHub publishing is configured, it finds or
creates the GitHub OIDC provider. These shared AWS account prerequisites remain
outside this installation's Terraform state.

The deployment role ARN and workload boundary ARN flow directly from bootstrap
state into the deployment root. AWS provider role assumption obtains and
refreshes temporary credentials. There is no access-key file to copy between
the components.

For an unattended installation, `./install apply --yes --profile runs-on-admin`
can bootstrap IAM, deploy, and export contracts in one invocation. `--yes`
approves the Terraform changes without an interactive prompt. Normal `apply`
prompts before applying each component it needs to create.

## Deploy RunsOn and the publishing target

Review and apply the deployment using an AWS identity trusted by the bootstrap
role:

```sh
./install plan --profile runs-on-admin
./install apply --deployment-only --profile runs-on-admin
```

On a fresh installation, `plan` covers bootstrap only because the deployment
provider needs an existing role. After bootstrap, `plan` covers deployment.
Ordinary `apply` bootstraps automatically when its local bootstrap state has
no deployment role; subsequent applies use the existing role. Run the explicit
`bootstrap` command when changing deployment trust or its permissions.

Successful deployment writes two versioned YAML contracts:

| File | Consumer and contents |
| --- | --- |
| `.local/contracts/installation.yaml` | RunsOn execution: account, region, environment, setup URL, pinned versions, networking, and runtime identities |
| `.local/contracts/publishing.yaml` | Image publishing: account, region, publisher role, allowed authentication, image encryption key, raw-disk upload method, and required ownership tags |

Both files use `schema_version: 1`. Their source of truth is
[deployment/outputs.tf](deployment/outputs.tf); the installer exports Terraform's
`yamlencode` output after a successful apply. Give consumers an explicit path to
the appropriate YAML file. They need neither Terraform nor access to its state.
The contracts contain identifiers and authentication metadata; callers obtain
temporary credentials when publishing.

The publishing destination is regional EC2 AMIs backed by EBS snapshots. The
publisher uploads a raw disk through EBS direct APIs using the supplied KMS key,
then registers the snapshot as an AMI. It must supply the contract's required
tags when creating snapshots and AMIs. Image creation, validation, publishing,
and retention belong to the image module; individual AMIs and snapshots are
outside installation Terraform state.

## Finish the GitHub setup

Open `setup_url` from `.local/contracts/installation.yaml` in a browser. Follow
the RunsOn setup page to register the private GitHub App on the account named by
`github_organization`, then install the App and authorize the repositories that
will use runners. GitHub may require an organization owner to complete these
steps. Return to the RunsOn setup page and complete its connection flow. The
[upstream GitHub App guide](https://github.com/runs-on/terraform-aws-runs-on/blob/release/v3.3.1/modules/flex/docs/github-app-config.md)
describes the registration flow.

Open the AWS notification subscription email sent to `notification_email_file`
and follow its confirmation link. Terraform creates the subscription; the email
recipient confirms it.

To attribute costs in the daily reports, have a billing administrator activate
the `runs-on-installation` cost allocation tag in AWS Billing. Activation can
take 24 hours. This optional reporting setup uses separate billing permissions;
the bootstrap policy does not grant them.

Once the [Check RunsOn installation
workflow](../.github/workflows/runs-on-installation-smoke.yml) is available on
the repository's default branch, dispatch it from GitHub's Actions tab or the
GitHub CLI. Use this installation's `environment` value:

```sh
gh workflow run runs-on-installation-smoke.yml -f environment=production
```

The workflow requests an on-demand stock `2cpu-linux-x64` runner and executes a
small job. A successful run establishes that the App receives jobs, RunsOn
launches a runner, and the runner registers and executes the job. This check
requires App access to the repository and no publishing credentials. Custom
image behavior is verified separately by [RunsOn execution](../execution/README.md).

## Updates, exports, and removal

Keep the configuration and both states together. Their `account_id`, `name`,
and `region` identify one installation; the installer rejects inputs that
conflict with its existing state. Use a separate checkout and `.local/`
directory for another installation. `--config` selects an input file for this
checkout's states.

```text
.local/
  config.yaml
  license.txt
  notification-email.txt
  contracts/
    installation.yaml
    publishing.yaml
  state/
    bootstrap.tfstate
    deployment.tfstate
  terraform/
    bootstrap/
    deployment/
```

To change deployment settings, edit the configuration, run `./install plan`,
then `./install apply --deployment-only`, using the appropriate `--profile`.
Use `./install bootstrap --plan` and `./install bootstrap` for bootstrap IAM
changes. Retain the bootstrap role while deployment resources still require it.

```sh
./install status
./install export
```

`status` prints the installation contract from Terraform state. `export`
recreates both YAML files from state. These commands work without the
configuration and secret files. Neither command performs a live runner health
check. Generated contracts should be regenerated from Terraform rather than
edited by hand.

Before decommissioning, retire published images and their snapshots, stop runner
jobs, and remove retained data from installation-owned buckets. Then run:

```sh
./install destroy --profile runs-on-admin
```

The installer reads the image encryption key from Terraform state and checks
that no snapshots or volumes still depend on it before running Terraform
destroy. This also protects retained images when an interrupted deployment has
not yet exported its contracts. Key deletion is scheduled with a 30-day waiting
period. After deployment destruction succeeds, the installer
removes its exported contracts and retains bootstrap IAM and local state. The
shared service-linked roles and GitHub OIDC provider remain available to other
installations. GitHub App removal is a separate action in GitHub settings.
