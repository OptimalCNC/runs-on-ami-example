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

  publishing_contract = {
    schema_version     = 1
    kind               = "ami-publishing-target"
    name               = var.name
    account_id         = var.account_id
    region             = var.region
    publisher_role_arn = aws_iam_role.publisher.arn
    authentication = {
      local = {
        method         = "sts-assume-role"
        principal_arns = var.publisher_principal_arns
      }
      github = local.github_publishing ? {
        method       = "github-oidc"
        repository   = var.publisher_github_repository
        environment  = var.publisher_github_environment
        provider_arn = local.github_oidc_provider_arn
        audience     = "sts.amazonaws.com"
        subject      = local.github_oidc_subject
      } : null
    }
    destination = {
      type          = "ec2-ami"
      upload_method = "ebs-direct-api"
      disk_format   = "raw"
      encrypted     = true
      kms_key_arn   = aws_kms_key.images.arn
      required_tags = local.publication_tags
    }
  }
}

output "installation" {
  description = "Nonsecret installation contract for RunsOn execution."
  value       = local.installation_contract
}

output "publishing" {
  description = "Nonsecret destination and authentication contract for image publishing."
  value       = local.publishing_contract
}

output "installation_yaml" {
  description = "Export to .local/contracts/installation.yaml after successful deployment."
  value       = yamlencode(local.installation_contract)
}

output "publishing_yaml" {
  description = "Export to .local/contracts/publishing.yaml after successful deployment."
  value       = yamlencode(local.publishing_contract)
}
