# RunsOn installation

This module provisions RunsOn Flex and image-publishing IAM access together
in one AWS account and region.

The `bootstrap/` Terraform root creates the dedicated deployment role using an
existing authorized AWS login. The `deployment/` root assumes that role and
provisions RunsOn, public networking, and the image publisher role. `python3 installer.py`
connects the two roots and exports their consumer contracts.
GitHub App registration and installation, and notification email confirmation,
remain browser steps.

Installation and publishing share one configuration and deployment state.
Within `deployment/`, `installation.*.tf` and `publishing.*.tf` keep their inputs
and exports separate; `installation.tf` and `publishing.tf` define their resources.
Terraform tests cover installation and publishing together. Bootstrap publisher
permissions live in `bootstrap/publishing.tf` and feed the shared workload boundary.

The blueprint pins the official [RunsOn Flex Terraform module
3.3.1](https://github.com/runs-on/terraform-aws-runs-on/tree/release/v3.3.1/modules/flex).
It uses a small Fargate control plane and two public subnets. Recurring costs include the
[Fargate control plane](https://aws.amazon.com/fargate/pricing/), one
[public IPv4 address](https://aws.amazon.com/vpc/pricing/), and two
[Secrets Manager secrets](https://aws.amazon.com/secrets-manager/pricing/).
Use the linked pricing pages for rates in your region. Runner jobs, stored data,
logs, and API requests add to that baseline. The default maximum runner lifetime
is 60 minutes.

## Prerequisites

Install Python 3.11 or newer on Linux and Terraform yourself, and make them
available on `PATH`. Both `bootstrap/` and `deployment/` require Terraform.
The Python commands use only the standard library. Terraform initialization
downloads the pinned module and AWS provider. Install AWS CLI v2 for the
optional account-preparation helper.

Run this module's Python tests:

```sh
python3 -m unittest discover -s tests -v
```

Check Terraform using only the reusable source, committed provider locks, and
mock tests in a temporary directory. The Bash block clears inherited `TF_*`
settings and leaves local state and variable files outside the checks. Tests use
synthetic inputs and mock providers without creating AWS resources.

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
[PERMISSIONS.md](PERMISSIONS.md). Supply credentials using your normal AWS
authentication method, such as an existing profile or temporary credentials
copied from the AWS access portal into environment variables. The examples below
use `--profile runs-on-admin`; omit that option when using environment credentials.
The account script passes the profile to AWS CLI's `--profile` option; the
installer passes it to the Terraform AWS provider's `profile` setting.

Create private configuration storage and copy the example:

```sh
install -d -m 700 .local
cp installation.example.toml .local/config.toml
```

Edit `.local/config.toml` and create the referenced `license.txt` and
`notification-email.txt` files inside `.local/`. File paths in the configuration
resolve relative to that configuration file. Put only the license or email
address in its respective file. `.local/` is Git-ignored; state and
plans can contain the license and email address, so keep private backups of the
whole `.local/` directory.

The TOML tables group AWS identity, installation settings, deployment trust,
and publishing access. Set these fields before deploying:

| Field | Meaning |
| --- | --- |
| `aws.account_id`, `aws.region` | Quoted AWS account ID and region that will own RunsOn and published images |
| `installation.name` | Unique installation name: 3–24 lowercase letters, digits, or hyphens, beginning with a letter; used in resource names and ownership tags |
| `installation.environment` | RunsOn `env` label used by workflow jobs |
| `installation.github_organization` | GitHub organization or personal account where the App will be installed |
| `installation.vpc_cidr` | Installation VPC network; defaults to `10.80.0.0/16` |
| `installation.license_file`, `installation.notification_email_file` | Paths to the private license and email files, relative to the configuration file |
| `deployment.trusted_principal_arns` | Existing IAM user or role ARNs in `aws.account_id` permitted to assume the deployment role |
| `publishing.principal_arns` | Existing local IAM users or roles permitted to publish images |
| `publishing.github_repositories` | Optional list of exact `repository` and protected `environment` pairs permitted to publish through OIDC; environment defaults to `image-publish` |
| `publishing.github_repositories[].subject_prefix` | Optional known repository OIDC subject prefix; omitted or empty discovers that repository's current prefix through GitHub |

Choose at least one local publishing principal or a GitHub publishing repository.
For local publishing, its principal's AWS policy must also permit assuming the
publisher role. For GitHub publishing, configure each named environment's allowed
branches and approval rules in its repository settings. Trust uses the exact
repository/environment subjects derived from the configuration, including
immutable identifiers where GitHub uses them. All listed pairs assume the same
publisher role and share its image lifecycle permissions. Use IAM role ARNs in the
configuration, including their paths; an STS assumed-role session ARN is not a
role ARN.

Omit `[[publishing.github_repositories]]` tables when GitHub publishing is disabled.
When enabled, the account's GitHub OIDC provider must already exist; its ARN is
derived from `aws.account_id` and needs no configuration field.

Before installation, have an administrator prepare the shared ECS and EC2 Spot
service-linked roles and, when GitHub publishing is enabled, the GitHub OIDC
provider with audience `sts.amazonaws.com`. They can use the AWS console or their
existing account tooling. Alternatively, with AWS CLI available on `PATH`, run:

```sh
python3 account.py --profile runs-on-admin
```

This command checks the account identity, reuses existing prerequisites, and
creates missing ones. It does not run Terraform or change installation state.
The required permissions are described in [PERMISSIONS.md](PERMISSIONS.md).

## Bootstrap the deployment role

An existing authorized AWS identity performs the initial bootstrap. Review its
planned IAM resources, then apply them:

```sh
python3 installer.py bootstrap --plan --profile runs-on-admin
python3 installer.py bootstrap --profile runs-on-admin
```

Bootstrap creates the deployment role and its policies, including a permissions
boundary for workload roles. Shared AWS account prerequisites are prepared
separately and remain outside this installation's Terraform state.

The deployment role ARN and workload boundary ARN flow directly from bootstrap
state into the deployment root. AWS provider role assumption obtains and
refreshes temporary credentials. There is no access-key file to copy between
the components.

After account preparation, `python3 installer.py apply --yes --profile runs-on-admin`
can bootstrap IAM, deploy, and export contracts in one unattended invocation. `--yes`
approves the Terraform changes without an interactive prompt. Normal `apply`
prompts before applying each component it needs to create.

## Deploy RunsOn and the publishing target

Review and apply the deployment using an AWS identity trusted by the bootstrap
role:

```sh
python3 installer.py plan --profile runs-on-admin
python3 installer.py apply --deployment-only --profile runs-on-admin
```

On a fresh installation, `plan` covers bootstrap only because the deployment
provider needs an existing role. After bootstrap, `plan` covers deployment.
Ordinary `apply` bootstraps automatically when its local bootstrap state has
no deployment role; subsequent applies use the existing role. Run the explicit
`bootstrap` command when changing deployment trust or its permissions.

Successful deployment writes two JSON contracts:

| File | Consumer and contents |
| --- | --- |
| `.local/contracts/installation.json` | Installation setup: account, region, environment, organization, and setup URL |
| `.local/contracts/publishing.json` | Image-publishing access: account, region, publisher role, and required ownership tags |

Publishing ownership tags are in the top-level `required_tags` field.
The contracts' source of truth is
[deployment/installation.outputs.tf](deployment/installation.outputs.tf) and
[deployment/publishing.outputs.tf](deployment/publishing.outputs.tf); the installer reads
each contract with `terraform output -json <name>` after a successful apply.
Give consumers an explicit path to the appropriate JSON file. They need neither
Terraform nor access to its state.
The contracts contain resource identifiers; callers obtain temporary credentials
when publishing.

The publisher role requires the exported ownership tags on snapshots and AMIs.
[Image building, publishing, and retention](../images/README.md) belong to the
image module; individual AMIs and snapshots are outside installation Terraform
state. RunsOn uses the source image and the region's EBS defaults for runner-volume
encryption. The installation creates no custom EBS encryption key.

## Finish the GitHub setup

Open `setup_url` from `.local/contracts/installation.json` in a browser. Follow
the RunsOn setup page to register the private GitHub App on the account named by
`github_organization`, then install the App and authorize the repositories that
will use runners. GitHub may require an organization owner to complete these
steps. Return to the RunsOn setup page and complete its connection flow. The
[upstream GitHub App guide](https://github.com/runs-on/terraform-aws-runs-on/blob/release/v3.3.1/modules/flex/docs/github-app-config.md)
describes the registration flow.

Open the AWS notification subscription email sent to `notification_email_file`
and follow its confirmation link. Terraform creates the subscription; the email
recipient confirms it.

Once the [Check RunsOn installation
workflow](../.github/workflows/runs-on-installation-smoke.yml) is available on
the repository's default branch, dispatch it from GitHub's Actions tab or the
GitHub CLI. Use this installation's `environment` value:

```sh
gh workflow run runs-on-installation-smoke.yml -f environment=production
```

The workflow requests a lowest-price Spot `t3.nano` runner with the default RunsOn
image and executes `uname -a`. The environment input is optional
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
  config.toml
  license.txt
  notification-email.txt
  contracts/
    installation.json
    publishing.json
  state/
    bootstrap.tfstate
    deployment.tfstate
  terraform/
    bootstrap/
    deployment/
```

To change deployment settings, edit the configuration, run `python3 installer.py plan`,
then `python3 installer.py apply --deployment-only`, using the appropriate `--profile`.
Use `python3 installer.py bootstrap --plan` and `python3 installer.py bootstrap` for bootstrap IAM
changes. Retain the bootstrap role while deployment resources still require it.

For an existing YAML configuration, move its settings into the tables shown in
`installation.example.toml` and save it as `.local/config.toml`. Run `python3 installer.py
apply --deployment-only` to refresh the Terraform outputs and export the current
JSON contracts, then update consumer paths. For GitHub image publishing, set
`PUBLISHING_CONFIG` with the chosen image name, exported `account_id` and `region`,
and exported `required_tags` as `tags`. Set `PUBLISHING_ROLE_ARN` separately from
`publisher_role_arn`:

```sh
jq --arg name ubuntu2404-xenomai-cobalt \
  '{name: $name, account_id, region, tags: .required_tags}' .local/contracts/publishing.json \
  | gh variable set PUBLISHING_CONFIG --env production
jq -r '.publisher_role_arn' .local/contracts/publishing.json \
  | gh variable set PUBLISHING_ROLE_ARN --env production
```

Export alone reads the outputs already saved in state.

```sh
python3 installer.py export
cat .local/contracts/installation.json
```

`export` recreates both JSON files from state without the configuration and
secret files. It does not perform a live runner health check. Generated
contracts should be regenerated from Terraform rather than edited by hand.

Before decommissioning, stop runner jobs and remove retained data from
installation-owned buckets. Published images and snapshots remain independent
of the installation, but removal deletes their publisher role. Retain publication
records and arrange access to any images you keep. Then run:

```sh
python3 installer.py destroy --profile runs-on-admin
```

After deployment destruction succeeds, the installer removes its exported contracts and retains bootstrap IAM
and local state. The shared service-linked roles and GitHub OIDC provider remain available to other
installations. GitHub App removal is a separate action in GitHub settings.
