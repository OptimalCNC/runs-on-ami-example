variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the intended 12-digit AWS account ID."
  }
}

variable "region" { type = string }

variable "repository" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.repository))
    error_message = "repository must be owner/name."
  }
}

variable "environment" {
  type    = string
  default = "ami-build"
}

variable "github_oidc_subject_prefix" {
  type    = string
  default = null
  validation {
    condition = var.github_oidc_subject_prefix == null ? true : (
      can(regex("^repo:[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", var.github_oidc_subject_prefix)) &&
      replace(var.github_oidc_subject_prefix, "/@[0-9]+/", "") == "repo:${var.repository}"
    )
    error_message = "Use this repository's GitHub OIDC subject prefix, optionally including its immutable numeric IDs."
  }
}

variable "name_prefix" {
  type    = string
  default = "ami-example"
  validation {
    condition     = can(regex("^[a-z0-9-]{3,32}$", var.name_prefix))
    error_message = "Use 3..32 lowercase letters, digits or hyphens."
  }
}

variable "artifact_bucket_name" {
  type    = string
  default = null
}

variable "artifact_retention_days" {
  type    = number
  default = 365
  validation {
    condition     = var.artifact_retention_days >= 30
    error_message = "Retain reproducibility inputs and reports for at least 30 days."
  }
}

variable "existing_oidc_provider_arn" {
  type    = string
  default = null
}

variable "existing_controller_role_arn" {
  type    = string
  default = null
}

variable "operator_user_arn" {
  type    = string
  default = null
  validation {
    condition     = var.operator_user_arn == null ? true : can(regex("^arn:aws:iam::${var.account_id}:user/[A-Za-z0-9+=,.@_/-]+$", var.operator_user_arn))
    error_message = "The local operator must be an explicit IAM user in the intended account."
  }
}

variable "existing_builder_profile_name" {
  type    = string
  default = null
}

variable "existing_probe_profile_name" {
  type    = string
  default = null
}

variable "existing_artifact_bucket" {
  type    = string
  default = null
}
