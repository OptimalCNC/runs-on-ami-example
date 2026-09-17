locals {
  publishing_contract = {
    schema_version     = 4
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
        provider_arn = local.github_oidc_provider_arn
        audience     = "sts.amazonaws.com"
        repositories = local.github_publishers
      } : null
    }
    required_tags = local.publication_tags
  }
}

output "publishing" {
  description = "Nonsecret account, authentication, and ownership settings for image publishing."
  value       = local.publishing_contract
}

output "publishing_yaml" {
  description = "Export to .local/contracts/publishing.yaml after successful deployment."
  value       = yamlencode(local.publishing_contract)
}
