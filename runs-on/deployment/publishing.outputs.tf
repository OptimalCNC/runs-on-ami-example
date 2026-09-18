output "publishing" {
  description = "Nonsecret account, publisher role, and ownership settings for image publishing."
  value = {
    name               = var.name
    account_id         = var.account_id
    region             = var.region
    publisher_role_arn = aws_iam_role.publisher.arn
    required_tags      = { "runs-on-installation" = var.name }
  }
}
