resource "aws_sns_topic" "this" {
  name = var.topic_name
}

resource "aws_sns_topic_subscription" "queue" {
  topic_arn            = aws_sns_topic.this.arn
  protocol             = "sqs"
  endpoint             = var.subscriber_queue_arn
  raw_message_delivery = true
}

data "aws_iam_policy_document" "queue" {
  statement {
    effect = "Allow"

    actions   = ["sqs:SendMessage"]
    resources = [var.subscriber_queue_arn]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.this.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "subscriber" {
  queue_url = var.subscriber_queue_url
  policy    = data.aws_iam_policy_document.queue.json
}
