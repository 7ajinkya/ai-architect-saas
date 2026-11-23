output "alarm_arns" {
  description = "ARNs of the four created alarms."
  value = [
    aws_cloudwatch_metric_alarm.api_5xx.arn,
    aws_cloudwatch_metric_alarm.api_latency.arn,
    aws_cloudwatch_metric_alarm.lambda_errors.arn,
    aws_cloudwatch_metric_alarm.lambda_duration.arn,
  ]
}
