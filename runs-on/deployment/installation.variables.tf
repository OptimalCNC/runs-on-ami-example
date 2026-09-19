variable "account_id" {
  description = "AWS account that owns the installation and published images."
  type        = string
}

variable "region" {
  description = "AWS region for the installation and image publication."
  type        = string
}

variable "aws_profile" {
  description = "Optional AWS profile for the existing authorized identity."
  type        = string
  default     = null
}

variable "name" {
  description = "Unique installation name, also used to scope IAM permissions."
  type        = string
}

variable "environment" {
  description = "RunsOn env label used to select this installation."
  type        = string
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
}

variable "workload_boundary_arn" {
  description = "Permissions boundary exported by bootstrap and attached to every workload role."
  type        = string
}

variable "vpc_cidr" {
  description = "IPv4 CIDR for the dedicated VPC; split into two public subnets."
  type        = string
  default     = "10.80.0.0/16"
}
