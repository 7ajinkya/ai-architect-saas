variable "bucket_name" {
  description = "Globally unique bucket name."
  type        = string
}

variable "versioning_status" {
  description = "Object versioning posture."
  type        = string
}

variable "sse_algorithm" {
  description = "Server-side encryption algorithm applied at rest."
  type        = string
}

variable "access_posture" {
  description = "Named public-access posture: all_blocked or none_blocked."
  type        = string
}