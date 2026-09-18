output "installation" {
  description = "Nonsecret RunsOn setup and operator information."
  value = {
    name                = var.name
    account_id          = var.account_id
    region              = var.region
    environment         = var.environment
    github_organization = var.github_organization
    setup_url           = module.runs_on.ingress.url
  }
}
