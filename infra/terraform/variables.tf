variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "Account ID used to build ARNs (no data source, so the plan works offline)."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "environment" {
  description = "Environment name; used in resource names."
  type        = string
  default     = "demo"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.environment))
    error_message = "environment must be 2-16 lowercase characters."
  }
}

variable "offline_plan" {
  description = "Skip AWS account/credential lookups so `terraform plan` runs without real credentials."
  type        = bool
  default     = false
}

variable "availability_zones" {
  description = "Two AZs (explicit, no data source)."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
  validation {
    condition     = length(var.availability_zones) == 2
    error_message = "Exactly two availability zones are expected."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "enable_nat_gateway" {
  description = <<-EOT
    false (default): Fargate tasks run in public subnets with a public IP for egress but accept
    inbound traffic only from the ALB security group; no NAT cost. true: tasks move to private
    subnets behind one NAT gateway (higher cost, no public IP on tasks).
  EOT
  type        = bool
  default     = false
}

variable "certificate_arn" {
  description = "ACM certificate for the HTTPS listener (TLS terminates at the ALB)."
  type        = string
  validation {
    condition     = can(regex("^arn:aws[a-z-]*:acm:", var.certificate_arn))
    error_message = "certificate_arn must be an ACM certificate ARN."
  }
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs allowed to reach the ALB on 80/443. Narrow this for a private demo."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "image_tag" {
  description = "Immutable image tag (or digest-pinned tag) pushed to the ECR repository."
  type        = string
  default     = "rc"
}

variable "api_cpu" {
  description = "Task CPU units (Fargate)."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Task memory (MiB)."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_engine_version" {
  description = "PostgreSQL major.minor with the pgvector extension available on RDS."
  type        = string
  default     = "17.6"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "enable_otel_collector" {
  description = "Run an ADOT collector sidecar exporting traces to X-Ray and metrics to CloudWatch (EMF)."
  type        = bool
  default     = true
}

variable "otel_collector_image" {
  description = "Pinned AWS Distro for OpenTelemetry collector image."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.3@sha256:8aa9ea5f67b8d318f7d6af24677e3c70f7098bc0631147cb5fa91addbe980b06"
}

variable "app_environment" {
  description = "Non-secret application settings (PROVIDER, models, AGENT_POLICY, GITLAB_BASE_URL, MODEL_PRICING...)."
  type        = map(string)
  default = {
    PROVIDER        = "openai"
    EMBEDDING_MODEL = "text-embedding-3-small"
    ANSWER_MODEL    = "gpt-4.1-mini"
  }
}
