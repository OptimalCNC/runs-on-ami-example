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
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { "ami-example:owner" = var.repository, "ami-example:purpose" = "infrastructure" }
  }
}

locals {
  bucket         = coalesce(var.existing_artifact_bucket, var.artifact_bucket_name)
  bucket_arn     = "arn:aws:s3:::${local.bucket}"
  ec2_arn        = "arn:aws:ec2:${var.region}:${var.account_id}"
  image_arn      = "arn:aws:ec2:${var.region}::image"
  snapshot_arn   = "arn:aws:ec2:${var.region}::snapshot"
  ssm_arn        = "arn:aws:ssm:${var.region}:${var.account_id}"
  oidc_arn       = var.existing_oidc_provider_arn != null ? var.existing_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
  security_group = var.existing_security_group_id != null ? var.existing_security_group_id : aws_security_group.management[0].id
  existing_profiles = {
    builder = var.existing_builder_profile_name
    probe   = var.existing_probe_profile_name
  }
  profiles      = { for name, existing in local.existing_profiles : name => existing != null ? existing : aws_iam_instance_profile.management[name].name }
  profile_roles = { for name, existing in local.existing_profiles : name => existing != null ? data.aws_iam_instance_profile.existing[name].role_arn : aws_iam_role.management[name].arn }
  profile_arns  = { for name, existing in local.existing_profiles : name => existing != null ? data.aws_iam_instance_profile.existing[name].arn : aws_iam_instance_profile.management[name].arn }
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.existing_oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.repository}:environment:${var.environment}"]
    }
  }
  dynamic "statement" {
    for_each = var.operator_user_arn == null ? [] : [var.operator_user_arn]
    content {
      sid     = "LocalOperator"
      actions = ["sts:AssumeRole"]
      principals {
        type        = "AWS"
        identifiers = [statement.value]
      }
      condition {
        test     = "StringLike"
        variable = "sts:RoleSessionName"
        values   = ["ami-example-*"]
      }
    }
  }
}

resource "aws_iam_role" "controller" {
  count                = var.existing_controller_role_arn == null ? 1 : 0
  name                 = "${var.name_prefix}-controller"
  assume_role_policy   = data.aws_iam_policy_document.github_trust.json
  max_session_duration = 21600
}

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "management" {
  for_each           = { for name, existing in local.existing_profiles : name => existing if existing == null }
  name               = "${var.name_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}
resource "aws_iam_instance_profile" "management" {
  for_each = aws_iam_role.management
  name     = "${var.name_prefix}-${each.key}"
  role     = each.value.name
}
data "aws_iam_instance_profile" "existing" {
  for_each = { for name, existing in local.existing_profiles : name => existing if existing != null }
  name     = each.value
}
data "aws_iam_policy_document" "ssm_agent" {
  statement {
    sid = "RegionalAgentChannels"
    actions = [
      "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
      "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage",
      "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply"
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }
  statement {
    sid       = "RegionalEC2Registration"
    actions   = ["ssm:UpdateInstanceInformation", "ssm:ListInstanceAssociations"]
    resources = ["${local.ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/ami-example:owner"
      values   = [var.repository]
    }
  }
  statement {
    sid       = "ReadRequiredAWSManagedDocuments"
    actions   = ["ssm:GetDocument", "ssm:DescribeDocument"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript", "arn:aws:ssm:${var.region}::document/AWS-StartPortForwardingSession"]
  }
}
resource "aws_iam_role_policy" "ssm_agent" {
  for_each = aws_iam_role.management
  name     = "regional-ssm-agent"
  role     = each.value.name
  policy   = data.aws_iam_policy_document.ssm_agent.json
}
resource "aws_iam_role_policy" "probe_logs" {
  count = var.existing_probe_profile_name == null ? 1 : 0
  name  = "ssm-output-only"
  role  = aws_iam_role.management["probe"].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${local.bucket_arn}/${var.repository}/ssm/*" }
  ] })
}

resource "aws_security_group" "management" {
  count       = var.existing_security_group_id == null ? 1 : 0
  name_prefix = "${var.name_prefix}-management-"
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

resource "aws_s3_bucket" "artifacts" {
  count         = var.existing_artifact_bucket == null ? 1 : 0
  bucket        = var.artifact_bucket_name
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "artifacts" {
  count                   = var.existing_artifact_bucket == null ? 1 : 0
  bucket                  = aws_s3_bucket.artifacts[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "artifacts" {
  count  = var.existing_artifact_bucket == null ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  count  = var.existing_artifact_bucket == null ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  count      = var.existing_artifact_bucket == null ? 1 : 0
  depends_on = [aws_s3_bucket_versioning.artifacts]
  bucket     = aws_s3_bucket.artifacts[0].id
  rule {
    id     = "retained-build-inputs-and-reports"
    status = "Enabled"
    filter { prefix = "${var.repository}/" }
    expiration { days = var.artifact_retention_days }
    noncurrent_version_expiration { noncurrent_days = var.artifact_retention_days }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_policy" "artifacts" {
  count  = var.existing_artifact_bucket == null ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [local.bucket_arn, "${local.bucket_arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } } }
  ] })
}

data "aws_kms_alias" "ebs" { name = "alias/aws/ebs" }

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
      values   = [var.region]
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
      values   = [var.repository]
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
      values   = [var.repository]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:Owner"
      values   = [var.account_id]
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
      values   = [var.repository]
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
        values   = [var.repository]
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
      values   = [var.repository]
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
      values   = [var.repository]
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
      values   = [var.repository]
    }
  }
  statement {
    sid       = "CreateTaggedImageAndSnapshots"
    actions   = ["ec2:CreateImage"]
    resources = ["${local.image_arn}/*", "${local.snapshot_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.repository]
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
      values   = [var.repository]
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
      values   = [var.repository]
    }
    condition {
      test     = "StringEqualsIfExists"
      variable = "aws:RequestTag/ami-example:owner"
      values   = [var.repository]
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
      values   = [var.repository]
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
      values   = [var.repository]
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
      values   = [var.repository]
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
      values   = [var.repository]
    }
  }
  statement {
    actions   = ["ssm:StartSession"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-StartPortForwardingSession"]
  }
  statement {
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript", local.bucket_arn]
  }
  statement {
    actions   = ["ssm:TerminateSession"]
    resources = ["${local.ssm_arn}:session/ami-example-*"]
  }
  statement {
    actions   = ["s3:GetBucketLocation", "s3:GetBucketVersioning"]
    resources = [local.bucket_arn]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.repository}/*"]
    }
  }
  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${local.bucket_arn}/${var.repository}/*"]
  }
  statement {
    actions   = ["kms:DescribeKey"]
    resources = [data.aws_kms_alias.ebs.target_key_arn]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKeyWithoutPlaintext", "kms:ReEncrypt*"]
    resources = [data.aws_kms_alias.ebs.target_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ec2.${var.region}.amazonaws.com"]
    }
  }
  statement {
    actions   = ["kms:CreateGrant"]
    resources = [data.aws_kms_alias.ebs.target_key_arn]
    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "controller" {
  count  = var.existing_controller_role_arn == null ? 1 : 0
  name   = "image-management-and-cleanup"
  role   = aws_iam_role.controller[0].name
  policy = data.aws_iam_policy_document.controller.json
}
