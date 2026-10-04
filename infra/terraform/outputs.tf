output "alb_dns_name" {
  value = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "db_master_secret_arn" {
  description = "RDS-managed master credentials (bootstrap only; never used by the API)."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "migration_task_definition" {
  value = aws_ecs_task_definition.migrate.arn
}

output "secret_arns" {
  description = "Containers whose values must be set out of band before the first deploy."
  value       = merge({ for k, s in aws_secretsmanager_secret.app : k => s.arn }, { for k, s in aws_secretsmanager_secret.migration : k => s.arn })
}
