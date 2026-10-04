# Terraform validation — PASS

Terraform 1.16.5; AWS provider 6.67.0, readonly committed lock. On 2026-10-04:
fmt -check -recursive, fresh TF_DATA_DIR init -backend=false -input=false -lockfile=readonly,
and validate all exit 0. Provider cache and extraction paths were explicitly on /workspace,
not the 512 MiB /tmp filesystem. No lock drift, remote backend, real credentials or apply.

Three offline -refresh=false plans used offline.tfvars and placeholder AWS environment keys
with account/region/metadata credential checks disabled. [Plan summary](terraform-plans.json)
and [execution log](logs/terraform.log) record current evidence; plans/state/cache stay out of Git.
