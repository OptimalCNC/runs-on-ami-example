variable "publisher_principal_arns" {
  description = "Existing IAM users or roles allowed to assume the image publisher role."
  type        = list(string)
  default     = []
}

variable "publisher_github_repositories" {
  description = "Exact GitHub repository/environment pairs and resolved OIDC subject prefixes allowed to publish."
  type = list(object({
    repository     = string
    environment    = optional(string, "image-publish")
    subject_prefix = string
  }))
  default = []
}
