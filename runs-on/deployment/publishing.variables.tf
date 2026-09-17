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
    condition     = length(var.publisher_principal_arns) > 0 || length(var.publisher_github_repositories) > 0
    error_message = "Configure at least one publisher principal or a GitHub publishing repository."
  }
}

variable "publisher_github_repositories" {
  description = "Exact GitHub repository/environment pairs and resolved OIDC subject prefixes allowed to publish."
  type = list(object({
    repository     = string
    environment    = optional(string, "image-publish")
    subject_prefix = string
  }))
  default  = []
  nullable = false
  validation {
    condition = alltrue([
      for publisher in var.publisher_github_repositories : can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", publisher.repository))
    ])
    error_message = "Each GitHub publisher repository must have the form owner/repository."
  }
  validation {
    condition = alltrue([
      for publisher in var.publisher_github_repositories : can(regex("^[A-Za-z0-9_.-]+$", publisher.environment))
    ])
    error_message = "Each GitHub publisher environment must contain letters, digits, underscores, dots or hyphens."
  }
  validation {
    condition = alltrue([
      for publisher in var.publisher_github_repositories : (
        can(regex("^repo:[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", publisher.subject_prefix)) &&
        replace(publisher.subject_prefix, "/@[0-9]+/", "") == "repo:${publisher.repository}"
      )
    ])
    error_message = "Each GitHub publisher requires its repository's OIDC subject prefix, obtained from GitHub."
  }
  validation {
    condition = length(distinct([
      for publisher in var.publisher_github_repositories : "${publisher.repository}:${publisher.environment}"
    ])) == length(var.publisher_github_repositories)
    error_message = "GitHub publisher repository/environment pairs must be unique."
  }
}
