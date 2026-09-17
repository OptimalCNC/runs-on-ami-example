# RunsOn installation

This module provisions RunsOn Flex and image-publishing IAM access together
in one AWS account and region. Start here from a fresh checkout:

```sh
cd runs-on
```

The `bootstrap/` Terraform root creates the dedicated deployment role using an
existing authorized AWS login. The `deployment/` root assumes that role and
provisions RunsOn, public networking, and the image publisher role. `./install`
connects the two roots and exports their consumer contracts.
GitHub App registration and installation, and notification email confirmation,
remain browser steps.

Installation and publishing share one configuration and deployment state.
Within `deployment/`, `installation.*.tf` and `publishing.*.tf` keep their inputs
and exports separate; `installation.tf` and `publishing.tf` define their resources.
Their Terraform tests are separated the same way. Bootstrap publisher permissions
live in `bootstrap/publishing.tf` and feed the shared workload boundary.

The blueprint pins the official [RunsOn Flex Terraform module
3.3.1](https://github.com/runs-on/terraform-aws-runs-on/tree/release/v3.3.1/modules/flex).
It uses a small Fargate control plane, two public subnets, and an S3 gateway
endpoint. Recurring costs include the
[Fargate control plane](https://aws.amazon.com/fargate/pricing/), one
[public IPv4 address](https://aws.amazon.com/vpc/pricing/), and two
[Secrets Manager secrets](https://aws.amazon.com/secrets-manager/pricing/).
Use the linked pricing pages for rates in your region. Runner jobs, stored data,
logs, and API requests add to that baseline. The default maximum runner lifetime
is 60 minutes. Daily cost reporting uses a $5 notification threshold, which does
not enforce a spending cap.

## Prerequisites

Use Python 3.12 with `venv` support on Linux x86-64. Install the checksum-verified
AWS CLI and Terraform versions from [tools.lock.json](tools.lock.json), then
install the Python dependency:

```sh
python3 install-tools.py
export PATH="$PWD/.local/tools/bin:$PATH"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`./install` automatically uses `.venv/bin/python`. Terraform initialization
downloads the pinned module and AWS provider.

Run this module's Python tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Check Terraform using only the reusable source, committed provider locks, and
mock tests in a temporary directory. The Bash block clears inherited `TF_*`
settings and leaves local state and variable files outside the checks. Tests use
synthetic inputs and mock providers without creating AWS resources.
`install-tools.py --terraform-only` installs just the tool needed for these checks.

```sh
bash <<'BASH'
set -euo pipefail
for variable in "${!TF_@}"; do unset "$variable"; done
export TF_IN_AUTOMATION=true
check_dir=$(mktemp -d)
trap 'rm -rf "$check_dir"' EXIT
for component in bootstrap deployment; do
  mkdir "$check_dir/$component"
  cp "$component/"*.tf "$component/.terraform.lock.hcl" "$check_dir/$component/"
  cp -R "$component/tests" "$check_dir/$component/tests"
  terraform -chdir="$check_dir/$component" fmt -check -recursive
  terraform -chdir="$check_dir/$component" init -backend=false -input=false -lockfile=readonly
  terraform -chdir="$check_dir/$component" validate
  terraform -chdir="$check_dir/$component" test -no-color
done
BASH
```

If you enable GitHub publishing and leave an entry's `subject_prefix` empty,
also install the GitHub CLI and authenticate it with access to read that
repository's Actions OIDC subject configuration. The installer discovers each
repository's publishing trust independently. Supplying known subject prefixes
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
| `publisher_github_repositories` | Optional list of exact `repository` and protected `environment` pairs permitted to publish through OIDC; environment defaults to `image-publish` |
| `publisher_github_repositories[].subject_prefix` | Optional known repository OIDC subject prefix; omitted or empty discovers that repository's current prefix through GitHub |

Choose at least one local publishing principal or a GitHub publishing repository.
For local publishing, its principal's AWS policy must also permit assuming the
publisher role. For GitHub publishing, configure each named environment's allowed
branches and approval rules in its repository settings. Trust uses the exact
repository/environment subjects recorded in the publishing contract, including
immutable identifiers where GitHub uses them. All listed pairs assume the same
publisher role and share its image lifecycle permissions. Use IAM role ARNs in the
configuration, including their paths; an STS assumed-role session ARN is not a
role ARN.

Use `[]` for `publisher_github_repositories` when GitHub publishing is disabled.
When enabled, bootstrap reuses or creates the account's GitHub OIDC provider;
its ARN is derived from `account_id` and needs no configuration field.

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
| `.local/contracts/installation.yaml` | Installation setup and runner configuration: account, region, environment, setup URL, pinned versions, networking, and runtime identities |
| `.local/contracts/publishing.yaml` | Image-publishing access: account, region, publisher role, allowed authentication, and required ownership tags |

`installation.yaml` uses `schema_version: 1`; `publishing.yaml` uses
`schema_version: 4`, with the authorized repository/environment subjects in
`authentication.github.repositories`. GitHub authentication is `null` when
disabled. Publishing ownership tags are in the top-level `required_tags` field.
The contracts' source of truth is
[deployment/installation.outputs.tf](deployment/installation.outputs.tf) and
[deployment/publishing.outputs.tf](deployment/publishing.outputs.tf); the installer exports Terraform's
`yamlencode` output after a successful apply. Give consumers an explicit path to
the appropriate YAML file. They need neither Terraform nor access to its state.
The contracts contain identifiers and authentication metadata; callers obtain
temporary credentials when publishing.

The publisher role requires the exported ownership tags on snapshots and AMIs.
[Image building, publishing, and retention](../images/README.md) belong to the
image module; individual AMIs and snapshots are outside installation Terraform
state. RunsOn uses the source image and the region's EBS defaults for runner-volume
encryption. The installation creates no custom EBS encryption key.

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

The workflow requests a lowest-price Spot `t3.nano` runner with the default RunsOn
image and reports its checks in the job summary. The environment input is optional
and defaults to `production`. A successful run establishes that the App receives
jobs, RunsOn
launches a runner, and the runner registers and executes the job. This check
requires App access to the repository and no publishing credentials. To build
and test the Cobalt example on a custom image, use
[RunsOn execution](../execution/README.md).

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

When updating an installation that owns an image encryption key, stop runner
jobs and prevent new launches until the update finishes. Retain the original
publishing target with its publication records for cleanup, and migrate or
retire every snapshot and volume using that key. Existing AMIs and snapshots
cannot be decrypted in place. The installer blocks bootstrap updates,
deployment apply, and destruction while these dependencies remain. Run
`./install bootstrap` to update permissions, then `./install apply
--deployment-only` to retire the old key, alias, and policy and export the
current publishing target. Key deletion retains the waiting period recorded in
Terraform state (30 days for this installation).

```sh
./install status
./install export
```

`status` prints the installation contract from Terraform state. `export`
recreates both YAML files from state. These commands work without the
configuration and secret files. Neither command performs a live runner health
check. Generated contracts should be regenerated from Terraform rather than
edited by hand.

Before decommissioning, stop runner jobs and remove retained data from
installation-owned buckets. Published images and snapshots remain independent
of the installation, but removal deletes their publisher role. Retain publication
records and arrange access to any images you keep. Then run:

```sh
./install destroy --profile runs-on-admin
```

If Terraform state still contains a legacy image encryption key, the installer
checks that no snapshots or volumes depend on it before running Terraform
destroy. This also protects older encrypted publications when an interrupted
deployment has not yet exported its contracts. After deployment destruction
succeeds, the installer removes its exported contracts and retains bootstrap IAM
and local state. The shared service-linked roles and GitHub OIDC provider remain available to other
installations. GitHub App removal is a separate action in GitHub settings.
