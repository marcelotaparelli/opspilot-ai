resource "aws_cloudwatch_log_group" "api" {
  name              = "${local.log_prefix}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "${local.log_prefix}/migrate"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "collector" {
  count             = var.enable_otel_collector ? 1 : 0
  name              = "${local.log_prefix}/otel-collector"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# Destination of OTel metrics exported by the collector (CloudWatch EMF).
resource "aws_cloudwatch_log_group" "metrics" {
  count             = var.enable_otel_collector ? 1 : 0
  name              = "${local.log_prefix}/metrics"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "flow" {
  name              = "${local.log_prefix}/vpc-flow-rejects"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
