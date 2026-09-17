locals {
  iam_prefix = "arn:aws:iam::${var.account_id}"
  regional   = "${var.region}:${var.account_id}"
  ec2        = "arn:aws:ec2:${local.regional}"

  # Explicit names exclude the deployer and its own policy/boundary resources.
  workload_role_names = concat([
    for suffix in [
      "ec2-instance", "flex", "flex-execution", "public-ingress",
      "github-apps-setup", "cache-broker", "github-runner-cache-refresh",
      "stack-config-materializer", "job-diagnostics-resolver", "scheduler",
    ] : "${var.name}-${suffix}-role"
  ], [local.publisher_role_name])
  workload_role_arns   = [for name in local.workload_role_names : "${local.iam_prefix}:role/${name}"]
  workload_policy_arns = [local.publisher_policy_arn]
  instance_profile_arn = "${local.iam_prefix}:instance-profile/${var.name}-ec2-instance-profile"
  service_linked_role_arns = [
    "${local.iam_prefix}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS",
    "${local.iam_prefix}:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot",
  ]
  owned     = { StringEquals = { "aws:ResourceTag/runs-on-stack-name" = var.name } }
  new_owned = { StringEquals = { "aws:RequestTag/runs-on-stack-name" = var.name } }
}

resource "aws_iam_role" "deployment" {
  name                 = "${var.name}-deployer"
  max_session_duration = 3600

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

  lifecycle {
    precondition {
      condition     = length(jsonencode(local.workload_boundary)) <= 6144
      error_message = "The workload boundary exceeds the IAM managed-policy size limit."
    }
  }
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

  lifecycle {
    precondition {
      condition     = length(jsonencode(each.value)) <= 6144
      error_message = "A deployment policy exceeds the IAM managed-policy size limit."
    }
  }
}

resource "aws_iam_role_policy_attachment" "deployment" {
  for_each = aws_iam_policy.deployment

  role       = aws_iam_role.deployment.name
  policy_arn = each.value.arn
}
