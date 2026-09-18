locals {
  publisher_policy_arn = "${local.iam_prefix}:policy/${var.name}-image-publisher"

  publisher_boundary_statements = [
    {
      Effect    = "Allow"
      Action    = "ebs:StartSnapshot"
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestTag/runs-on-installation" = var.name, "aws:RequestedRegion" = var.region } }
    },
    {
      Effect   = "Allow"
      Action   = "ec2:CreateTags"
      Resource = "arn:aws:ec2:${var.region}::snapshot/*"
      Condition = {
        StringEquals = {
          "aws:RequestTag/runs-on-installation" = var.name
        }
        ArnEquals = { "aws:PrincipalArn" = "${local.iam_prefix}:role/${var.name}-image-publisher" }
      }
    },
    {
      # EBS authorizes initial tags against snapshot/* before allocating an
      # ID. Standalone tagging uses snap-<id>; a publisher cannot claim an
      # existing snapshot owned by another installation, even if its mutable
      # identity policy is changed by the deployer.
      Effect   = "Deny"
      Action   = "ec2:CreateTags"
      Resource = "arn:aws:ec2:${var.region}::snapshot/snap-*"
      Condition = {
        ArnEquals       = { "aws:PrincipalArn" = "${local.iam_prefix}:role/${var.name}-image-publisher" }
        StringNotEquals = { "aws:ResourceTag/runs-on-installation" = var.name }
      }
    },
    {
      Effect    = "Allow"
      Action    = ["ebs:PutSnapshotBlock", "ebs:CompleteSnapshot", "ec2:DeleteSnapshot", "ec2:DeregisterImage"]
      Resource  = ["arn:aws:ec2:${var.region}::snapshot/*", "arn:aws:ec2:${var.region}::image/*"]
      Condition = { StringEquals = { "aws:ResourceTag/runs-on-installation" = var.name } }
    },
    {
      Effect    = "Allow"
      Action    = "ec2:RegisterImage"
      Resource  = "arn:aws:ec2:${var.region}::image/*"
      Condition = { StringEquals = { "aws:RequestTag/runs-on-installation" = var.name } }
    },
    {
      Effect    = "Allow"
      Action    = "ec2:RegisterImage"
      Resource  = "arn:aws:ec2:${var.region}::snapshot/*"
      Condition = { StringEquals = { "aws:ResourceTag/runs-on-installation" = var.name } }
    },
    {
      Effect    = "Allow"
      Action    = "ec2:CreateTags"
      Resource  = "arn:aws:ec2:${var.region}::image/*"
      Condition = { StringEquals = { "aws:RequestTag/runs-on-installation" = var.name, "ec2:CreateAction" = "RegisterImage" } }
    },
  ]
}
