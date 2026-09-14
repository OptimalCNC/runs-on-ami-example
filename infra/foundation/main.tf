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
  bucket         = coalesce(var.existing_artifact_bucket, var.artifact_bucket_name, "${var.name_prefix}-${var.account_id}-${var.region}-artifacts")
  bucket_arn     = "arn:aws:s3:::${local.bucket}"
  ec2_arn        = "arn:aws:ec2:${var.region}:${var.account_id}"
  oidc_arn       = var.existing_oidc_provider_arn != null ? var.existing_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
  controller_arn = var.existing_controller_role_arn != null ? var.existing_controller_role_arn : aws_iam_role.controller[0].arn
  github_subject = "${coalesce(var.github_oidc_subject_prefix, "repo:${var.repository}")}:environment:${var.environment}"
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
      values   = [local.github_subject]
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
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${local.bucket_arn}/${var.repository}/reports/ssm/*" }
  ] })
}

resource "aws_s3_bucket" "artifacts" {
  count         = var.existing_artifact_bucket == null ? 1 : 0
  bucket        = local.bucket
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
    filter { prefix = "${var.repository}/reports/" }
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

data "aws_iam_policy_document" "controller_storage" {
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
}

resource "aws_iam_role_policy" "controller_storage" {
  count  = var.existing_controller_role_arn == null ? 1 : 0
  name   = "artifact-and-deployment-state"
  role   = aws_iam_role.controller[0].name
  policy = data.aws_iam_policy_document.controller_storage.json
}
