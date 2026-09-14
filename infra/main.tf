terraform {
  required_version = "= 1.16.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.10.0"
    }
  }
}

provider "aws" {
  region              = var.foundation.region
  allowed_account_ids = [var.foundation.account_id]
  default_tags {
    tags = { "ami-example:owner" = var.foundation.repository, "ami-example:purpose" = "infrastructure" }
  }
}

locals {
  bucket_arn     = "arn:aws:s3:::${var.foundation.artifact_bucket}"
  ec2_arn        = "arn:aws:ec2:${var.foundation.region}:${var.foundation.account_id}"
  image_arn      = "arn:aws:ec2:${var.foundation.region}::image"
  snapshot_arn   = "arn:aws:ec2:${var.foundation.region}::snapshot"
  ssm_arn        = "arn:aws:ssm:${var.foundation.region}:${var.foundation.account_id}"
  security_group = var.existing_security_group_id != null ? var.existing_security_group_id : aws_security_group.management[0].id
  profile_roles  = var.foundation.management_role_arns
  profile_arns   = var.foundation.management_profile_arns
}

resource "aws_security_group" "management" {
  count       = var.existing_security_group_id == null ? 1 : 0
  name_prefix = "${var.foundation.name_prefix}-management-"
  description = "SSM management with no inbound access"
  vpc_id      = var.vpc_id
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

data "aws_iam_policy_document" "controller" {
  statement {
    sid = "ReadRegionalImageAndManagementState"
    actions = [
      "ec2:DescribeImages", "ec2:DescribeSnapshots", "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus", "ec2:DescribeInstanceTypes", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
      "ec2:DescribeInstanceCreditSpecifications",
      "ec2:DescribeVolumes", "ec2:DescribeKeyPairs", "ec2:DescribeRegions", "ec2:DescribeVpcs", "ec2:DescribeTags",
      "ec2:DescribeNetworkInterfaces", "ec2:DescribeAvailabilityZones", "ec2:DescribeRouteTables", "ec2:DescribeVpcEndpoints",
      "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.foundation.region]
    }
  }
  statement {
    sid       = "ReadLockedParentAttributes"
    actions   = ["ec2:DescribeImageAttribute"]
    resources = ["${local.image_arn}/${var.source_ami_id}", "${local.image_arn}/${var.controller_ami_id}"]
  }
  statement {
    sid       = "ReadOwnedResourceAttributes"
    actions   = ["ec2:DescribeImageAttribute", "ec2:DescribeInstanceAttribute"]
    resources = ["${local.image_arn}/*", "${local.ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    sid       = "LaunchLockedParents"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.image_arn}/${var.source_ami_id}", "${local.image_arn}/${var.controller_ami_id}"]
  }
  statement {
    sid       = "LaunchOwnedCandidateImages"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.image_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:Owner"
      values   = [var.foundation.account_id]
    }
  }
  statement {
    sid       = "UseConfiguredLaunchNetwork"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:subnet/${var.subnet_id}", "${local.ec2_arn}:security-group/${local.security_group}"]
  }
  statement {
    sid       = "CreateInterfacesInConfiguredSubnet"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:network-interface/*"]
    condition {
      test     = "ArnEquals"
      variable = "ec2:Subnet"
      values   = ["${local.ec2_arn}:subnet/${var.subnet_id}"]
    }
  }
  statement {
    sid       = "UseOwnedTemporaryKeys"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:key-pair/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  dynamic "statement" {
    for_each = { builder = var.builder_instance_type, probe = var.instance_type }
    content {
      sid       = "CreateTagged${title(statement.key)}Instances"
      actions   = ["ec2:RunInstances"]
      resources = ["${local.ec2_arn}:instance/*"]
      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/ami-example:owner"
        values   = [var.foundation.repository]
      }
      condition {
        test     = "StringEquals"
        variable = "aws:RequestTag/ami-example:purpose"
        values   = [statement.key]
      }
      condition {
        test     = "StringEquals"
        variable = "ec2:InstanceType"
        values   = [statement.value]
      }
      condition {
        test     = "StringEquals"
        variable = "ec2:InstanceMarketType"
        values   = ["on-demand"]
      }
      condition {
        test     = "StringEquals"
        variable = "ec2:MetadataHttpTokens"
        values   = ["required"]
      }
      condition {
        test     = "ArnEquals"
        variable = "ec2:InstanceProfile"
        values   = [local.profile_arns[statement.key]]
      }
    }
  }
  statement {
    sid       = "CreateTaggedEncryptedVolumes"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
    condition {
      test     = "Bool"
      variable = "ec2:Encrypted"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:VolumeType"
      values   = ["gp3"]
    }
    condition {
      test     = "NumericLessThanEquals"
      variable = "ec2:VolumeSize"
      values   = [tostring(var.root_volume_gib)]
    }
  }
  statement {
    sid       = "CreateTaggedTemporaryKey"
    actions   = ["ec2:CreateKeyPair"]
    resources = ["${local.ec2_arn}:key-pair/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    sid     = "TagDuringCreation"
    actions = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*", "${local.snapshot_arn}/*",
    "${local.ec2_arn}:network-interface/*", "${local.ec2_arn}:key-pair/*", "${local.image_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateImage", "CreateKeyPair"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    sid       = "CreateTaggedImageAndSnapshots"
    actions   = ["ec2:CreateImage"]
    resources = ["${local.image_arn}/*", "${local.snapshot_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    sid = "ManageOwnedResources"
    actions = ["ec2:TerminateInstances", "ec2:StopInstances", "ec2:CreateImage",
      "ec2:DeregisterImage", "ec2:DeleteSnapshot", "ec2:DeleteVolume",
    "ec2:DeleteKeyPair", "ec2:GetConsoleOutput"]
    resources = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*", "${local.snapshot_arn}/*",
    "${local.ec2_arn}:key-pair/*", "${local.image_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    sid     = "UpdateOwnedResourceTags"
    actions = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*", "${local.snapshot_arn}/*",
    "${local.ec2_arn}:key-pair/*", "${local.image_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
    condition {
      test     = "StringEqualsIfExists"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["Name", "ami-example:owner", "ami-example:build-id", "ami-example:purpose", "ami-example:expires-at", "ami-example:recipe-id", "ami-example:retain"]
    }
  }
  statement {
    sid       = "SetOwnedImageDescription"
    actions   = ["ec2:ModifyImageAttribute"]
    resources = ["${local.image_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:Attribute"
      values   = ["description"]
    }
  }
  statement {
    sid       = "AdoptMarkedRunsOnTestInstances"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/ami-example:runs-on-repository"
      values   = [var.foundation.repository]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:purpose"
      values   = ["test"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["ami-example:owner", "ami-example:build-id", "ami-example:purpose", "ami-example:expires-at"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    sid       = "PassOnlyManagementRoles"
    actions   = ["iam:PassRole"]
    resources = values(local.profile_roles)
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
  statement {
    actions   = ["iam:GetInstanceProfile"]
    resources = values(local.profile_arns)
  }
  statement {
    sid       = "SSMOwnedInstances"
    actions   = ["ssm:StartSession", "ssm:SendCommand"]
    resources = ["${local.ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/ami-example:owner"
      values   = [var.foundation.repository]
    }
  }
  statement {
    actions   = ["ssm:StartSession"]
    resources = ["arn:aws:ssm:${var.foundation.region}::document/AWS-StartPortForwardingSession"]
  }
  statement {
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.foundation.region}::document/AWS-RunShellScript", local.bucket_arn]
  }
  statement {
    actions   = ["ssm:TerminateSession"]
    resources = ["${local.ssm_arn}:session/ami-example-*"]
  }
  statement {
    actions   = ["kms:DescribeKey"]
    resources = [var.foundation.ebs_key_arn]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncrypt*"]
    resources = [var.foundation.ebs_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ec2.${var.foundation.region}.amazonaws.com"]
    }
  }
  statement {
    actions   = ["kms:CreateGrant"]
    resources = [var.foundation.ebs_key_arn]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "controller" {
  count  = var.foundation.controller_role_managed ? 1 : 0
  name   = "image-management-and-cleanup"
  role   = element(reverse(split("/", var.foundation.controller_role_arn)), 0)
  policy = data.aws_iam_policy_document.controller.json
}
