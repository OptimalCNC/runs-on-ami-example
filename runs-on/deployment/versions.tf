terraform {
  backend "local" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.45.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]

  assume_role {
    role_arn     = var.deployment_role_arn
    session_name = "runs-on-deployment"
  }

  default_tags {
    tags = local.tags
  }
}
