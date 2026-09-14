provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}

override_data {
  target = data.aws_kms_alias.ebs
  values = {
    target_key_arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
  }
}

variables {
  account_id = "123456789012"
  region     = "us-east-1"
  repository = "example/image-runners"
}

run "foundation_without_network_or_images" {
  command = plan

  assert {
    condition     = output.artifact_bucket == "ami-example-123456789012-us-east-1-artifacts"
    error_message = "A new foundation must derive its bucket name from the deployment identity."
  }
  assert {
    condition     = length(aws_iam_role.management) == 2 && length(aws_iam_instance_profile.management) == 2 && length(aws_iam_role.controller) == 1
    error_message = "The foundation must establish controller, builder and probe identities before networking."
  }
  assert {
    condition     = alltrue([for rule in aws_s3_bucket_lifecycle_configuration.artifacts[0].rule : alltrue([for filter in rule.filter : filter.prefix == "example/image-runners/reports/"])])
    error_message = "Report expiration must not include deployment state or historical unprefixed evidence."
  }
  assert {
    condition     = contains(flatten([for statement in jsondecode(output.controller_storage_policy_json).Statement : statement.Action]), "s3:GetObjectVersion") && contains(flatten([for statement in jsondecode(output.controller_storage_policy_json).Statement : statement.Action]), "s3:PutObject")
    error_message = "The controller needs versioned reads and conditional state publication before management provisioning."
  }
}

run "existing_resources_are_references" {
  command = plan
  variables {
    existing_artifact_bucket      = "existing-example-artifacts"
    existing_oidc_provider_arn    = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    existing_controller_role_arn  = "arn:aws:iam::123456789012:role/existing-controller"
    existing_builder_profile_name = "existing-builder"
    existing_probe_profile_name   = "existing-probe"
  }
  override_data {
    target = data.aws_iam_instance_profile.existing["builder"]
    values = {
      arn      = "arn:aws:iam::123456789012:instance-profile/existing-builder"
      role_arn = "arn:aws:iam::123456789012:role/existing-builder"
    }
  }
  override_data {
    target = data.aws_iam_instance_profile.existing["probe"]
    values = {
      arn      = "arn:aws:iam::123456789012:instance-profile/existing-probe"
      role_arn = "arn:aws:iam::123456789012:role/existing-probe"
    }
  }
  assert {
    condition     = length(aws_s3_bucket.artifacts) == 0 && length(aws_s3_bucket_policy.artifacts) == 0 && length(aws_s3_bucket_lifecycle_configuration.artifacts) == 0
    error_message = "Referencing an existing bucket must not manage its protections or retention."
  }
  assert {
    condition     = length(aws_iam_role.controller) == 0 && length(aws_iam_role_policy.controller_storage) == 0 && length(aws_iam_role.management) == 0 && length(aws_iam_role_policy.probe_logs) == 0 && length(aws_iam_openid_connect_provider.github) == 0 && !output.controller_role_managed
    error_message = "External roles, profiles and OIDC must remain reference-only."
  }
}

run "another_deployment_has_its_own_storage_and_trust" {
  command = plan
  variables {
    account_id                 = "210987654321"
    region                     = "eu-west-1"
    repository                 = "another-org/runner-images"
    environment                = "release"
    name_prefix                = "runner-images"
    github_oidc_subject_prefix = "repo:another-org@200/runner-images@300"
    existing_oidc_provider_arn = "arn:aws:iam::210987654321:oidc-provider/token.actions.githubusercontent.com"
  }
  override_data {
    target = data.aws_kms_alias.ebs
    values = {
      target_key_arn = "arn:aws:kms:eu-west-1:210987654321:key/22222222-3333-4444-5555-666666666666"
    }
  }
  assert {
    condition     = output.artifact_bucket == "runner-images-210987654321-eu-west-1-artifacts"
    error_message = "A second deployment must receive a bucket derived from its own identity."
  }
  assert {
    condition     = jsondecode(output.controller_trust_json).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:another-org@200/runner-images@300:environment:release"
    error_message = "Controller trust must bind the selected immutable repository and execution environment."
  }
  assert {
    condition     = contains(flatten([for statement in jsondecode(output.controller_storage_policy_json).Statement : statement.Resource]), "arn:aws:s3:::runner-images-210987654321-eu-west-1-artifacts/another-org/runner-images/*")
    error_message = "Artifact and state access must be scoped to the selected repository namespace."
  }
}
