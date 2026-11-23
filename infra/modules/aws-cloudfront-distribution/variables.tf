variable "distribution_name" {
  description = "Name used for the distribution and its origin access control."
  type        = string
}

variable "origin_bucket_domain_name" {
  description = "Regional domain name of the S3 origin bucket."
  type        = string
}

variable "origin_bucket_id" {
  description = "Identifier of the S3 origin bucket."
  type        = string
}

variable "origin_bucket_arn" {
  description = "ARN of the S3 origin bucket."
  type        = string
}

variable "default_root_object" {
  description = "Object returned when the root path is requested."
  type        = string
}

variable "price_class" {
  description = "CloudFront price class: PriceClass_100, PriceClass_200 or PriceClass_All."
  type        = string
}
