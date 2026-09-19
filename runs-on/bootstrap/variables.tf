variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "aws_profile" {
  description = "Optional AWS profile for the existing authorized identity."
  type        = string
  default     = null
}

variable "name" {
  type = string
}

variable "trusted_principal_arns" {
  description = "Existing IAM user or role ARNs allowed to assume the dedicated deployment role."
  type        = list(string)
}
