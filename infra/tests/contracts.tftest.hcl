provider "aws" {
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}

variables {
  foundation            = jsondecode(file("management.tfvars.json.example")).foundation
  vpc_id                = "vpc-11111111111111111"
  subnet_id             = "subnet-11111111111111111"
  vpc_cidr              = "10.20.0.0/16"
  source_ami_id         = "ami-11111111111111111"
  controller_ami_id     = "ami-22222222222222222"
  instance_type         = "t3.small"
  builder_instance_type = "c7i.large"
}

run "management_scopes_image_and_network_permissions" {
  command = plan
  variables {
    existing_security_group_id = "sg-11111111111111111"
  }
  assert {
    condition     = length(aws_security_group.management) == 0 && length(aws_iam_role_policy.controller) == 1
    error_message = "Management must attach its policy without modifying an existing security group."
  }
  assert {
    condition     = toset([for statement in jsondecode(output.controller_policy_json).Statement : statement.Resource if try(statement.Sid, "") == "LaunchLockedParents"][0]) == toset(["arn:aws:ec2:us-east-1::image/ami-11111111111111111", "arn:aws:ec2:us-east-1::image/ami-22222222222222222"])
    error_message = "Launch permissions must use both explicitly selected parents."
  }
  assert {
    condition     = toset([for statement in jsondecode(output.controller_policy_json).Statement : statement.Resource if try(statement.Sid, "") == "UseConfiguredLaunchNetwork"][0]) == toset(["arn:aws:ec2:us-east-1:123456789012:subnet/subnet-11111111111111111", "arn:aws:ec2:us-east-1:123456789012:security-group/sg-11111111111111111"])
    error_message = "Launch permissions must use the selected management subnet and security group."
  }
  assert {
    condition     = output.root_volume_gib == 16 && output.parent_root_volume_gib == 30
    error_message = "The candidate root must default to 16 GiB while parent probes retain 30 GiB."
  }
  assert {
    condition     = tonumber([for statement in jsondecode(output.controller_policy_json).Statement : statement.Condition.NumericLessThanEquals["ec2:VolumeSize"] if try(statement.Sid, "") == "CreateTaggedEncryptedVolumes"][0]) == 30
    error_message = "The launch policy must accommodate 30 GiB parent probes without increasing the candidate root."
  }
}

run "larger_candidate_root_is_authorized" {
  command = plan
  variables {
    existing_security_group_id = "sg-11111111111111111"
    root_volume_gib            = 40
  }
  assert {
    condition     = tonumber([for statement in jsondecode(output.controller_policy_json).Statement : statement.Condition.NumericLessThanEquals["ec2:VolumeSize"] if try(statement.Sid, "") == "CreateTaggedEncryptedVolumes"][0]) == 40
    error_message = "The launch policy must accommodate an explicitly larger candidate root."
  }
}

run "builder_scratch_volume_is_authorized" {
  command = plan
  variables {
    existing_security_group_id = "sg-11111111111111111"
    root_volume_gib            = 8
    parent_root_volume_gib     = 8
  }
  assert {
    condition     = tonumber([for statement in jsondecode(output.controller_policy_json).Statement : statement.Condition.NumericLessThanEquals["ec2:VolumeSize"] if try(statement.Sid, "") == "CreateTaggedEncryptedVolumes"][0]) == 16
    error_message = "The launch policy must accommodate the builder's 16 GiB scratch volume."
  }
}

run "external_controller_is_not_modified" {
  command = plan
  variables {
    foundation = merge(jsondecode(file("management.tfvars.json.example")).foundation, {
      controller_role_managed = false
    })
    existing_security_group_id = "sg-11111111111111111"
  }
  assert {
    condition     = length(aws_iam_role_policy.controller) == 0 && length(aws_security_group.management) == 0
    error_message = "Referencing external resources must not attach policies or create a replacement security group."
  }
}

run "reject_mixed_account_foundation" {
  command = plan
  variables {
    foundation = merge(jsondecode(file("management.tfvars.json.example")).foundation, {
      controller_role_arn = "arn:aws:iam::210987654321:role/other-controller"
    })
  }
  expect_failures = [var.foundation]
}
