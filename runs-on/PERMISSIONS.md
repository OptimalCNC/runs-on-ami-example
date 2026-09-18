# Installation permissions

The existing AWS login establishes the deployment role's authority. It must
already be authorized in the destination account; creating a new IAM user does
not give that user permission to create other roles. Use an existing IAM
Identity Center permission set or IAM role whose administrator has granted the
bootstrap permissions below.

## Identity responsibilities

| Identity | Responsibility |
| --- | --- |
| Account administrator | Prepare shared ECS/Spot service-linked roles and the GitHub OIDC provider when needed |
| Existing AWS login | Create and maintain bootstrap IAM and assume the deployment role |
| Deployment role, `<name>-deployer` | Provision this installation's networking, RunsOn resources, workload roles, and publisher IAM |
| RunsOn workload roles | Receive jobs, launch and register runners, and operate the control plane |
| Publisher role, `<name>-image-publisher` | Upload image snapshots and register or retire the installation's AMIs |

The deployment root uses the native RunsOn Terraform module. Its deployment role
calls AWS resource APIs directly. `iam:PassRole` is granted to that role for the
workload roles it assigns to AWS services. The initial login uses
`sts:AssumeRole` to enter the deployment role.

Workload roles carry the bootstrap-managed permissions boundary, which limits
the maximum permissions their attached policies can grant. The deployment role
can manage only the named workload roles and cannot modify its own role,
deployment policies, or the boundary. The initial bootstrap administrator owns
those authority changes. Review [bootstrap/permissions.tf](bootstrap/permissions.tf)
when changing the pinned RunsOn version or enabling additional services.

## Permissions for the existing AWS login

Bootstrap creates these resources, where `<name>` is the installation name from
`config.toml`:

- IAM role `<name>-deployer`.
- Managed policies `<name>-deployment-iam`, `<name>-deployment-services`, and
  `<name>-deployment-network`, attached to the deployment role.
- Managed policy `<name>-workload-boundary`, attached as the permissions boundary
  of roles created during deployment.

The initial identity needs permission to create, read, update, tag, and delete
these exact resources and their policy versions and attachments. It also needs
`sts:AssumeRole` on `<name>-deployer`. Put its same-account IAM user or role ARN
in `deployment.trusted_principal_arns`; both the source identity's permissions and the
deployment role's trust must permit assumption. For an assumed session, use its
underlying IAM role ARN in that list.

This identity can author the deployment role's policies and workload boundary,
so it is a security administrator for the installation. Scoping it to these IAM
resource names does not make it a routine deployment operator. The routine
operator's smaller grant is described below.

An administrator prepares the shared account prerequisites separately, using
the AWS console, existing account tooling, or `python3 account.py`. These resources
are not owned by either installation Terraform state. The bootstrap identity
does not need account-preparation permissions unless it also performs that task.

Have the administrator grant the following policy before bootstrap, replacing
every `<account-id>` and `<name>` with the intended configuration values:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole",
        "iam:UpdateRoleDescription", "iam:UpdateAssumeRolePolicy",
        "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies", "iam:ListRolePolicies",
        "iam:ListInstanceProfilesForRole", "sts:AssumeRole"
      ],
      "Resource": "arn:aws:iam::<account-id>:role/<name>-deployer"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:CreatePolicy", "iam:GetPolicy", "iam:GetPolicyVersion",
        "iam:DeletePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion", "iam:ListPolicyVersions",
        "iam:ListEntitiesForPolicy", "iam:TagPolicy", "iam:UntagPolicy",
        "iam:ListPolicyTags"
      ],
      "Resource": [
        "arn:aws:iam::<account-id>:policy/<name>-workload-boundary",
        "arn:aws:iam::<account-id>:policy/<name>-deployment-iam",
        "arn:aws:iam::<account-id>:policy/<name>-deployment-services",
        "arn:aws:iam::<account-id>:policy/<name>-deployment-network"
      ]
    }
  ]
}
```

The installation-specific policy is also available as the bootstrap
`existing_identity_policy_json` output after bootstrap; its canonical definition
is [bootstrap/outputs.tf](bootstrap/outputs.tf). That output records required
authorization and does not grant permissions to the current identity.

The identity running `python3 account.py` needs these separate permissions:

- `iam:GetRole` and `iam:CreateServiceLinkedRole` for
  `arn:aws:iam::<account-id>:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS`
  and `arn:aws:iam::<account-id>:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot`.
  Restrict creation with `iam:AWSServiceName` equal to `ecs.amazonaws.com` or
  `spot.amazonaws.com`.
- When GitHub publishing is enabled, `iam:GetOpenIDConnectProvider`,
  and `iam:CreateOpenIDConnectProvider` for
  `arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com`.

The helper checks the caller's account before making changes, reuses existing
roles and providers, and creates missing ones. If the prerequisites already
exist, its creation permissions can be omitted; an existing GitHub OIDC provider
must include the `sts.amazonaws.com` audience.

The policy is scoped to the resources and enabled features of this blueprint.
AWS Organizations service control policies, a source role's own permissions
boundary, and session policies can further restrict these grants. Read/list API
actions in the deployment and workload policies that do not support
resource-level authorization use `Resource: "*"`; that does not grant unrelated
write access.

## Routine deployment access

After bootstrap, routine deployment uses an existing login with this policy,
with the placeholders replaced by the destination account and installation
name:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::<account-id>:role/<name>-deployer"
  }]
}
```

Its IAM ARN must also appear in `deployment.trusted_principal_arns`. Run bootstrap using the
authorized administrator to change that list, then give the routine operator
the existing configuration and private local states. The operator can run
`python3 installer.py plan` and `python3 installer.py apply --deployment-only` using its own profile.
It does not need the bootstrap administrator's credentials. Prepare shared
account prerequisites before handing off routine deployment.

## Publishing access

Local publisher identities need `sts:AssumeRole` on the publisher ARN in
`publishing.json` and must appear in `publishing.principal_arns`. An AWS role
profile obtains temporary credentials without writing access keys to the
contract:

```ini
[profile runs-on-publisher]
role_arn = arn:aws:iam::<account-id>:role/<name>-image-publisher
source_profile = your-authorized-login
region = us-east-1
```

Use the account, ARN, and region from the generated contract. The profile belongs
in the publisher's AWS configuration, and its source profile uses the
publisher's existing authentication method.

GitHub publishing uses one account-wide OIDC provider with audience
`sts.amazonaws.com`. Each `publishing.github_repositories` entry authorizes one
exact repository/environment subject in the publisher role's trust. A publishing job requires
`id-token: write` and must name its authorized GitHub environment when requesting
temporary credentials. Configure environment protection rules in each repository.
All authorized repositories assume the same publisher role and can manage the
installation's published images; repository entries do not isolate image ownership.

The publisher policy permits EBS direct snapshot writes and AMI registration
with the required `runs-on-installation` ownership tag, and retirement of images
and snapshots carrying that tag. AMI registration also requires that tag on its
backing snapshots. Image inspection is read-only across the
selected region. The publisher can read that region's EBS encryption default and
has no KMS grants. IAM enforces ownership; the
[image publisher](../images/README.md#publish-to-the-installations-target) checks
disk and snapshot requirements. RunsOn uses no custom EBS key or associated
runtime KMS grants. Workload KMS access for the S3 cache is scoped separately
to that service and its cache objects.

The exact publishing policy and trust are maintained in
[deployment/publishing.tf](deployment/publishing.tf), with its permissions boundary
in [bootstrap/publishing.tf](bootstrap/publishing.tf). Publishing credentials
provide image lifecycle access; deployment credentials provide infrastructure
management access.
