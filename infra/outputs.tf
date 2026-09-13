output "controller_role_arn" {
  value = var.existing_controller_role_arn != null ? var.existing_controller_role_arn : aws_iam_role.controller[0].arn
}
output "builder_profile_name" { value = local.profiles.builder }
output "probe_profile_name" { value = local.profiles.probe }
output "management_role_arns" { value = local.profile_roles }
output "management_profile_arns" { value = local.profile_arns }
output "management_trust_json" { value = data.aws_iam_policy_document.ec2_trust.json }
output "management_policy_json" { value = data.aws_iam_policy_document.ssm_agent.json }
output "artifact_bucket" { value = local.bucket }
output "security_group_id" { value = local.security_group }
output "required_runs_on_common_tag" {
  value = { "ami-example:runs-on-repository" = var.repository }
}
output "controller_policy_json" { value = data.aws_iam_policy_document.controller.json }
output "controller_trust_json" { value = data.aws_iam_policy_document.github_trust.json }
