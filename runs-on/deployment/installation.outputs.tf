locals {
  installation_contract = {
    schema_version      = 1
    kind                = "runs-on-installation"
    name                = var.name
    account_id          = var.account_id
    region              = var.region
    environment         = var.environment
    github_organization = var.github_organization
    setup_url           = module.runs_on.ingress.url
    versions = {
      terraform_module = "3.3.1"
      app              = local.app_version
      bootstrap        = local.bootstrap_version
    }
    network = {
      vpc_id             = aws_vpc.this.id
      public_subnet_ids  = aws_subnet.public[*].id
      security_group_ids = module.runs_on.platform.networking.security_group_ids
    }
    runtime = {
      cluster_name               = module.runs_on.runtime.cluster_name
      service_name               = module.runs_on.runtime.service_name
      task_role_arn              = module.runs_on.runtime.task_role_arn
      runner_role_arn            = module.runs_on.platform.runner_iam.role_arn
      runner_profile_arn         = module.runs_on.platform.runner_iam.profile_arn
      runner_max_runtime_minutes = 60
    }
  }
}

output "installation" {
  description = "Nonsecret RunsOn setup and operator information."
  value       = local.installation_contract
}

output "installation_yaml" {
  description = "Export to .local/contracts/installation.yaml after successful deployment."
  value       = yamlencode(local.installation_contract)
}
