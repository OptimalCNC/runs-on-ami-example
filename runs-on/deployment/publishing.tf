locals {
  snapshot_arn = "arn:aws:ec2:${var.region}::snapshot/*"
  image_arn    = "arn:aws:ec2:${var.region}::image/*"

  publisher_trust = {
    Version = "2012-10-17"
    Statement = concat(
      length(var.publisher_principal_arns) > 0 ? [{
        Sid       = "AuthorizedLocalPublishers"
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { AWS = var.publisher_principal_arns }
      }] : [],
      length(var.publisher_github_repositories) > 0 ? [{
        Sid       = "AuthorizedGitHubEnvironment"
        Effect    = "Allow"
        Action    = "sts:AssumeRoleWithWebIdentity"
        Principal = { Federated = "arn:aws:iam::${var.account_id}:oidc-provider/token.actions.githubusercontent.com" }
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = [for publisher in var.publisher_github_repositories : "${publisher.subject_prefix}:environment:${publisher.environment}"]
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
        Action   = ["ec2:DescribeImages", "ec2:DescribeSnapshots", "ec2:GetEbsEncryptionByDefault"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      {
        Sid      = "StartOwnedSnapshot"
        Effect   = "Allow"
        Action   = ["ebs:StartSnapshot", "ec2:CreateTags"]
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
}

resource "aws_iam_policy" "publisher" {
  name   = "${var.name}-image-publisher"
  policy = jsonencode(local.publisher_policy)
}

resource "aws_iam_role_policy_attachment" "publisher" {
  role       = aws_iam_role.publisher.name
  policy_arn = aws_iam_policy.publisher.arn
}
