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
    runtime = {
      cluster_name        = "test-install"
      service_name        = "runs-on"
      task_role_arn       = "arn:aws:iam::123456789012:role/test-install-service-role"
      task_role_name      = "test-install-service-role"
      cluster_arn         = "arn:aws:ecs:us-east-1:123456789012:cluster/test-install"
      service_arn         = "arn:aws:ecs:us-east-1:123456789012:service/test-install/runs-on"
      log_group_name      = "/aws/ecs/test-install/runs-on"
      task_definition_arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/test-install:1"
    }
    platform = {
      networking = { security_group_ids = ["sg-0123456789abcdef0"] }
      runner_iam = {
        role_arn    = "arn:aws:iam::123456789012:role/test-install-ec2-instance-role"
        profile_arn = "arn:aws:iam::123456789012:instance-profile/test-install-ec2-instance-profile"
      }
    }
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

run "local_publishing_contract_and_permissions" {
  command = apply

  assert {
    condition = (
      jsonencode(yamldecode(output.publishing_yaml)) == jsonencode(output.publishing) &&
      output.publishing.kind == "ami-publishing-target" &&
      output.publishing.schema_version == 4
    )
    error_message = "The publishing YAML must preserve the access settings and schema discriminator."
  }

  assert {
    condition = alltrue([
      for secret in [var.license_key, var.notification_email] :
      !strcontains(output.publishing_yaml, secret)
    ])
    error_message = "Contracts must not export installation secrets."
  }

  assert {
    condition = (
      output.publishing.required_tags["runs-on-installation"] == var.name &&
      !contains(keys(output.publishing), "destination") &&
      output.publishing.authentication.github == null &&
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
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "TagNewOrOwnedSnapshots"]).Condition.StringEquals["aws:RequestTag/runs-on-installation"] == var.name &&
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
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "InspectRegionalEncryptionDefault"]).Action == "ec2:GetEbsEncryptionByDefault" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "InspectRegionalEncryptionDefault"]).Condition.StringEquals["aws:RequestedRegion"] == "us-east-1"
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
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      output.publishing.authentication.github.repositories[0].subject == "repo:example-org/images:environment:image-publish"
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
    condition = (
      tolist(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == tolist(["repo:example-org@1234/images@5678:environment:image-publish"]) &&
      output.publishing.authentication.github.repositories[0].subject == "repo:example-org@1234/images@5678:environment:image-publish"
    )
    error_message = "GitHub's immutable repository identity must be preserved in the role trust and publishing handoff."
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

  assert {
    condition = jsonencode(output.publishing.authentication.github) == jsonencode({
      method       = "github-oidc"
      audience     = "sts.amazonaws.com"
      provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
      repositories = [
        { repository = "example-org/images", environment = "image-publish", subject = "repo:example-org@1234/images@5678:environment:image-publish" },
        { repository = "other-org/another-image", environment = "release", subject = "repo:other-org/another-image:environment:release" },
        { repository = "example-org/images", environment = "staging", subject = "repo:example-org@1234/images@5678:environment:staging" },
      ]
    })
    error_message = "The publishing contract must let each repository select its own environment and exact trusted subject."
  }
}

run "reject_mismatched_github_repository_subject" {
  command = plan
  variables {
    publisher_github_repositories = [{
      repository     = "example-org/images"
      subject_prefix = "repo:other-org/images"
    }]
  }
  expect_failures = [var.publisher_github_repositories]
}

run "reject_github_wildcard_environment" {
  command = plan
  variables {
    publisher_github_repositories = [{
      repository     = "example-org/images"
      environment    = "*"
      subject_prefix = "repo:example-org/images"
    }]
  }
  expect_failures = [var.publisher_github_repositories]
}

run "reject_duplicate_github_repository_environment" {
  command = plan
  variables {
    publisher_github_repositories = [
      { repository = "example-org/images", subject_prefix = "repo:example-org/images" },
      { repository = "example-org/images", environment = "image-publish", subject_prefix = "repo:example-org/images" },
    ]
  }
  expect_failures = [var.publisher_github_repositories]
}

run "missing_publisher_identity" {
  command = plan
  variables {
    publisher_principal_arns = []
  }
  expect_failures = [var.publisher_principal_arns]
}
