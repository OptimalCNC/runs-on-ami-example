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
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
      key_id = "12345678-1234-1234-1234-123456789012"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-install-image-publisher" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/test-install-image-key-use" }
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
    alerts = { topic_arn = "arn:aws:sns:us-east-1:123456789012:test-install" }
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
      jsonencode(yamldecode(output.installation_yaml)) == jsonencode(output.installation) &&
      jsonencode(yamldecode(output.publishing_yaml)) == jsonencode(output.publishing) &&
      output.installation.kind == "runs-on-installation" &&
      output.publishing.kind == "ami-publishing-target" &&
      output.installation.schema_version == 1 && output.publishing.schema_version == 1
    )
    error_message = "Both YAML handoffs must preserve their typed contract and schema discriminator."
  }

  assert {
    condition = alltrue([
      for secret in [var.license_key, var.notification_email] :
      !strcontains(output.installation_yaml, secret) && !strcontains(output.publishing_yaml, secret)
    ])
    error_message = "Contracts must not export installation secrets."
  }

  assert {
    condition = (
      output.publishing.destination.encrypted &&
      output.publishing.destination.disk_format == "raw" &&
      output.publishing.destination.kms_key_arn == aws_kms_key.images.arn &&
      output.publishing.authentication.github == null &&
      toset(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Principal.AWS) == toset(var.publisher_principal_arns) &&
      aws_iam_role.publisher.permissions_boundary == var.workload_boundary_arn
    )
    error_message = "Local publication must use the provisioned key and explicitly authorized principals within the workload boundary."
  }

  assert {
    condition = (
      length(aws_subnet.public) == 2 &&
      aws_subnet.public[0].availability_zone != aws_subnet.public[1].availability_zone &&
      aws_subnet.public[0].cidr_block != aws_subnet.public[1].cidr_block &&
      aws_route.public.gateway_id == aws_internet_gateway.this.id &&
      aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway"
    )
    error_message = "The installation must have distinct public subnets and an internet gateway route."
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
        for action in flatten([statement.Action]) : !startswith(action, "iam:") && action != "ec2:RunInstances" && action != "*"
      ]
    ]))
    error_message = "Image publication must not grant installation administration or EC2 launch privileges."
  }

  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "GeneratePublisherSnapshotKey"]).Action == "kms:GenerateDataKey" &&
      one([for statement in jsondecode(aws_iam_policy.publisher.policy).Statement : statement if statement.Sid == "GeneratePublisherSnapshotKey"]).Resource == aws_kms_key.images.arn
    )
    error_message = "Encrypted EBS direct publication must generate data keys only with this installation's image key."
  }

  assert {
    condition = (
      one([for statement in jsondecode(aws_kms_key.images.policy).Statement : statement if statement.Sid == "AllowSpotInstancesToUseImages"]).Principal.AWS == "arn:aws:iam::123456789012:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot" &&
      contains(one([for statement in jsondecode(aws_kms_key.images.policy).Statement : statement if statement.Sid == "AllowSpotInstancesToUseImages"]).Action, "kms:Decrypt") &&
      one([for statement in jsondecode(aws_kms_key.images.policy).Statement : statement if statement.Sid == "AllowSpotResourceGrants"]).Principal.AWS == "arn:aws:iam::123456789012:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot" &&
      one([for statement in jsondecode(aws_kms_key.images.policy).Statement : statement if statement.Sid == "AllowSpotResourceGrants"]).Condition.Bool["kms:GrantIsForAWSResource"] == "true"
    )
    error_message = "Encrypted Spot launches require the account's exact Spot service-linked role to use the key and create AWS resource grants."
  }
}

run "github_environment_trust" {
  command = apply
  variables {
    publisher_principal_arns          = []
    publisher_github_repository       = "example-org/images"
    publisher_github_subject_prefix   = "repo:example-org/images"
    publisher_github_environment      = "image-publish"
    existing_github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Principal.Federated == var.existing_github_oidc_provider_arn &&
      length(jsondecode(aws_iam_role.publisher.assume_role_policy).Statement) == 1 &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:example-org/images:environment:image-publish" &&
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      output.publishing.authentication.github.subject == "repo:example-org/images:environment:image-publish"
    )
    error_message = "OIDC must authorize only the selected repository and protected environment, using the existing provider."
  }
}

run "immutable_github_subject" {
  command = apply
  variables {
    publisher_principal_arns          = []
    publisher_github_repository       = "example-org/images"
    publisher_github_subject_prefix   = "repo:example-org@1234/images@5678"
    existing_github_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:example-org@1234/images@5678:environment:image-publish" &&
      output.publishing.authentication.github.subject == "repo:example-org@1234/images@5678:environment:image-publish"
    )
    error_message = "GitHub's immutable repository identity must be preserved in the role trust and publishing handoff."
  }
}

run "missing_publisher_identity" {
  command = plan
  variables {
    publisher_principal_arns = []
  }
  expect_failures = [var.publisher_principal_arns]
}

run "reject_cross_account_deployment" {
  command = plan
  variables {
    deployment_role_arn = "arn:aws:iam::999999999999:role/unrelated"
  }
  expect_failures = [var.deployment_role_arn]
}
