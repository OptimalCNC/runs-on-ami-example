variable "account_id" {
  type = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the 12-digit destination AWS account ID."
  }
}

variable "region" {
  type = string

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.region))
    error_message = "region must be an AWS commercial-region identifier."
  }
}

variable "name" {
  type = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name))
    error_message = "name must be 3-24 lowercase letters, digits, or hyphens, starting with a letter."
  }
}

variable "trusted_principal_arns" {
  description = "Existing IAM user or role ARNs allowed to assume the dedicated deployment role."
  type        = list(string)

  validation {
    condition = length(var.trusted_principal_arns) > 0 && alltrue([
      for arn in var.trusted_principal_arns : can(regex("^arn:aws:iam::${var.account_id}:(user|role)/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "trusted_principal_arns must contain existing IAM users or roles in account_id; STS session ARNs are not accepted."
  }
}
