locals {
  ecs_assume = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ecs:${var.region}:${local.account}:*" }
        StringEquals = { "aws:SourceAccount" = local.account }
      }
    }]
  })
  log_group_arns = concat(
    [aws_cloudwatch_log_group.api.arn, aws_cloudwatch_log_group.migrate.arn],
    aws_cloudwatch_log_group.collector[*].arn,
  )
}

# ------------------------------------------------------------ execution role (ECS agent)
# Pulls this one image, writes to these log groups, reads these secrets. Nothing else.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = local.ecs_assume
}

resource "aws_iam_role_policy" "execution" {
  name = "least-privilege"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*" # account-level API without resource-level permissions
      },
      {
        Sid      = "PullThisRepository"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.api.arn
      },
      {
        Sid      = "WriteTaskLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for arn in local.log_group_arns : "${arn}:log-stream:*"]
      },
      {
        Sid    = "ReadInjectedSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = concat(
          [for s in aws_secretsmanager_secret.app : s.arn],
          [for s in aws_secretsmanager_secret.migration : s.arn],
        )
      },
      {
        Sid      = "DecryptWithStackKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = aws_kms_key.main.arn
      },
    ]
  })
}

# ------------------------------------------------------------ task role (application)
# The application calls no AWS API itself. The only grant is for the optional collector
# sidecar: X-Ray trace upload (no resource-level permissions exist) and EMF metrics.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = local.ecs_assume
}

resource "aws_iam_role_policy" "task_collector" {
  count = var.enable_otel_collector ? 1 : 0
  name  = "otel-collector-export"
  role  = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "XRayTraces"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
      {
        Sid      = "EmfMetrics"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = ["${aws_cloudwatch_log_group.metrics[0].arn}:*"]
      },
    ]
  })
}

# Migration task: no AWS permissions at all (its secret is injected by the execution role).
resource "aws_iam_role" "migrate" {
  name               = "${local.name}-migrate"
  assume_role_policy = local.ecs_assume
}

# ------------------------------------------------------------ VPC flow logs
resource "aws_iam_role" "flow_logs" {
  name = "${local.name}-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "write-flow-logs"
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = ["${aws_cloudwatch_log_group.flow.arn}:*"]
    }]
  })
}
