locals {
  tags = {
    "runs-on-installation" = var.name
    "runs-on-stack-name"   = var.name
    "runs-on-environment"  = var.environment
  }
  app_version       = "v3.3.1"
  bootstrap_version = "v0.1.12"
}

module "runs_on" {
  source  = "runs-on/runs-on/aws//modules/flex"
  version = "3.3.1"

  stack_name          = var.name
  environment         = var.environment
  github_organization = var.github_organization
  license_key         = var.license_key
  email               = var.notification_email
  tags                = local.tags
  cost_allocation_tag = "runs-on-installation"

  vpc_id            = aws_vpc.this.id
  public_subnet_ids = aws_subnet.public[*].id
  private_mode      = "false"
  ssh_allowed       = false
  ssm_allowed       = false

  permission_boundary_arn = var.workload_boundary_arn
  # Flex uses these identifiers in count/for_each, before resource ARNs exist.
  ebs_encryption_key_id  = "alias/${var.name}-images"
  app_custom_policy_arns = ["arn:aws:iam::${var.account_id}:policy/${var.name}-image-key-use"]
  app_tag                = local.app_version
  bootstrap_tag          = local.bootstrap_version
  app_size               = "small"
  app_capacity_provider  = "fargate"

  log_retention_days              = 7
  runner_max_runtime              = 60
  runner_config_auto_extends_from = ""
  cache_expiration_days           = 1
  force_destroy_buckets           = false
  enable_cost_reports             = "daily"
  app_budget_daily_usd            = 5
  enable_default_dashboard        = false
  enable_waf                      = false
  enable_efs                      = false
  enable_ecr                      = false
  enable_bedrock                  = false
  enable_stickydisk_isolation     = true

  depends_on = [
    aws_route.public,
    aws_route_table_association.public,
    aws_kms_alias.images,
    aws_iam_policy.image_key_use,
  ]
}
