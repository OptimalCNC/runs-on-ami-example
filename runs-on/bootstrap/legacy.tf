# Permissions needed only while an older deployment still owns an image key.
locals {
  legacy_image_key_policy_arn = "${local.iam_prefix}:policy/${var.name}-image-key-use"
  legacy_key_retirement_statements = [
    {
      # Refresh and retire keys left in deployment state by older versions.
      Effect = "Allow"
      Action = [
        "kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus", "kms:ListResourceTags", "kms:ScheduleKeyDeletion", "kms:DeleteAlias",
      ]
      Resource  = "arn:aws:kms:${local.regional}:key/*"
      Condition = local.owned
    },
    {
      # Retire the removed key's managed policy alongside the key itself.
      Effect   = "Allow"
      Action   = "iam:DetachRolePolicy"
      Resource = local.workload_role_arns
      Condition = {
        ArnEquals = { "iam:PolicyARN" = local.legacy_image_key_policy_arn }
      }
    },
    {
      Effect = "Allow"
      Action = [
        "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions", "iam:ListPolicyTags",
        "iam:ListEntitiesForPolicy", "iam:DeletePolicy", "iam:DeletePolicyVersion",
      ]
      Resource = local.legacy_image_key_policy_arn
    },
    {
      Effect   = "Allow"
      Action   = "kms:DeleteAlias"
      Resource = "arn:aws:kms:${local.regional}:alias/${var.name}-images"
    },
    {
      Effect   = "Allow"
      Action   = "kms:ListAliases"
      Resource = "*"
    },
  ]
}
