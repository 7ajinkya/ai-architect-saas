variable "topic_name" {
  description = "Name of the SNS topic."
  type        = string
}

variable "subscriber_queue_arn" {
  description = "ARN of the SQS queue subscribed to the topic."
  type        = string
}

variable "subscriber_queue_url" {
  description = "URL of the SQS queue subscribed to the topic."
  type        = string
}
