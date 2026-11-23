output "function_arn" {
  description = "ARN of the function."
  value       = aws_lambda_function.this.arn
}

output "function_name" {
  description = "Name of the function."
  value       = aws_lambda_function.this.function_name
}

output "role_arn" {
  description = "ARN of the least-privilege execution role."
  value       = aws_iam_role.this.arn
}

output "function_invoke_arn" {
  description = "Invoke ARN of the function, for API Gateway integrations."
  value       = aws_lambda_function.this.invoke_arn
}
