mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_resource "aws_vpc" {
    defaults = { id = "vpc-0123456789abcdef0" }
  }
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-0123456789abcdef0" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-install-image-publisher" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/test-install-image-publisher" }
  }
}

override_module {
  target = module.runs_on
  outputs = {
    ingress = { url = "https://example.execute-api.us-east-1.amazonaws.com" }
  }
}

variables {
  account_id               = "123456789012"
  region                   = "us-east-1"
  name                     = "test-install"
  environment              = "testing"
  github_organization      = "example-org"
  license_key              = "secret-license-for-tests"
  notification_email       = "private-notifications@example.com"
  deployment_role_arn      = "arn:aws:iam::123456789012:role/test-install-deployment"
  workload_boundary_arn    = "arn:aws:iam::123456789012:policy/test-install-workload-boundary"
  publisher_principal_arns = ["arn:aws:iam::123456789012:role/image-operator"]
}

run "installation_setup_and_network" {
  command = apply

  assert {
    condition = (
      output.installation.environment == var.environment &&
      output.installation.setup_url == "https://example.execute-api.us-east-1.amazonaws.com"
    )
    error_message = "Installation exports must provide the selected environment and GitHub App setup URL."
  }

  assert {
    condition = alltrue([
      for secret in [var.license_key, var.notification_email] : !strcontains(jsonencode(output.installation), secret)
    ])
    error_message = "Installation information must not export secrets."
  }

  assert {
    condition = (
      length(aws_subnet.public) == 2 &&
      aws_subnet.public[0].availability_zone != aws_subnet.public[1].availability_zone &&
      aws_subnet.public[0].cidr_block != aws_subnet.public[1].cidr_block &&
      aws_route.public.gateway_id == aws_internet_gateway.this.id
    )
    error_message = "The installation must have distinct public subnets and an internet gateway route."
  }
}

run "local_publishing_contract_and_permissions" {
  command = apply

  assert {
    condition = alltrue([
      for secret in [var.license_key, var.notification_email] :
      !strcontains(jsonencode(output.publishing), secret)
    ])
    error_message = "Contracts must not export installation secrets."
  }

  assert {
    condition = (
      output.publishing.required_tags["runs-on-installation"] == var.name &&
      length(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement) == 1 &&
      toset(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Principal.AWS) == toset(var.publisher_principal_arns) &&
      aws_iam_role.publisher.permissions_boundary == var.workload_boundary_arn
    )
    error_message = "Publishing access must export ownership tags and authorize only configured principals within the workload boundary."
  }

  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "StartOwnedSnapshot"]).Condition.StringEquals["aws:RequestTag/runs-on-installation"] == var.name &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "WriteOwnedSnapshot"]).Condition.StringEquals["aws:ResourceTag/runs-on-installation"] == var.name &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "RegisterOwnedSnapshotSource"]).Action == "ec2:RegisterImage" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "RegisterOwnedSnapshotSource"]).Resource == "arn:aws:ec2:us-east-1::snapshot/*" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "RegisterOwnedSnapshotSource"]).Condition.StringEquals["aws:ResourceTag/runs-on-installation"] == var.name &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "RetireOwnedImages"]).Condition.StringEquals["aws:ResourceTag/runs-on-installation"] == var.name &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "PreventSnapshotOwnershipTakeover"]).Effect == "Deny" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "PreventSnapshotOwnershipTakeover"]).Action == "ec2:CreateTags" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "PreventSnapshotOwnershipTakeover"]).Resource == "arn:aws:ec2:us-east-1::snapshot/snap-*" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "PreventSnapshotOwnershipTakeover"]).Condition.StringNotEquals["aws:ResourceTag/runs-on-installation"] == var.name &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "TagImageAtRegistration"]).Condition.StringEquals["ec2:CreateAction"] == "RegisterImage"
    )
    error_message = "Publishers must not acquire ownership of existing images or mutate another installation's snapshots."
  }

  assert {
    condition = alltrue(flatten([
      for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : [
        for action in flatten([statement.Action]) : !startswith(action, "iam:") && !startswith(action, "kms:") && action != "ec2:RunInstances" && action != "*"
      ]
    ]))
    error_message = "Unencrypted image publication must not grant KMS, installation administration or EC2 launch privileges."
  }

  assert {
    condition = (
      contains(one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "InspectRegionalImages"]).Action, "ec2:GetEbsEncryptionByDefault") &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "InspectRegionalImages"]).Condition.StringEquals["aws:RequestedRegion"] == "us-east-1"
    )
    error_message = "Publishers must be able to inspect the destination region's EBS encryption default before uploading."
  }

}

run "github_environment_trust" {
  command = apply
  variables {
    publisher_principal_arns = []
    publisher_github_repositories = [{
      repository     = "example-org/images"
      subject_prefix = "repo:example-org/images"
      environment    = "image-publish"
    }]
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Principal.Federated == "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" &&
      length(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement) == 1 &&
      tolist(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == tolist(["repo:example-org/images:environment:image-publish"]) &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    )
    error_message = "OIDC must authorize only the selected repository and protected environment, using the existing provider."
  }
}

run "immutable_github_subject" {
  command = apply
  variables {
    publisher_principal_arns = []
    publisher_github_repositories = [{
      repository     = "example-org/images"
      subject_prefix = "repo:example-org@1234/images@5678"
    }]
  }

  assert {
    condition     = tolist(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == tolist(["repo:example-org@1234/images@5678:environment:image-publish"])
    error_message = "GitHub's immutable repository identity must be preserved in the role trust."
  }
}

run "multiple_github_repositories_with_local_publisher" {
  command = apply
  variables {
    publisher_github_repositories = [
      {
        repository     = "example-org/images"
        environment    = "image-publish"
        subject_prefix = "repo:example-org@1234/images@5678"
      },
      {
        repository     = "other-org/another-image"
        environment    = "release"
        subject_prefix = "repo:other-org/another-image"
      },
      {
        repository     = "example-org/images"
        environment    = "staging"
        subject_prefix = "repo:example-org@1234/images@5678"
      },
    ]
  }

  assert {
    condition = (
      length(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement) == 2 &&
      toset(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Principal.AWS) == toset(["arn:aws:iam::123456789012:role/image-operator"]) &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Action == "sts:AssumeRole" &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[1].Action == "sts:AssumeRoleWithWebIdentity" &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[1].Principal.Federated == "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      toset(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset([
        "repo:example-org@1234/images@5678:environment:image-publish",
        "repo:other-org/another-image:environment:release",
        "repo:example-org@1234/images@5678:environment:staging",
      ])
    )
    error_message = "Trust must retain local publishers and authorize only the exact repository/environment pairs through the shared OIDC provider."
  }

}
