terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Remote state is deliberately not configured: this is a blueprint that is validated and
  # planned offline, never applied from this repository. Configure an encrypted S3 backend
  # with locking before any real use (state contains resource metadata and ARNs).
}

provider "aws" {
  region = var.region

  # Offline planning: no account lookup, no metadata service, no credential check.
  # Real credentials come only from the operator's environment, never from this code.
  skip_credentials_validation = var.offline_plan
  skip_requesting_account_id  = var.offline_plan
  skip_metadata_api_check     = var.offline_plan
  skip_region_validation      = var.offline_plan

  default_tags {
    tags = {
      Project     = "opspilot-ai"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
