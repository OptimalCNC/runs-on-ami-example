locals {
  iam_prefix = "arn:aws:iam::${var.account_id}"
  regional   = "${var.region}:${var.account_id}"
  ec2        = "arn:aws:ec2:${local.regional}"

  # Explicit names exclude the deployer and its own policy/boundary resources.
  workload_role_arns = concat([
    for suffix in [
      "ec2-instance", "flex", "flex-execution", "public-ingress",
      "github-apps-setup", "cache-broker", "github-runner-cache-refresh",
      "stack-config-materializer", "job-diagnostics-resolver", "scheduler",
    ] : "${local.iam_prefix}:role/${var.name}-${suffix}-role"
  ], ["${local.iam_prefix}:role/${var.name}-image-publisher"])
  instance_profile_arn = "${local.iam_prefix}:instance-profile/${var.name}-ec2-instance-profile"
  owned                = { StringEquals = { "aws:ResourceTag/runs-on-stack-name" = var.name } }
  new_owned            = { StringEquals = { "aws:RequestTag/runs-on-stack-name" = var.name } }
}

resource "aws_iam_role" "deployment" {
  name = "${var.name}-deployer"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "${local.iam_prefix}:root" }
      Condition = { ArnEquals = { "aws:PrincipalArn" = var.trusted_principal_arns } }
    }]
  })
}

resource "aws_iam_policy" "workload_boundary" {
  name        = "${var.name}-workload-boundary"
  description = "Maximum permissions of RunsOn ${var.name} runtime and image publishing roles."
  policy      = jsonencode(local.workload_boundary)
}

resource "aws_iam_policy" "deployment" {
  for_each = {
    iam      = local.deployment_iam
    services = local.deployment_services
    network  = local.deployment_network
  }

  name        = "${var.name}-deployment-${each.key}"
  description = "RunsOn ${var.name} deployment: ${each.key}."
  policy      = jsonencode(each.value)
}

resource "aws_iam_role_policy_attachment" "deployment" {
  for_each = aws_iam_policy.deployment

  role       = aws_iam_role.deployment.name
  policy_arn = each.value.arn
}
