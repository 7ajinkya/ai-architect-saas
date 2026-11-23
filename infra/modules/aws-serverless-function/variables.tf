# Graded inputs for the verified serverless-function contract.
#
# function_name binds the stable resource identity; code_artifact_path
# and code_artifact_sha256 are the human-supplied deployment package
# reference (basename + lowercase hex sha256). table_arns and
# log_group_arn scope the least-privilege inline policy; both refer to
# resources declared in the same configuration.

variable "function_name" {
  description = "Name of the function."
  type        = string
}

variable "code_artifact_path" {
  description = "Basename of the deployment zip carried next to the generated tree. Never inline code."
  type        = string
}

variable "code_artifact_sha256" {
  description = "Lowercase hex sha256 of the deployment zip."
  type        = string
}

variable "runtime" {
  description = "Lambda runtime identifier."
  type        = string
}

variable "handler" {
  description = "Entry point within the artifact."
  type        = string
}

variable "memory_mb" {
  description = "Memory allocated to the function, in megabytes."
  type        = number
}

variable "timeout_seconds" {
  description = "Maximum execution duration, in seconds."
  type        = number
}

variable "table_arns" {
  description = "DynamoDB table ARNs the function may read and write. Empty for logs-only functions."
  type        = list(string)
  default     = []
}

variable "table_name" {
  description = "Table name exposed as TABLE_NAME. Empty when the function uses no table."
  type        = string
  default     = ""
}

variable "log_group_arn" {
  description = "CloudWatch log group ARN for function logs."
  type        = string
}
