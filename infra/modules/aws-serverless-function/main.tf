# Event-driven compute capability (verified demo path).
#
# Least-privilege execution role (Lambda trust only), one inline
# policy (CloudWatch Logs on the explicit log group ARN; DynamoDB
# read/write on the explicit table ARNs), and the function itself.
# No managed policies, no attachments, no wildcards beyond the
# API Gateway invoke path suffix, which is the standard
# least-privilege form for HTTP APIs.

locals {
  log_group_name = element(split(":", var.log_group_arn), 6)
}

resource "aws_iam_role" "this" {
  name = format("%s-execution-role", var.function_name)
  assume_role_policy = jsonencode({
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
    Version = "2012-10-17"
  })
}

resource "aws_iam_role_policy" "this" {
  name = format("%s-execution-role-policy", var.function_name)
  role = aws_iam_role.this.id
  policy = jsonencode({
    Statement = concat(
      [{
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Effect   = "Allow"
        Resource = ["${var.log_group_arn}:*"]
      }],
      length(var.table_arns) > 0 ? [{
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ]
        Effect   = "Allow"
        Resource = var.table_arns
      }] : []
    )
    Version = "2012-10-17"
  })
}

resource "aws_lambda_function" "this" {
  function_name = var.function_name
  role          = aws_iam_role.this.arn
  handler       = var.handler
  runtime       = var.runtime
  # Reference form: reads the bundled file at plan/apply time, so the
  # hash is always correct by construction. The deterministic renderer
  # instead embeds the base64 of the human-verified hex sha256 (same
  # bytes, verified shape, no filesystem reads at generation time).
  filename         = var.code_artifact_path
  source_code_hash = filebase64sha256(var.code_artifact_path)

  memory_size = var.memory_mb
  timeout     = var.timeout_seconds

  environment {
    variables = {
      TABLE_NAME = var.table_name
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = local.log_group_name
  }
}