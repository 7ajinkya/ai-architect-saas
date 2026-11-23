# Graded inputs for the verified cloudwatch-alarms contract.
#
# The platform supplies only the stable name prefix and sibling
# identities. Every threshold and the notification destination are
# explicit human decisions: no default hides an operational choice.

variable "alarm_name_prefix" {
  description = "Stable prefix for all four alarm names."
  type        = string
}

variable "api_id" {
  description = "API Gateway API identifier used in metric dimensions."
  type        = string
}

variable "function_name" {
  description = "Lambda function name used in metric dimensions."
  type        = string
}

variable "api_5xx_error_threshold" {
  description = "API Gateway 5XXError count threshold per period."
  type        = number
}

variable "api_latency_p99_threshold_ms" {
  description = "API Gateway p99 Latency threshold in milliseconds."
  type        = number
}

variable "lambda_error_threshold" {
  description = "Lambda Errors count threshold per period."
  type        = number
}

variable "lambda_duration_p99_threshold_ms" {
  description = "Lambda p99 Duration threshold in milliseconds."
  type        = number
}

variable "period_seconds" {
  description = "Alarm metric period in seconds."
  type        = number
}

variable "evaluation_periods" {
  description = "Number of periods evaluated before alarm state."
  type        = number
}

variable "comparison_operator" {
  description = "Threshold comparison operator."
  type        = string
}

variable "missing_data_treatment" {
  description = "CloudWatch treatment of missing datapoints."
  type        = string
}

variable "notification_target" {
  description = "Either none or an existing same-region SNS topic ARN."
  type        = string
}
