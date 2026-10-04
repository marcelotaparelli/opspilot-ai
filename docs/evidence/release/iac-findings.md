# IaC findings and accepted blueprint risks

Final Trivy 0.75.0 scan: **90 checks passed, four findings**.
[Full JSON](iac-scan.json). AWS-0053 HIGH public ALB; AWS-0104 CRITICAL unrestricted HTTPS
egress; AWS-0133 LOW Performance Insights off; AWS-0176 MEDIUM IAM DB authentication off.
All are deliberate blueprint risks with rationale/mitigation below; none is a false positive
or suppressed. Acceptance covers this undeployed blueprint, not a production deployment.

## 1. Public load balancer

**Finding:** `aws_lb.api.internal=false`, default ALB ingress `0.0.0.0/0` on 80/443.

**Risk:** public attack surface, abusive traffic and resource/API-cost exhaustion. TLS alone
does not authenticate users or impose quotas.

**Why it exists:** the HTTP API is intended to be reachable through a single public HTTPS entry.

**Mitigation:** ACM/TLS policy, HTTP redirect, dropped invalid headers, bearer authentication,
bounded request body/deadlines, API SG accepting only ALB traffic and private non-public RDS.
Narrow `allowed_ingress_cidrs` for a limited demonstration. WAF/rate limiting is not provisioned.

**Accepted / future work:** accepted for the blueprint's public entrypoint; review restricted
CIDRs, WAF/rate controls and identity management before broader exposure.

## 2. Unrestricted HTTPS egress

**Finding:** task SG allows TCP 443 to `0.0.0.0/0`.

**Risk:** compromised tasks could exfiltrate data or communicate with arbitrary HTTPS hosts.
Security groups do not validate destination domains or application payloads.

**Why it exists:** OpenAI, GitLab, ECR, Secrets Manager and telemetry need outbound HTTPS;
provider addresses change. NAT changes routing/public-IP exposure, not this egress policy.

**Mitigation:** only 443 is generally allowed; database egress is SG-specific on 5432. GitLab
URL is validated, redirects are disabled, credentials are scoped and emitted telemetry is
allowlisted. VPC rejected-flow logs help diagnosis; they are not an exfiltration prevention tool.

**Accepted / future work:** accepted for the small blueprint. Evaluate AWS VPC endpoints for
AWS traffic and a controlled egress proxy/firewall with domain policy for external APIs.

## 3. Database IAM authentication disabled

**Finding:** `iam_database_authentication_enabled=false`.

**Risk:** password-based runtime credentials can be stolen or remain usable too long;
rotation requires operational coordination.

**Why it exists:** current asyncpg/SQLAlchemy adapter uses a configured DSN and pooled
connections. There is no IAM-token refresh/reconnect implementation in the app.

**Mitigation:** separate runtime/migration roles, forced RLS, limited grants, non-public RDS,
forced TLS, Secrets Manager injection and RDS-managed master secret. API never uses master
credentials. Certificate trust/identity and runtime-role rotation need deployment validation.

**Accepted / future work:** accepted for current adapter compatibility. Evaluate short-lived
IAM tokens with pool refresh behavior, or automate coordinated password rotation/redeployment.

## 4. Performance Insights disabled

**Finding:** `performance_insights_enabled=false`.

**Risk:** reduced DB diagnosis, query/load attribution and capacity planning.

**Why it exists:** the small blueprint avoids enabling an additional optional DB diagnostics
service by default; no production query baseline has been measured.

**Mitigation:** application retrieval/DB timing traces, CloudWatch/RDS signals and PostgreSQL
log export are available declarations. DB slow-query logging must be reviewed for sensitive
bound parameters before real data; logs are not a substitute for measured DB diagnostics.

**Accepted / future work:** accepted for the undeployed blueprint; evaluate the currently
available RDS diagnostics mode, retention and cost for the actual workload, with privacy-aware
SQL logging and operational alarms. Do not assume any particular product lifecycle/pricing.
