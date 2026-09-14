output "account_id" { value = var.account_id }
output "region" { value = var.region }
output "repository" { value = var.repository }
output "environment" { value = var.environment }
output "name_prefix" { value = var.name_prefix }
output "controller_role_arn" { value = local.controller_arn }
output "controller_role_managed" { value = var.existing_controller_role_arn == null }
output "builder_profile_name" { value = local.profiles.builder }
output "probe_profile_name" { value = local.profiles.probe }
output "management_role_arns" { value = local.profile_roles }
output "management_profile_arns" { value = local.profile_arns }
output "management_trust_json" { value = data.aws_iam_policy_document.ec2_trust.json }
output "management_policy_json" { value = data.aws_iam_policy_document.ssm_agent.json }
output "artifact_bucket" { value = local.bucket }
output "oidc_provider_arn" { value = local.oidc_arn }
output "ebs_key_arn" { value = data.aws_kms_alias.ebs.target_key_arn }
output "controller_storage_policy_json" { value = data.aws_iam_policy_document.controller_storage.json }
output "controller_trust_json" { value = data.aws_iam_policy_document.github_trust.json }
