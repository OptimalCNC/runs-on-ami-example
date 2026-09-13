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
    tags = { "ami-example:project" = "runs-on-ami-example", "ami-example:purpose" = "operator" }
  }
}

data "aws_iam_policy_document" "assume_controller" {
  statement {
    sid       = "AssumeOnlyImageController"
    actions   = ["sts:AssumeRole"]
    resources = [var.controller_role_arn]
    condition {
      test     = "StringLike"
      variable = "sts:RoleSessionName"
      values   = ["ami-example-*"]
    }
  }
}

resource "aws_iam_policy" "assume_controller" {
  name   = "${var.user_name}-assume-controller"
  policy = data.aws_iam_policy_document.assume_controller.json
}

resource "aws_iam_user" "operator" {
  name                 = var.user_name
  path                 = "/"
  force_destroy        = false
  permissions_boundary = aws_iam_policy.assume_controller.arn
}

resource "aws_iam_user_policy_attachment" "assume_controller" {
  user       = aws_iam_user.operator.name
  policy_arn = aws_iam_policy.assume_controller.arn
}
