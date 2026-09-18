locals {
  deployment_iam = {
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
        Resource = local.workload_role_arns
        Condition = {
          StringEquals = { "iam:PermissionsBoundary" = aws_iam_policy.workload_boundary.arn }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription",
          "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
          "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
        ]
        Resource = local.workload_role_arns
      },
      {
        Effect   = "Allow"
        Action   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource = local.workload_role_arns
        Condition = {
          ArnEquals = {
            "iam:PolicyARN" = [
              local.publisher_policy_arn,
              "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            ]
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy", "iam:GetPolicyVersion",
          "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:ListPolicyVersions",
          "iam:ListEntitiesForPolicy", "iam:SetDefaultPolicyVersion", "iam:TagPolicy",
          "iam:UntagPolicy", "iam:ListPolicyTags",
        ]
        Resource = local.publisher_policy_arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions"]
        Resource = aws_iam_policy.workload_boundary.arn
      },
      {
        Effect = "Allow"
        Action = [
          "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
          "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile", "iam:UntagInstanceProfile", "iam:ListInstanceProfileTags",
        ]
        Resource = local.instance_profile_arn
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = local.workload_role_arns
        Condition = {
          StringEquals = {
            "iam:PassedToService" = ["ec2.amazonaws.com", "ecs-tasks.amazonaws.com", "lambda.amazonaws.com", "scheduler.amazonaws.com"]
          }
        }
      },
    ]
  }

  deployment_services = {
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucket*", "s3:ListBucket*",
          "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration",
          "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration",
          "s3:GetAccelerateConfiguration", "s3:GetReplicationConfiguration",
          "s3:PutBucket*", "s3:DeleteBucketPolicy", "s3:DeleteObject", "s3:DeleteObjectVersion",
        ]
        Resource = ["arn:aws:s3:::${var.name}-cache-*", "arn:aws:s3:::${var.name}-cache-*/*"]
      },
      {
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction*", "lambda:GetPolicy",
          "lambda:UpdateFunction*", "lambda:ListVersionsByFunction", "lambda:ListTags",
          "lambda:TagResource", "lambda:UntagResource", "lambda:AddPermission", "lambda:RemovePermission",
          "lambda:InvokeFunction", "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
        ]
        Resource = "arn:aws:lambda:${local.regional}:function:${var.name}-*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
          "logs:ListTagsLogGroup", "logs:TagLogGroup", "logs:UntagLogGroup", "logs:ListTagsForResource",
          "logs:TagResource", "logs:UntagResource", "logs:DescribeLogStreams", "logs:GetLogEvents", "logs:FilterLogEvents",
        ]
        Resource = local.log_arns
      },
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups", "ecs:ListClusters", "ecs:ListTaskDefinitions", "ecs:DescribeTaskDefinition", "ssm:DescribeParameters"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:CreateCluster", "ecs:DeleteCluster", "ecs:DescribeClusters", "ecs:PutClusterCapacityProviders",
          "ecs:UpdateCluster", "ecs:UpdateClusterSettings", "ecs:CreateService", "ecs:DeleteService",
          "ecs:UpdateService", "ecs:DescribeServices", "ecs:ListServices", "ecs:ListTasks", "ecs:DescribeTasks",
          "ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition",
          "ecs:TagResource", "ecs:UntagResource", "ecs:ListTagsForResource",
        ]
        Resource = [
          "arn:aws:ecs:${local.regional}:cluster/${var.name}",
          "arn:aws:ecs:${local.regional}:service/${var.name}/flexd",
          "arn:aws:ecs:${local.regional}:task/${var.name}/*",
          "arn:aws:ecs:${local.regional}:task-definition/${var.name}-flexd:*",
        ]
      },
      {
        # Provider 6.45 waits for the deployment created by CreateService or
        # UpdateService, authorizing both the service and deployment ARN.
        Effect = "Allow"
        Action = ["ecs:ListServiceDeployments", "ecs:DescribeServiceDeployments"]
        Resource = [
          "arn:aws:ecs:${local.regional}:service/${var.name}/flexd",
          "arn:aws:ecs:${local.regional}:service-deployment/${var.name}/flexd/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:Describe*", "dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive", "dynamodb:UpdateContinuousBackups", "dynamodb:ListTagsOfResource", "dynamodb:TagResource", "dynamodb:UntagResource"]
        Resource = "arn:aws:dynamodb:${local.regional}:table/${var.name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueue*", "sqs:SetQueueAttributes", "sqs:ListQueueTags", "sqs:TagQueue", "sqs:UntagQueue"]
        Resource = "arn:aws:sqs:${local.regional}:${var.name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:Subscribe", "sns:Unsubscribe", "sns:GetSubscriptionAttributes", "sns:SetSubscriptionAttributes", "sns:ListSubscriptionsByTopic", "sns:ListTagsForResource", "sns:TagResource", "sns:UntagResource"]
        Resource = ["arn:aws:sns:${local.regional}:${var.name}-alerts", "arn:aws:sns:${local.regional}:${var.name}-alerts:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy", "secretsmanager:UpdateSecret", "secretsmanager:ListSecretVersionIds", "secretsmanager:TagResource", "secretsmanager:UntagResource"]
        Resource = "arn:aws:secretsmanager:${local.regional}:secret:/runs-on/${var.name}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:GetParameter", "ssm:DeleteParameter", "ssm:ListTagsForResource", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource"]
        Resource = "arn:aws:ssm:${local.regional}:parameter/${var.name}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["events:PutRule", "events:DeleteRule", "events:DescribeRule", "events:PutTargets", "events:RemoveTargets", "events:ListTargetsByRule", "events:ListTagsForResource", "events:TagResource", "events:UntagResource"]
        Resource = "arn:aws:events:${local.regional}:rule/${var.name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule"]
        Resource = "arn:aws:scheduler:${local.regional}:schedule/default/${var.name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["resource-groups:CreateGroup", "resource-groups:DeleteGroup", "resource-groups:GetGroup*", "resource-groups:GetTags", "resource-groups:UpdateGroup", "resource-groups:UpdateGroupQuery", "resource-groups:Tag", "resource-groups:Untag"]
        Resource = "arn:aws:resource-groups:${local.regional}:group/${var.name}-ec2-instances"
      },
      {
        # Read and retire the budget present in existing installation state.
        Effect   = "Allow"
        Action   = ["budgets:ModifyBudget", "budgets:ViewBudget", "budgets:ListTagsForResource"]
        Resource = "arn:aws:budgets::${var.account_id}:budget/${var.name}-app-daily-budget"
      },
    ]
  }

  network_actions = [
    "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute", "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
    "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
    "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable", "ec2:ReplaceRouteTableAssociation",
    "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute",
    # Existing installations still need permission to remove their S3 endpoint.
    "ec2:DeleteVpcEndpoints",
    "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
    "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:ModifySecurityGroupRules",
    "ec2:CreateLaunchTemplate", "ec2:DeleteLaunchTemplate", "ec2:CreateLaunchTemplateVersion", "ec2:DeleteLaunchTemplateVersions", "ec2:ModifyLaunchTemplate",
    "ec2:CreateTags", "ec2:DeleteTags",
  ]
  deployment_network = {
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ec2:Describe*"
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = local.network_actions
        Resource  = "${local.ec2}:*"
        Condition = local.owned
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:CreateRouteTable",
          "ec2:CreateSecurityGroup", "ec2:CreateLaunchTemplate",
        ]
        Resource  = "${local.ec2}:*"
        Condition = local.new_owned
      },
      {
        # The authorization covers both the new rule and its existing parent
        # security group. The parent is constrained by the ownership grant.
        Effect    = "Allow"
        Action    = "ec2:AuthorizeSecurityGroupEgress"
        Resource  = "${local.ec2}:security-group-rule/*"
        Condition = local.new_owned
      },
      {
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "${local.ec2}:*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/runs-on-stack-name" = var.name
            "ec2:CreateAction"                  = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup", "CreateLaunchTemplate", "AuthorizeSecurityGroupEgress"]
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = "apigateway:GET"
        Resource = "arn:aws:apigateway:${var.region}::/restapis*"
      },
      {
        Effect   = "Allow"
        Action   = "apigateway:POST"
        Resource = "arn:aws:apigateway:${var.region}::/restapis"
        Condition = {
          StringEquals = { "apigateway:Request/ApiName" = "${var.name}-public-ingress" }
        }
      },
      {
        Effect    = "Allow"
        Action    = ["apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"]
        Resource  = "arn:aws:apigateway:${var.region}::/restapis/*"
        Condition = local.owned
      },
      {
        Effect   = "Allow"
        Action   = "apigateway:GET"
        Resource = "arn:aws:apigateway:${var.region}::/tags/*"
      },
      {
        # CreateRestApi checks its tag-on-create permission against this ARN.
        # Tag updates also require PATCH on the underlying API, scoped above
        # to APIs that already belong to this installation.
        Effect   = "Allow"
        Action   = "apigateway:PUT"
        Resource = "arn:aws:apigateway:${var.region}::/tags/arn%3Aaws%3Aapigateway%3A${var.region}%3A%3A%2Frestapis%2F*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/runs-on-stack-name" = var.name
          }
        }
      },
    ]
  }
}
