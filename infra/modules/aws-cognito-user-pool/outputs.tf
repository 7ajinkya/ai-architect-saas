output "user_pool_id" {
  description = "Identifier of the user pool."
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "ARN of the user pool."
  value       = aws_cognito_user_pool.this.arn
}

output "user_pool_endpoint" {
  description = "Endpoint of the user pool."
  value       = aws_cognito_user_pool.this.endpoint
}

output "client_id" {
  description = "Identifier of the public application client."
  value       = aws_cognito_user_pool_client.this.id
}

output "issuer_url" {
  description = "OIDC issuer URL of the user pool."
  value       = "https://${aws_cognito_user_pool.this.endpoint}"
}
