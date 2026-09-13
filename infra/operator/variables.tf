variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the intended 12-digit AWS account ID."
  }
}
variable "region" { type = string }
variable "user_name" {
  type    = string
  default = "runs-on-ami-example-operator"
}
variable "controller_role_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", var.controller_role_arn))
    error_message = "The controller must be a named role in the intended AWS account."
  }
}
