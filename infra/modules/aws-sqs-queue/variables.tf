variable "queue_name" {
  description = "Name of the standard queue."
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "Visibility timeout applied to received messages."
  type        = number
}

variable "message_retention_seconds" {
  description = "How long the queue retains a message."
  type        = number
}
