# Managed HTTP entry point (verified demo path).
#
# HTTP API with a $default stage (auto-deploy, access logging to the
# explicit log group), one AWS_PROXY Lambda integration, one $default
# route, and the invoke permission scoped to this API's execution ARN.
# Names derive from the api_name variable; no custom domains,
# authorizers, or usage plans are exposed.

resource "aws_apigatewayv2_api" "this" {
  name          = var.api_name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "this" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.lambda_invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "this" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "$default"
  target    = join("/", ["integrations", aws_apigatewayv2_integration.this.id])
}

resource "aws_apigatewayv2_stage" "this" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = var.log_group_arn
    format          = var.access_log_format
  }
}

resource "aws_lambda_permission" "this" {
  statement_id  = format("%s-invoke", var.api_name)
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_arn
  principal     = "apigateway.amazonaws.com"
  source_arn    = join("/", [aws_apigatewayv2_api.this.execution_arn, "$default", "$default"])
}
