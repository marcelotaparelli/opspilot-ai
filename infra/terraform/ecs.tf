locals {
  collector_cpu    = var.enable_otel_collector ? 128 : 0
  collector_memory = var.enable_otel_collector ? 256 : 0
  task_subnets     = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
  image            = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"

  # Destination: traces -> AWS X-Ray, metrics -> CloudWatch (EMF log group). The app sends
  # OTLP/HTTP to the sidecar on localhost only; nothing is exposed outside the task.
  collector_config = yamlencode({
    receivers = { otlp = { protocols = { http = { endpoint = "127.0.0.1:4318" } } } }
    processors = {
      memory_limiter = { check_interval = "1s", limit_mib = 200 }
      batch          = {}
    }
    exporters = {
      awsxray = { region = var.region }
      awsemf = {
        region         = var.region
        namespace      = "OpsPilot"
        log_group_name = "${local.log_prefix}/metrics"
      }
    }
    service = {
      pipelines = {
        traces  = { receivers = ["otlp"], processors = ["memory_limiter", "batch"], exporters = ["awsxray"] }
        metrics = { receivers = ["otlp"], processors = ["memory_limiter", "batch"], exporters = ["awsemf"] }
      }
    }
  })

  log_options = {
    "awslogs-region"        = var.region
    "awslogs-stream-prefix" = "ecs"
    "mode"                  = "non-blocking"
    "max-buffer-size"       = "4m"
  }

  api_container = {
    name                   = "api"
    image                  = local.image
    essential              = true
    user                   = "10001:10001"
    readonlyRootFilesystem = true
    cpu                    = var.api_cpu - local.collector_cpu
    memory                 = var.api_memory - local.collector_memory
    stopTimeout            = 30 # uvicorn drains for up to 15 s after SIGTERM
    linuxParameters        = { initProcessEnabled = true, capabilities = { drop = ["ALL"] } }
    portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    environment = [
      for k, v in merge(
        var.app_environment,
        { APP_ENV = "production" },
        var.enable_otel_collector ? { OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4318" } : {},
      ) : { name = k, value = v }
    ]
    secrets = [for k, s in aws_secretsmanager_secret.app : { name = k, valueFrom = s.arn }]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-group" = aws_cloudwatch_log_group.api.name })
    }
    dependsOn = var.enable_otel_collector ? [{ containerName = "otel-collector", condition = "START" }] : []
  }

  collector_container = {
    name                   = "otel-collector"
    image                  = var.otel_collector_image
    user                   = "4317:4317"
    command                = ["--config=env:AOT_CONFIG_CONTENT"]
    essential              = false # telemetry is fail-open: losing it never stops the API
    readonlyRootFilesystem = true
    cpu                    = local.collector_cpu
    memory                 = local.collector_memory
    linuxParameters        = { capabilities = { drop = ["ALL"] } }
    environment            = [{ name = "AOT_CONFIG_CONTENT", value = local.collector_config }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-group" = "${local.log_prefix}/otel-collector" })
    }
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode(concat(
    [local.api_container],
    var.enable_otel_collector ? [local.collector_container] : [],
  ))
}

# One-off schema migration: run explicitly before a deployment (see docs/deployment/aws.md),
# never on API start-up and never once per replica. The advisory lock makes a concurrent
# second run a no-op, and an unknown schema version fails fast.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.migrate.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name                   = "migrate"
    image                  = local.image
    essential              = true
    user                   = "10001:10001"
    readonlyRootFilesystem = true
    command                = ["python", "-m", "opspilot.persistence.migrate"]
    linuxParameters        = { initProcessEnabled = true, capabilities = { drop = ["ALL"] } }
    secrets                = [for k, s in aws_secretsmanager_secret.migration : { name = k, valueFrom = s.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.log_options, { "awslogs-group" = aws_cloudwatch_log_group.migrate.name })
    }
  }])
}

resource "aws_ecs_service" "api" {
  name                              = "${local.name}-api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_desired_count
  launch_type                       = "FARGATE"
  platform_version                  = "1.4.0"
  health_check_grace_period_seconds = 30
  enable_execute_command            = false
  propagate_tags                    = "SERVICE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = local.task_subnets
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = !var.enable_nat_gateway
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https]
}
