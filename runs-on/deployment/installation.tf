locals {
  tags = {
    "runs-on-installation" = var.name
    "runs-on-stack-name"   = var.name
    "runs-on-environment"  = var.environment
  }
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
  ssm_allowed       = false

  permission_boundary_arn = var.workload_boundary_arn

  runner_max_runtime              = 60
  runner_config_auto_extends_from = ""
  cache_expiration_days           = 1
  app_budget_daily_usd            = 0
  enable_cost_reports             = "no"
  enable_default_dashboard        = false
  enable_stickydisk_isolation     = true

  depends_on = [
    aws_route.public,
    aws_route_table_association.public,
  ]
}
