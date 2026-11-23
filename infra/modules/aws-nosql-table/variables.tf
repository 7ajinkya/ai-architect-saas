variable "table_name" {
  description = "Name of the table."
  type        = string
}

variable "hash_key_name" {
  description = "Attribute name of the partition key."
  type        = string
}

variable "hash_key_type" {
  description = "Scalar type of the partition key."
  type        = string
}

variable "range_key_name" {
  description = "Attribute name of the sort key. Null when absent."
  type        = string
  default     = null
}

variable "range_key_type" {
  description = "Scalar type of the sort key. Null when absent."
  type        = string
  default     = null
}

variable "billing_mode" {
  description = "Capacity mode. Only on-demand is exposed by this contract."
  type        = string
}

variable "point_in_time_recovery" {
  description = "Whether point-in-time recovery is enabled."
  type        = bool
}