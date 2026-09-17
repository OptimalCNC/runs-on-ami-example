# Keep the real Flex module in this plan to check a fresh installation without
# pre-existing infrastructure or an image encryption key.
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1", region = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

mock_provider "random" {}
mock_provider "time" {}

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

run "plan_without_existing_resources" {
  command = plan
}
