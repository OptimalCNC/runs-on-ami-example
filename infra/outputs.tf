output "account_id" { value = var.foundation.account_id }
output "region" { value = var.foundation.region }
output "repository" { value = var.foundation.repository }
output "foundation" { value = var.foundation }
output "security_group_id" { value = local.security_group }
output "vpc_id" { value = var.vpc_id }
output "subnet_id" { value = var.subnet_id }
output "vpc_cidr" { value = var.vpc_cidr }
output "source_ami_id" { value = var.source_ami_id }
output "controller_ami_id" { value = var.controller_ami_id }
output "instance_type" { value = var.instance_type }
output "builder_instance_type" { value = var.builder_instance_type }
output "root_volume_gib" { value = var.root_volume_gib }
output "parent_root_volume_gib" { value = var.parent_root_volume_gib }
output "controller_policy_json" { value = data.aws_iam_policy_document.controller.json }
output "required_runs_on_common_tag" {
  value = { "ami-example:runs-on-repository" = var.foundation.repository }
}
