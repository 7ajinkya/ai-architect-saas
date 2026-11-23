# CloudWatch alarms for one HTTP API and one Lambda (verified demo path).
#
# Exactly four upper-bound alarms: API Gateway 5XX errors and p99
# latency, Lambda errors and p99 duration. Dimensions bind to the
# sibling resources in the same spec. No SNS topic is created: the
# notification target is either "none" or an existing topic ARN.

locals {
  alarm_actions = var.notification_target == "none" ? [] : [var.notification_target]
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name  = "${var.alarm_name_prefix}-api-5xx"
  namespace   = "AWS/ApiGateway"
  metric_name = "5XXError"
  dimensions = {
    ApiId = var.api_id
    Stage = "$default"
  }
  statistic           = "Sum"
  unit                = "Count"
  period              = var.period_seconds
  evaluation_periods  = var.evaluation_periods
  threshold           = var.api_5xx_error_threshold
  comparison_operator = var.comparison_operator
  treat_missing_data  = var.missing_data_treatment
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  alarm_name  = "${var.alarm_name_prefix}-api-latency"
  namespace   = "AWS/ApiGateway"
  metric_name = "Latency"
  dimensions = {
    ApiId = var.api_id
    Stage = "$default"
  }
  extended_statistic  = "p99"
  unit                = "Milliseconds"
  period              = var.period_seconds
  evaluation_periods  = var.evaluation_periods
  threshold           = var.api_latency_p99_threshold_ms
  comparison_operator = var.comparison_operator
  treat_missing_data  = var.missing_data_treatment
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name  = "${var.alarm_name_prefix}-lambda-errors"
  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions = {
    FunctionName = var.function_name
  }
  statistic           = "Sum"
  unit                = "Count"
  period              = var.period_seconds
  evaluation_periods  = var.evaluation_periods
  threshold           = var.lambda_error_threshold
  comparison_operator = var.comparison_operator
  treat_missing_data  = var.missing_data_treatment
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  alarm_name  = "${var.alarm_name_prefix}-lambda-duration"
  namespace   = "AWS/Lambda"
  metric_name = "Duration"
  dimensions = {
    FunctionName = var.function_name
  }
  extended_statistic  = "p99"
  unit                = "Milliseconds"
  period              = var.period_seconds
  evaluation_periods  = var.evaluation_periods
  threshold           = var.lambda_duration_p99_threshold_ms
  comparison_operator = var.comparison_operator
  treat_missing_data  = var.missing_data_treatment
  alarm_actions       = local.alarm_actions
}
