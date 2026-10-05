# Phase 4 release evidence

This page preserves the pre-publication Phase 4/v0.1.0 gate snapshot. Its provider and CI
statuses below describe that historical gate. Separately, the
[post-release OpenAI live smoke](live-openai-smoke.json) passed at
2026-10-04T23:45:24+00:00; see [current provider status](live-provider-status.md).
The [post-release GitLab live smoke](live-gitlab-smoke.json) also passed at
2026-10-05T00:16:39+00:00 in a real sandbox. Neither live run was part of the original gate.
The published v0.1.0 tag and release are unchanged.

ENGINEERING RELEASE: PASS

OPENAI LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

PUSH REALIZADO: NO

Current evidence: [final validation](final-validation.md), [gate ledger](final-gates.json),
[executions and logs](execution-records.json), [source manifest](validated-source-manifest.json).
All critical local engineering gates and full clean-room passed before the final commit.
The old [VM attempt](vm-revalidation.md), [restricted attempt](revalidation.md) and prior
[handoff](handoff.md) describe historical failures, not the current release status.

| Evidence | Result |
| --- | --- |
| [Clean-room](clean-room-validation.md) | PASS: cold pinned install, quality, 241 unit / 72 integration, regressions, migrations, new volume, image and HTTP |
| [Container](container-metadata.json) / [clean rebuild](clean-room-container.json) | PASS: observed non-root/read-only/tmpfs/capabilities |
| [Production config](production-config-audit.md) / [runtime](production-runtime.json) | PASS locally; AWS runtime untested |
| [HTTP RAG](http-rag.json) / [agent](http-agent.json) | PASS with fake provider/GitLab; duplicated clean-room reports use `clean-` prefix |
| [Observability](observability-runtime.json) | Real Collector/Jaeger up/down/recovery; SDK/fault integration also PASS |
| [Vulnerabilities](vulnerability-summary.md) / [SBOM](sbom-image.cdx.json) / [provenance](artifact-provenance.json) | Dependency audit clean; full image findings retained; zero fixable HIGH/CRITICAL |
| [Secrets](secret-scan.md) | Tree/history PASS and synthetic control detected |
| [Terraform validation](terraform-validation.md) / [plans](terraform-plans.json) | PASS, 56/58/53 creates; no apply |
| [IaC risks](iac-findings.md) / [full scan](iac-scan.json) | Four accepted blueprint risks, no suppressions |
| [CI](ci-audit.md) | Static/source PASS; hosted run NOT EXECUTED |
| Live providers at the original gate | Both NOT EXECUTED; rehearsals are not live evidence. [Current post-release status](live-provider-status.md) is separate. |
| [Changed files](files-changed.json) | Complete Phase 4 inventory relative to validated base HEAD |

The engineering result does not establish real-model quality, external-provider permissions,
AWS deployment or vulnerability-free software. The held-out split was not rerun. One final
release commit is allowed after these gates; no push is authorized or performed.
