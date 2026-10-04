> HISTORICAL ATTEMPT. Current ENGINEERING RELEASE: PASS; both live providers NOT EXECUTED; PUSH REALIZADO: NO. See [final validation](final-validation.md).

# Phase 4 — handoff da continuação

Latest attempt: [revalidation handoff](revalidation.md), including the seven-change audit,
additional smoke fixes and newly executed static checks. The record below is HISTORICAL
to that attempt; its test/scanner/clean-room results must not be promoted to FINAL.

ENGINEERING RELEASE: BLOCKED

OPENAI LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

GITLAB LIVE EVIDENCE: NOT EXECUTED — CREDENTIALS NOT PROVIDED

PUSH REALIZADO: NO

A continuação preservou os arquivos de trabalho importantes da Phase 4 e avançou auditoria,
correções dos smokes, Compose/CI e documentação. A release não foi concluída: o sandbox nega
sockets locais/Docker, bloqueia downloads e não tem o Python fixado nem scanners/Terraform.
Não há autorização pelo contrato para commit sem todos os gates críticos e clean-room PASS.
Nenhuma credencial de provider foi solicitada ou utilizada.

| Item | Resultado / evidência |
| --- | --- |
| 1. Resumo executivo | Phase 4 retomada, documentação/evidence atualizadas, release BLOCKED; código e artefatos herdados preservados, sem commit/push. |
| 2. Bugs/lacunas encontrados | Smoke OpenAI registrava fatos sem exigir usage/custo/modelo/trace/evidence para PASS; qualquer ProviderError podia contar como timeout. GitLab contava respostas, omitindo tentativas sem resposta, e cleanup dependia de IID. CLI podia expor mensagens de exceções. Corrigidos no código, com novas regressões; execução pytest final pendente. Compose não encaminhava APP_ENV/EXPOSE_API_DOCS; corrigido e parsing validado. CI retinha apenas gate fixável; agora gera relatório completo e guarda artefatos em falha. |
| 3. Production config audit | [Auditoria](production-config-audit.md): docs off, erros padronizados, checks production, tenant/policy, prazos, roles, segredos e telemetry. ADOT ganhou UID explícito 4317 e seleção explícita da configuração env. TLS de cliente, privacidade de logs SQL, grant compartilhado de secrets e runtime AWS continuam sem prova. |
| 4. Migrations | Código v1→v2 e novos testes auditados; empty/idempotence/preserved-data/future-version/concurrency **BLOCKED** contra DB real. Nenhuma nova validação de migration PASS. |
| 5. Container | Build final sem cache foi tentado: Docker socket denied. ID/digest/size/UID observados: indisponíveis. Dockerfile/API/migrator declaram 10001; ADOT declara 4317. Read-only-root/tmp/capabilities runtime **BLOCKED**. [Metadata](container-metadata.json). |
| 6. SBOM | [CycloneDX herdado](sbom-image.cdx.json) preservado; Trivy 0.75.0, timestamp 2026-10-04T19:04:35+00:00, imagem antiga opspilot-ai:rc identificada por sha256:33316c9b583d4e1ad5d77a7a312b78ebf46967a80b9e903594ac2bfdaf995a14. **Não é SBOM final.** |
| 7. Dependency vulnerabilities | Audit herdado: 67 pacotes / zero findings reportados. Versão/freshness não comprovadas pelo JSON. Audit final **BLOCKED**; [proveniência](artifact-provenance.json). |
| 8. Image vulnerabilities | Sem scan final, contagem/fixabilidade indisponíveis. Docker/Trivy indisponíveis; não inferir zero vulnerabilidades do SBOM. [Resumo](vulnerability-summary.md). |
| 9. IaC findings | Quatro riscos deliberados formalmente documentados: ALB público, HTTPS egress aberto, IAM DB auth off, Performance Insights off. Cada um tem risco/motivo/mitigação/accepted-future. Não classificados como false positive. Scan final **BLOCKED**. [Registro](iac-findings.md). |
| 10. Secret scan | Working tree/history/positive-control finais **BLOCKED**: Gitleaks ausente. Relato anterior não promovido a evidência final. Allowlists estreitas mantidas; [registro](secret-scan.md). |
| 11. Terraform architecture | Internet → ALB público → Fargate → RDS privado/pgvector; ECR immutable, secret containers/KMS e ADOT opcional. Default sem NAT com IP público dos tasks e ingress somente ALB; NAT opcional desloca tasks para private. Não deployed. |
| 12. Terraform fmt/init/validate | Comandos tentados, executable ausente. **BLOCKED**, lockfile preservado; [registro](terraform-validation.md). |
| 13. Plans | Default/NAT/no-collector finais **BLOCKED**. Contagens 56/58/53 pertencem somente ao handoff anterior. Nenhum apply. [Resumo](terraform-plan-summary.md). |
| 14. AWS cost drivers | Fargate, RDS, ALB, NAT, endpoints, logs/telemetry, OpenAI e egress descritos qualitativamente. Nenhum monthly cost exato inventado. NAT opcional evita baseline de gateway no demo, com tradeoff de IP público/SG. [AWS](../../deployment/aws.md). |
| 15. CI audit | Auditoria estática PASS: lock/Ruff/mypy/suites/dev/agent/security/build/scans/SBOM/Terraform presentes; Actions SHAs completos e scanner checksums. Live/held-out execution/apply ausentes. Hosted run **NOT EXECUTED**. [Auditoria](ci-audit.md). |
| 16. Clean-room | Snapshot isolado sem venv/cache/.env/runtime; lock metadata PASS, cold locked installation falhou. Reprodução completa **BLOCKED**, nunca PASS. Recriar snapshot final e completar todos os estágios. [Registro](clean-room-validation.md). |
| 17. Unit | Tentativa pré-edits: 295 collected totais, 226 selected, 69 deselected; runner interrompido, exit 130. Passed/failed/skipped completos: indisponíveis. Suite final **BLOCKED**, novos testes ainda não coletados/executados normalmente. |
| 18. Integration | Collected/passed/failed/skipped finais: indisponíveis; 69 integration deselected na coleta anterior não são resultado de integração. Suite final **BLOCKED**. |
| 19. RAG regression | DEV final **BLOCKED**, nenhuma execução held-out. Números históricos publicados permanecem atribuídos ao experimento anterior. |
| 20. Agent regression | Final **BLOCKED**. Histórico Phase 3: 16/16 casos real PostgreSQL/fake GitLab/scripted-offline planners; não mede LLM real. |
| 21. Security regression | Final **BLOCKED**. Histórico Phase 3 10/10; não promovido a resultado atual. |
| 22. Observability tests | Final **BLOCKED**. Perfil runtime também indisponível; revisão estática dos allowlists/native telemetry/log status efetuada. |
| 23. Release tests | Final **BLOCKED**. Novas regressões de evidência negativa, exception redaction, lost-response counting/cleanup adicionadas. 11 checks isolados da função real de predicates passaram como suplemento, **não** suite/rehearsal/provider evidence. [Revisão](test-quality-review.md). |
| 24. Docker build | `docker build --no-cache --tag opspilot-ai:release .` falhou por permissão do socket. Gate **BLOCKED**. |
| 25. Compose empty-volume | **BLOCKED**; sem acesso ao daemon para inspecionar/parar/remover apenas volumes de teste. Nenhum volume foi removido. Configs normal/observability/fake-GitLab: três parsing checks PASS. |
| 26. HTTP RAG smoke | health/ready/documents/query/tenant isolation contra stack novo **BLOCKED**. Nenhum PASS herdado usado como final. |
| 27. HTTP agent smoke | run/proposal/approve/execution/fake GitLab/terminal contra stack novo **BLOCKED**. |
| 28. OpenAI live | NOT EXECUTED — CREDENTIALS NOT PROVIDED. Strict minLength/maxLength real, modelos, embeddings, usage/custo ainda sem evidência live. |
| 29. GitLab live | NOT EXECUTED — CREDENTIALS NOT PROVIDED. Permissões/search/real create/cleanup não verificados live. |
| 30. Documentação | README, CURRENT-STATE, VALIDATION, DEPENDENCIES, ADR 003, agent workflow e observability atualizados; AWS, portfolio (541 palavras), interview notes e release evidence criados. Resultados históricos/finais qualificados. |
| 31. Limitações restantes | Sem suites finais, scans, imagem/runtime, plans ou clean-room PASS. Sem real-provider/model-quality/AWS evidence; identidade estática, sem ACL intra-tenant, recuperação manual, lease/search não garantem exactly-once, single-replica/single-AZ defaults, TLS/log privacy/runtime pendentes. |
| 32. Files changed | [Inventário completo](files-changed.json), incluindo mudanças herdadas; nenhum arquivo rastreado deletado. Working tree deliberadamente mantido dirty. |
| 33. Git | origin git@github.com:marcelotaparelli/opspilot-ai.git; HEAD main 55ddc102bef685d103f4868afea782cd1c5dbb88; fsck full PASS; log preserva Phases 1/1.5/2/3; diff-check PASS; status dirty, index sem staging. |
| 34. Commit | **Não criado**, SHA/message/author de release inexistentes. Mensagem reservada pelo contrato: chore: prepare opspilot release candidate. Identity config/git add/commit não executados porque faltam gates. |
| 35. Push | **PUSH REALIZADO: NO**. Sem fetch/pull/push/tag, reset/restore/clean/stash/checkout/switch/rebase. Sem Phase 5 ou infraestrutura aplicada. |

## Próximo ambiente / critério de conclusão

Retomar este mesmo working tree em ambiente com sockets locais e Docker acessíveis, downloads
permitidos e versões fixadas de Python/uv/scanners/Terraform. A venv anterior apontava para
Python ausente; uv sync a recriou, mas ela não pôde ser preenchida. É estado gerado/ignorado,
não um substituto do source tree, e será reconstruída por locked sync no próximo ambiente.
Não utilizar a venv ou relatórios herdados como prova de reprodução.

Reexecutar os gates completos, os três plans/invariantes, scans/positive-control, imagem final
com metadata/SBOM, read-only runtime, volumes vazios/perfil observability, HTTP e clean-room.
Corrigir falhas no working tree original e atualizar evidência. Somente então configurar o
autor especificado, revisar o diff staged e criar **um** commit com a mensagem exigida.
Falta de credenciais live não bloqueia essa conclusão; não pedir secrets nesta execução.
Após commit: status clean, log/show/fsck/origin; entregar SHA/message/author. Não fazer push.
