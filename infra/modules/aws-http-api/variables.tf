# Graded inputs for the verified http-api contract.
#
# api_name is derived from the stable resource identity. Lambda and
# log-group ARNs refer to resources declared in the same configuration;
# the renderer wires them deterministically and refuses ambiguous
# topologies instead of guessing.

variable "api_name" {
  description = "Name of the HTTP API, derived from the resource identity."
  type        = string
}

variable "lambda_invoke_arn" {
  description = "Invoke ARN of the single Lambda integration target."
  type        = string
}

variable "lambda_arn" {
  description = "ARN of the single Lambda function granted invocation."
  type        = string
}

variable "log_group_arn" {
  description = "ARN of the single log group receiving access logs."
  type        = string
}

variable "access_log_format" {
  description = "Static access-log format string. No interpolation."
  type        = string
}
