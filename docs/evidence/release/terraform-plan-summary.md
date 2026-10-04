# Offline Terraform plans — PASS

| Variant | Creates | Changes / destroys |
| --- | ---: | --- |
| Default | 56 | 0 / 0 |
| enable_nat_gateway=true | 58 | 0 / 0 |
| enable_otel_collector=false | 53 | 0 / 0 |

Fresh provider plans verify private encrypted RDS, managed master password, immutable ECR,
no secret-version resources, expected NAT count and task public-IP setting. API task container
JSON remains unknown until ECR URL resolution; user/capability/read-only controls are source
assertions and local Docker runtime evidence, not fully known provider-plan values.

[Machine summary](terraform-plans.json); all full plans/state/provider caches remain outside
Git. Only placeholders were used. No AWS apply, credentials validation or deployment occurred.
