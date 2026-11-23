output "table_id" {
  description = "Identifier of the table."
  value       = aws_dynamodb_table.this.id
}

output "table_arn" {
  description = "ARN of the table."
  value       = aws_dynamodb_table.this.arn
}

output "table_stream_arn" {
  description = "ARN of the table stream."
  value       = aws_dynamodb_table.this.stream_arn
}