variable "foundation" {
  type = object({
    account_id              = string
    region                  = string
    repository              = string
    environment             = string
    name_prefix             = string
    controller_role_arn     = string
    controller_role_managed = bool
    builder_profile_name    = string
    probe_profile_name      = string
    management_role_arns = object({
      builder = string
      probe   = string
    })
    management_profile_arns = object({
      builder = string
      probe   = string
    })
    artifact_bucket   = string
    oidc_provider_arn = string
    ebs_key_arn       = string
  })
  description = "Unwrapped outputs from the selected foundation Terraform root."
  validation {
    condition = (
      can(regex("^[0-9]{12}$", var.foundation.account_id)) &&
      can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.foundation.repository)) &&
      can(regex("^arn:aws:iam::${var.foundation.account_id}:role/.+$", var.foundation.controller_role_arn)) &&
      alltrue([for arn in values(var.foundation.management_role_arns) : can(regex("^arn:aws:iam::${var.foundation.account_id}:role/.+$", arn))]) &&
      alltrue([for arn in values(var.foundation.management_profile_arns) : can(regex("^arn:aws:iam::${var.foundation.account_id}:instance-profile/.+$", arn))]) &&
      can(regex("^arn:aws:iam::${var.foundation.account_id}:oidc-provider/token[.]actions[.]githubusercontent[.]com$", var.foundation.oidc_provider_arn)) &&
      can(regex("^arn:aws:kms:${var.foundation.region}:${var.foundation.account_id}:key/.+$", var.foundation.ebs_key_arn))
    )
    error_message = "Foundation outputs must identify one repository and AWS account/region with matching resource ARNs."
  }
}

variable "vpc_id" { type = string }

variable "subnet_id" { type = string }

variable "vpc_cidr" { type = string }

variable "source_ami_id" { type = string }

variable "controller_ami_id" { type = string }

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "builder_instance_type" {
  type    = string
  default = "c7i.large"
}

variable "root_volume_gib" {
  type    = number
  default = 80
  validation {
    condition     = var.root_volume_gib >= 8 && var.root_volume_gib <= 256 && floor(var.root_volume_gib) == var.root_volume_gib
    error_message = "Select an integer root volume size from 8 through 256 GiB, at least as large as each selected AMI."
  }
}

variable "existing_security_group_id" {
  type    = string
  default = null
}
