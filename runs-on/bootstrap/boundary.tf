locals {
  log_arns = [
    "arn:aws:logs:${local.regional}:log-group:/runs-on/${var.name}/*",
    "arn:aws:logs:${local.regional}:log-group:/aws/lambda/${var.name}-*",
    "arn:aws:logs:${local.regional}:log-group:/aws/ecs/${var.name}/*",
    "arn:aws:logs:${local.regional}:log-group:${var.name}/ec2/instances*",
  ]
  workload_boundary = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect = "Allow"
        Action = [
          "ec2:Describe*", "ec2:GetEbsEncryptionByDefault", "pricing:GetProducts", "cloudwatch:GetMetric*", "cloudwatch:DescribeAlarms",
          "cloudtrail:LookupEvents", "ce:GetCostAndUsage", "ecr-public:Get*", "ecr-public:Describe*",
          "ecr-public:BatchCheckLayerAvailability", "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "sts:GetServiceBearerToken"
        Resource  = "*"
        Condition = { StringEquals = { "sts:AWSServiceName" = "ecr-public.amazonaws.com" } }
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = ["RunsOn/Runners", "CWAgent"] } }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:*", "logs:*", "dynamodb:*", "sqs:*", "sns:*", "ssm:*",
          "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "lambda:InvokeFunction",
        ]
        Resource = concat(local.log_arns, [
          "arn:aws:s3:::${var.name}-cache-*", "arn:aws:s3:::${var.name}-cache-*/*",
          "arn:aws:dynamodb:${local.regional}:table/${var.name}-*",
          "arn:aws:sqs:${local.regional}:${var.name}-*",
          "arn:aws:sns:${local.regional}:${var.name}-alerts",
          "arn:aws:ssm:${local.regional}:parameter/${var.name}/*",
          "arn:aws:secretsmanager:${local.regional}:secret:/runs-on/${var.name}/*",
          "arn:aws:lambda:${local.regional}:function:${var.name}-*",
        ])
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:PassRole", "sts:AssumeRole", "sts:TagSession"]
        Resource = "${local.iam_prefix}:role/${var.name}-ec2-instance-role"
      },
      {
        Effect   = "Allow"
        Action   = "iam:GetRole"
        Resource = local.service_linked_role_arns
      },
      {
        # CreateFleet checks future launch resources before tags exist. Its
        # RunInstances authorization below checks ownership of the instances
        # and the existing launch-template, subnet, and security-group inputs.
        Effect = "Allow"
        Action = "ec2:CreateFleet"
        Resource = [
          "${local.ec2}:fleet/*", "${local.ec2}:instance/*", "${local.ec2}:volume/*",
          "${local.ec2}:network-interface/*", "${local.ec2}:spot-instances-request/*",
          "arn:aws:ec2:${var.region}::image/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = ["arn:aws:ec2:${var.region}::image/*", "${local.ec2}:spot-instances-request/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateTags", "ec2:DeleteTags", "ec2:TerminateInstances", "ec2:StopInstances", "ec2:StartInstances",
          "ec2:CreateVolume", "ec2:CreateSnapshot", "ec2:DeleteVolume", "ec2:DeleteSnapshot", "ec2:AttachVolume", "ec2:DetachVolume", "ec2:DeleteFleets",
        ]
        Resource  = ["${local.ec2}:*", "arn:aws:ec2:${var.region}::snapshot/*"]
        Condition = local.owned
      },
      {
        Effect    = "Allow"
        Action    = ["ec2:RunInstances", "ec2:CreateVolume", "ec2:CreateSnapshot"]
        Resource  = ["${local.ec2}:*", "arn:aws:ec2:${var.region}::snapshot/*"]
        Condition = local.new_owned
      },
      {
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = ["${local.ec2}:*", "arn:aws:ec2:${var.region}::snapshot/*"]
        Condition = {
          StringEquals = { "ec2:CreateAction" = ["RunInstances", "CreateFleet", "CreateVolume", "CreateSnapshot"] }
        }
      },
      {
        # The vendor cache uses AWS's managed S3 key. Constrain its use to the
        # installation bucket, including its bucket-key encryption context.
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = "arn:aws:kms:${local.regional}:key/*"
        Condition = {
          StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" }
          StringLike   = { "kms:EncryptionContext:aws:s3:arn" = ["arn:aws:s3:::${var.name}-cache-*", "arn:aws:s3:::${var.name}-cache-*/*"] }
        }
      },
    ], local.publisher_boundary_statements)
  }
}
