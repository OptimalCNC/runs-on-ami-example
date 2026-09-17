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

run "installation_setup_and_network" {
  command = apply

  assert {
    condition = (
      jsonencode(yamldecode(output.installation_yaml)) == jsonencode(output.installation) &&
      output.installation.kind == "runs-on-installation" &&
      output.installation.schema_version == 1 &&
      output.installation.environment == var.environment &&
      output.installation.setup_url == "https://example.execute-api.us-east-1.amazonaws.com"
    )
    error_message = "Installation exports must provide the selected environment and GitHub App setup URL."
  }

  assert {
    condition = alltrue([
      for secret in [var.license_key, var.notification_email] : !strcontains(output.installation_yaml, secret)
    ])
    error_message = "Installation information must not export secrets."
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
}

run "reject_cross_account_deployment" {
  command = plan
  variables {
    deployment_role_arn = "arn:aws:iam::999999999999:role/unrelated"
  }
  expect_failures = [var.deployment_role_arn]
}
