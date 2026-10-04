# Secret containers only. Values are written out of band (CLI/console) and therefore never
# enter Terraform state; ECS injects them at task start. Rotation is the operator's duty.
locals {
  app_secrets = {
    DATABASE_URL   = "Runtime role opspilot_app: postgresql+asyncpg://opspilot_app:<urlencoded>@<rds-endpoint>:5432/opspilot?ssl=require"
    TENANT_TOKENS  = "JSON token -> principal map (>=32 random characters per token)"
    OPENAI_API_KEY = "OpenAI project key with a spend limit"
    GITLAB_TOKEN   = "Project access token, Reporter, scope api, short expiry"
  }
  migration_secrets = {
    MIGRATION_DATABASE_URL = "Owner role used only by the one-off migration task (ssl=require)"
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each                = local.app_secrets
  name                    = "${local.name}/${lower(replace(each.key, "_", "-"))}"
  description             = each.value
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "migration" {
  for_each                = local.migration_secrets
  name                    = "${local.name}/${lower(replace(each.key, "_", "-"))}"
  description             = each.value
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}
