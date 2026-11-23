variable "log_group_name" {
  description = "Name of the log group, derived from the resource identity."
  type        = string
}

variable "retention_days" {
  description = "Log retention in days, from the closed allowlist."
  type        = number
}
