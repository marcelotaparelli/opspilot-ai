# PostgreSQL 17 with pgvector (the `vector` extension is created by the migration task).
# Private subnets only, encrypted with the stack key, TLS enforced by parameter.
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = local.name
  family = "postgres17"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class
  db_name        = "opspilot"
  username       = "opspilot_admin"

  # The master password is generated and rotated by RDS in Secrets Manager: it never
  # appears in Terraform configuration, plan output or state.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_allocated_storage * 2
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.main.arn

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az
  parameter_group_name   = aws_db_parameter_group.main.name

  backup_retention_period             = 7
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.db_deletion_protection
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-final"
  auto_minor_version_upgrade          = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  iam_database_authentication_enabled = false
  performance_insights_enabled        = false
}
