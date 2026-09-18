variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type = string
}

variable "trusted_principal_arns" {
  description = "Existing IAM user or role ARNs allowed to assume the dedicated deployment role."
  type        = list(string)
}
