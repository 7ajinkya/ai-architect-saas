variable "user_pool_name" {
  description = "Name of the Cognito user pool."
  type        = string
}

variable "policy_minimum_length" {
  description = "Minimum password length enforced by the user pool."
  type        = number
}

variable "mfa_configuration" {
  description = "MFA posture: OFF or OPTIONAL (TOTP second factor)."
  type        = string

  validation {
    condition     = contains(["OFF", "OPTIONAL"], var.mfa_configuration)
    error_message = "mfa_configuration must be OFF or OPTIONAL."
  }
}

variable "self_sign_up_enabled" {
  description = "Whether users may create their own accounts."
  type        = bool
}
