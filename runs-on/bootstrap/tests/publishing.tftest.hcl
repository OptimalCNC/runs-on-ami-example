mock_provider "aws" {
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/abcdefghijklmnopqrstuvwx-workload-boundary" }
  }
}

variables {
  account_id             = "123456789012"
  region                 = "us-east-1"
  name                   = "abcdefghijklmnopqrstuvwx"
  trusted_principal_arns = ["arn:aws:iam::123456789012:role/Admin"]
}

run "publisher_can_inspect_regional_encryption_default" {
  command = apply

  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_policy.workload_boundary.policy).Statement : try(
        statement.Effect == "Allow" &&
        contains(flatten([statement.Action]), "ec2:GetEbsEncryptionByDefault") &&
        statement.Resource == "*",
        false
      )
    ])
    error_message = "The boundary must permit the encryption-default read granted by the publisher's regional policy."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_policy.workload_boundary.policy).Statement :
      length([for action in flatten([statement.Action]) : action if startswith(action, "kms:")]) == 0 || try(
        toset(flatten([statement.Action])) == toset(["kms:Decrypt", "kms:GenerateDataKey"]) &&
        statement.Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com" &&
        toset(statement.Condition.StringLike["kms:EncryptionContext:aws:s3:arn"]) == toset([
          "arn:aws:s3:::abcdefghijklmnopqrstuvwx-cache-*", "arn:aws:s3:::abcdefghijklmnopqrstuvwx-cache-*/*"
        ]), false
      )
    ])
    error_message = "Workloads may use KMS only through the installation's S3 cache, not for EBS encryption."
  }

  assert {
    condition = alltrue(flatten([
      for statement in jsondecode(aws_iam_policy.deployment["network"].policy).Statement : [
        for action in flatten([statement.Action]) : !startswith(action, "kms:") || contains([
          "kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus", "kms:ListResourceTags",
          "kms:ScheduleKeyDeletion", "kms:DeleteAlias", "kms:ListAliases"
        ], action)
      ]
    ]))
    error_message = "Deployment KMS access must be limited to inspecting and retiring an older installation's key."
  }

  assert {
    condition = alltrue(flatten([
      for statement in jsondecode(aws_iam_policy.workload_boundary.policy).Statement : [
        for action in flatten([statement.Action]) : !contains([
          "ec2:EnableEbsEncryptionByDefault", "ec2:DisableEbsEncryptionByDefault"
        ], action)
      ]
    ]))
    error_message = "Publishing must not change the account's encryption default."
  }
}
