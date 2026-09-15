locals {
  # An administrator grants this policy to the existing bootstrap identity.
  # It authorizes this root module and the shared prerequisite checks in install.
  existing_identity_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription",
          "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies",
          "iam:ListInstanceProfilesForRole", "sts:AssumeRole",
        ]
        Resource = "${local.iam_prefix}:role/${var.name}-deployer"
      },
      {
        Effect = "Allow"
        Action = [
          "iam:CreatePolicy", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:DeletePolicy",
          "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion",
          "iam:ListPolicyVersions", "iam:ListEntitiesForPolicy", "iam:TagPolicy", "iam:UntagPolicy", "iam:ListPolicyTags",
        ]
        Resource = [
          "${local.iam_prefix}:policy/${var.name}-workload-boundary",
          "${local.iam_prefix}:policy/${var.name}-deployment-iam",
          "${local.iam_prefix}:policy/${var.name}-deployment-services",
          "${local.iam_prefix}:policy/${var.name}-deployment-network",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "iam:GetRole"
        Resource = local.service_linked_role_arns
      },
      {
        Effect    = "Allow"
        Action    = "iam:CreateServiceLinkedRole"
        Resource  = local.service_linked_role_arns
        Condition = { StringEquals = { "iam:AWSServiceName" = ["ecs.amazonaws.com", "spot.amazonaws.com"] } }
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetOpenIDConnectProvider", "iam:CreateOpenIDConnectProvider", "iam:TagOpenIDConnectProvider"]
        Resource = "${local.iam_prefix}:oidc-provider/token.actions.githubusercontent.com"
      },
    ]
  }
}

output "deployment_role_arn" {
  value = aws_iam_role.deployment.arn
}

output "workload_boundary_arn" {
  value = aws_iam_policy.workload_boundary.arn
}

output "existing_identity_policy_json" {
  description = "Policy an administrator grants the existing identity before bootstrap; identifiers are installation-specific."
  value       = jsonencode(local.existing_identity_policy)
}
