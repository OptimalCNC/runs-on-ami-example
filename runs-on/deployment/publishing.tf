locals {
  github_publishing        = length(var.publisher_github_repositories) > 0
  github_oidc_provider_arn = local.github_publishing ? var.existing_github_oidc_provider_arn : null
  github_publishers = [for publisher in var.publisher_github_repositories : {
    repository  = publisher.repository
    environment = publisher.environment
    subject     = "${publisher.subject_prefix}:environment:${publisher.environment}"
  }]
  snapshot_arn = "arn:aws:ec2:${var.region}::snapshot/*"
  image_arn    = "arn:aws:ec2:${var.region}::image/*"
  publication_tags = {
    "runs-on-installation" = var.name
  }

  publisher_trust = {
    Version = "2012-10-17"
    Statement = concat(
      length(var.publisher_principal_arns) > 0 ? [{
        Sid       = "AuthorizedLocalPublishers"
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { AWS = var.publisher_principal_arns }
      }] : [],
      local.github_publishing ? [{
        Sid       = "AuthorizedGitHubEnvironment"
        Effect    = "Allow"
        Action    = "sts:AssumeRoleWithWebIdentity"
        Principal = { Federated = local.github_oidc_provider_arn }
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = [for publisher in local.github_publishers : publisher.subject]
          }
        }
      }] : []
    )
  }

  publisher_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InspectRegionalImages"
        Effect   = "Allow"
        Action   = ["ec2:DescribeImages", "ec2:DescribeSnapshots"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      {
        Sid      = "InspectRegionalEncryptionDefault"
        Effect   = "Allow"
        Action   = "ec2:GetEbsEncryptionByDefault"
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      {
        Sid      = "StartOwnedSnapshot"
        Effect   = "Allow"
        Action   = "ebs:StartSnapshot"
        Resource = local.snapshot_arn
        Condition = {
          StringEquals = { "aws:RequestTag/runs-on-installation" = var.name }
        }
      },
      {
        Sid      = "TagNewOrOwnedSnapshots"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = local.snapshot_arn
        Condition = {
          StringEquals = { "aws:RequestTag/runs-on-installation" = var.name }
        }
      },
      {
        # StartSnapshot authorizes tags against snapshot/* before allocating an
        # ID and supplies no ec2:CreateAction. Direct tagging uses snap-* IDs;
        # deny takeover there. A change to concrete-ID creation auth fails closed.
        Sid      = "PreventSnapshotOwnershipTakeover"
        Effect   = "Deny"
        Action   = "ec2:CreateTags"
        Resource = "arn:aws:ec2:${var.region}::snapshot/snap-*"
        Condition = {
          StringNotEquals = { "aws:ResourceTag/runs-on-installation" = var.name }
        }
      },
      {
        Sid      = "WriteOwnedSnapshot"
        Effect   = "Allow"
        Action   = ["ebs:PutSnapshotBlock", "ebs:CompleteSnapshot"]
        Resource = local.snapshot_arn
        Condition = {
          StringEquals = { "aws:ResourceTag/runs-on-installation" = var.name }
        }
      },
      {
        Sid      = "RegisterOwnedImage"
        Effect   = "Allow"
        Action   = "ec2:RegisterImage"
        Resource = local.image_arn
        Condition = {
          StringEquals = { "aws:RequestTag/runs-on-installation" = var.name }
        }
      },
      {
        Sid      = "RegisterOwnedSnapshotSource"
        Effect   = "Allow"
        Action   = "ec2:RegisterImage"
        Resource = local.snapshot_arn
        Condition = {
          StringEquals = { "aws:ResourceTag/runs-on-installation" = var.name }
        }
      },
      {
        Sid      = "TagImageAtRegistration"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = local.image_arn
        Condition = {
          StringEquals = {
            "ec2:CreateAction"                    = "RegisterImage"
            "aws:RequestTag/runs-on-installation" = var.name
          }
        }
      },
      {
        Sid      = "RetireOwnedImages"
        Effect   = "Allow"
        Action   = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
        Resource = [local.image_arn, local.snapshot_arn]
        Condition = {
          StringEquals = { "aws:ResourceTag/runs-on-installation" = var.name }
        }
      },
    ]
  }
}

resource "aws_iam_role" "publisher" {
  name                 = "${var.name}-image-publisher"
  description          = "Publish and retire AMIs owned by ${var.name}; no deployment privileges."
  assume_role_policy   = jsonencode(local.publisher_trust)
  permissions_boundary = var.workload_boundary_arn
  max_session_duration = 3600
}

resource "aws_iam_policy" "publisher" {
  name   = "${var.name}-image-publisher"
  policy = jsonencode(local.publisher_policy)
}

resource "aws_iam_role_policy_attachment" "publisher" {
  role       = aws_iam_role.publisher.name
  policy_arn = aws_iam_policy.publisher.arn
}
