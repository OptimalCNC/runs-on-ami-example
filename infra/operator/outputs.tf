output "user_arn" { value = aws_iam_user.operator.arn }
output "user_name" { value = aws_iam_user.operator.name }
output "controller_role_arn" { value = var.controller_role_arn }
output "policy_arn" { value = aws_iam_policy.assume_controller.arn }
output "policy_json" { value = data.aws_iam_policy_document.assume_controller.json }
