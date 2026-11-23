# Centralized log retention (verified demo path).
#
# One group per CloudWatch node; the name derives from the stable
# resource identity, retention is a human cost decision from a closed
# allowlist. No metric filters, alarms, or subscription filters are
# exposed.

resource "aws_cloudwatch_log_group" "this" {
  name              = var.log_group_name
  retention_in_days = var.retention_days
}
