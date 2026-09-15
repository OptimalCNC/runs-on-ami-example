variable "account_id" {
  description = "AWS account that owns the installation and published images."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region for the installation and image publication."
  type        = string
}

variable "name" {
  description = "Unique installation name, also used to scope IAM permissions."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name))
    error_message = "name must contain 3 to 24 lowercase letters, digits or hyphens, beginning with a letter."
  }
}

variable "environment" {
  description = "RunsOn env label used to select this installation."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must begin with a lowercase letter and contain lowercase letters, digits or hyphens."
  }
}

variable "github_organization" {
  description = "GitHub organization or personal account installing the GitHub App."
  type        = string
}

variable "license_key" {
  description = "RunsOn license; supplied through TF_VAR_license_key."
  type        = string
  sensitive   = true
}

variable "notification_email" {
  description = "Notification address; its SNS subscription requires email confirmation."
  type        = string
  sensitive   = true
}

variable "deployment_role_arn" {
  description = "Deployment role exported by bootstrap."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", var.deployment_role_arn))
    error_message = "deployment_role_arn must be an IAM role in account_id."
  }
}

variable "workload_boundary_arn" {
  description = "Permissions boundary exported by bootstrap and attached to every workload role."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:policy/[A-Za-z0-9+=,.@_/-]+$", var.workload_boundary_arn))
    error_message = "workload_boundary_arn must be an IAM policy in account_id."
  }
}

variable "publisher_principal_arns" {
  description = "Existing IAM users or roles allowed to assume the image publisher role."
  type        = list(string)
  default     = []
  validation {
    condition = alltrue([
      for arn in var.publisher_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "publisher_principal_arns must contain IAM user or role ARNs."
  }
  validation {
    condition     = length(var.publisher_principal_arns) > 0 || var.publisher_github_repository != ""
    error_message = "Configure at least one publisher principal or a GitHub publishing repository."
  }
}

variable "publisher_github_repository" {
  description = "Optional exact owner/repository authorized through GitHub OIDC."
  type        = string
  default     = ""
  validation {
    condition     = var.publisher_github_repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.publisher_github_repository))
    error_message = "publisher_github_repository must be empty or owner/repository."
  }
}

variable "publisher_github_environment" {
  description = "GitHub environment whose OIDC subject can assume the publisher role."
  type        = string
  default     = "image-publish"
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+$", var.publisher_github_environment))
    error_message = "publisher_github_environment must contain letters, digits, underscores, dots or hyphens."
  }
}

variable "publisher_github_subject_prefix" {
  description = "GitHub repository OIDC subject prefix discovered by the installer, including immutable IDs when GitHub uses them."
  type        = string
  default     = ""
  validation {
    condition = var.publisher_github_repository == "" || (
      can(regex("^repo:[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", var.publisher_github_subject_prefix)) &&
      replace(var.publisher_github_subject_prefix, "/@[0-9]+/", "") == "repo:${var.publisher_github_repository}"
    )
    error_message = "GitHub publishing requires the selected repository's OIDC subject prefix, obtained from GitHub."
  }
}

variable "existing_github_oidc_provider_arn" {
  description = "Account-wide GitHub OIDC provider prepared by the installer before deployment."
  type        = string
  default     = ""
  validation {
    condition     = var.existing_github_oidc_provider_arn == "" || var.existing_github_oidc_provider_arn == "arn:aws:iam::${var.account_id}:oidc-provider/token.actions.githubusercontent.com"
    error_message = "existing_github_oidc_provider_arn must identify this account's GitHub Actions OIDC provider."
  }
  validation {
    condition     = var.publisher_github_repository == "" || var.existing_github_oidc_provider_arn != ""
    error_message = "GitHub publishing requires its account-wide OIDC provider ARN from installation prerequisites."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR for the dedicated VPC; split into two public subnets."
  type        = string
  default     = "10.80.0.0/16"
  validation {
    condition     = can(cidrsubnet(var.vpc_cidr, 1, 1)) && can(regex("^[0-9.]+/(1[6-9]|2[0-7])$", var.vpc_cidr))
    error_message = "vpc_cidr must be an IPv4 CIDR with prefix length 16 through 27."
  }
}
