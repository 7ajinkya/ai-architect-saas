output "api_id" {
  description = "API identifier used by metric-alarm dimensions."
  value       = aws_apigatewayv2_api.this.id
}

output "api_endpoint" {
  description = "Invoke URL of the default stage."
  value       = aws_apigatewayv2_stage.this.invoke_url
}

output "api_execution_arn" {
  description = "Execution ARN constraining invoke permission."
  value       = aws_apigatewayv2_api.this.execution_arn
}
