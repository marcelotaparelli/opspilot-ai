# retrieval-v2: representative retrieval evaluation

> **Post-freeze note (Phase 2).** Phase 2 changed `persistence/postgres.py` in its readiness
> check only (schema version 2 and RLS on the agent tables); vector and lexical SQL are
> byte-identical. Because that file is fingerprinted, `run --split heldout --freeze` now refuses
> to run, which is the intended behaviour. The held-out numbers below belong to commit `7ba3378`.

Measured 2026-10-04 on PostgreSQL 17.6 / pgvector 0.8.0 with the deterministic fake embedder
(`fake:sha256-bow-v1:256`). Machine-readable results are in [`evidence/`](evidence). The held-out
split was executed **once**, after the freeze described below.

**Headline (held-out, 36 queries):** lexical retrieval is the best strategy here, with MRR@5
0.753 vs 0.631 for hybrid and 0.438 for vector. Lexical beats hybrid by +0.123 MRR@5
(paired bootstrap 95% CI +0.045 to +0.217; 10 wins, 1 loss, 25 ties). These numbers describe the
*fake* embedder, which is not a semantic model. They do not predict how a real embedding model
would rank, and they do not show that hybrid retrieval is a bad idea in general.

## 1. Why retrieval-v1 was not enough

[`evals/retrieval-v1.json`](../../evals/retrieval-v1.json) (preserved unchanged, results in
`docs/evidence/retrieval-v1-*.json`) scores 1.0 on every metric for every strategy, which says
nothing:

- the queried tenant has 4 documents, fewer than K=5, so vector Recall@5 is 1.0 for any ranking;
- its 3 questions are bags of words copied from their target documents, and the fake embedder is a
  bag-of-words hash, so vector search was lexical overlap by construction;
- no natural-language question, so the Phase 1 lexical AND-semantics bug was invisible to it;
- no hard negatives, no graded or multiple relevance, no dev/test split.

## 2. Corpus

[`evals/retrieval-v2/corpus.json`](../../evals/retrieval-v2/corpus.json): **70 hand-written
documents** for a fictional payments company.

| Tenant | Documents | Chunks | Role |
| --- | ---: | ---: | --- |
| `acme` | 64 | 68 | Evaluated corpus; 4 long runbooks span 2 chunks each |
| `globex` | 6 | 6 | Cross-tenant sentinels: confusable copies of acme topics (e.g. a limiter that *fails closed*), marked `GLOBEX-CONFIDENTIAL`; never relevant, must never be retrieved |

Topics: Redis (7 documents), PostgreSQL (7), authentication, secrets and permissions, deployment
and CI/CD, Kubernetes containers, networking, API and rate limiting, queues, on-call, ownership,
SLOs and incident process.

**Hard negatives are built in by clustering.** For example:
- 7 documents mention Redis, but only one describes the rate limiter failing open.
- Another describes an idempotency cache that deliberately fails closed.
- Another (`rate-limit-internal-services`) covers rate limiting that does not depend on Redis at all.
- Disk-full appears three times: CI runners, PostgreSQL WAL, and Kubernetes node pressure.
- Signing-key rotation appears for OAuth, the webhook secret and the cache TLS certificate.

The acme vocabulary is 1,456 distinct word tokens.

## 3. Queries and labels

60 hand-written queries, 10 per type:

| Type | Intent |
| --- | --- |
| `paraphrase` | Answer document uses different words ("signed out" vs "logged out", "money movements" vs "transfers") |
| `explicit_terms` | Domain terms present in the answer (`PgBouncer transaction mode`, `VACUUM FREEZE`) |
| `identifier` | Exact strings: error codes, alert names, config keys, CLI commands, tunnel IDs |
| `ambiguous` | Underspecified ("Redis is down. What happens?", "permission denied") with several partial answers |
| `multi_relevant` | Two or three documents each answer part of the question |
| `hard_negative` | A lexically or semantically close document is wrong ("over quota" points at the quota document, but the answer is the fail-open document) |

Labels are graded and written by hand at authoring time:
- `2` means the document directly answers the question.
- `1` means partially relevant or supporting.

Each query also lists its known `hard_negatives` and a note explaining the label. **No label was
derived from any retriever output**, and labels were committed (hash-locked) before the first
retrieval run. Queries reference documents by readable keys. Document UUIDs are
`uuid5(NAMESPACE_URL, "opspilot-eval:retrieval-v2:<key>")`.

**Leakage controls**, enforced by `python -m opspilot.benchmark validate` and unit tests, with no
retriever involved:
- No non-identifier question shares a 5-word sequence with any document; identifiers are exact by design.
- No dev/held-out question pair has word-set Jaccard ≥ 0.5.
- Query IDs are unique across splits.
- Labels must exist in the query's tenant, and hard negatives must be disjoint from relevant documents.

Mean fraction of question words found in the grade-2 documents (held-out; stopwords included):

| paraphrase | ambiguous | hard_negative | multi_relevant | explicit_terms | identifier |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.32 | 0.46 | 0.47 | 0.52 | 0.63 | 1.00 |

## 4. Dev / held-out split and freeze

**Split rule (result-blind).** Within each type, `pinned_dev` queries go to dev first. Queries
ordered by `sha256(query id)` then fill dev up to 4 per type, and the rest are held out. Result:
**24 dev / 36 held-out** (4 + 6 per type). A unit test re-derives the split from this rule.

"How do I restart the service?" (the Phase 1 bug question) is pinned to dev: it was used during
Phase 1 development, so it could not be held out honestly.

Freeze procedure (all recorded under `evals/retrieval-v2/` and `evidence/`):

1. **Authoring lock** (`authoring-log.json`, 11:51 UTC). The SHA-256 of `corpus.json`, `dev.json`
   and `heldout.json` was recorded before any v2 retrieval ran. Corpus and held-out are immutable
   from then on. Freeze and held-out runs refuse if either hash differs. Dev was never edited either.
2. **Dev iteration.** Dev run 1 exposed the lexical defect in §6.1. A dev-only experiment compared
   6 lexical ranking variants ([log](evidence/retrieval-v2-dev-lexical-experiment.txt)); `ts_rank`
   replaced `ts_rank_cd`. Dev run 2 confirmed it. Tie-break sensitivity was measured on dev.
   `heldout.json` was not loaded by any dev tool.
3. **Freeze** (`freeze.json`, 11:58 UTC). This records SHA-256 for the dataset files and for the 9
   source files that determine the numbers (chunking, domain, retrieval, persistence SQL and schema,
   fake provider, application, evaluation, benchmark incl. metrics). It also records the
   configuration: K=5, 20 candidates per branch, RRF constant 60, chunk 1200/overlap 200, embedding
   space, the lexical/vector method and the metric definitions.
4. **Held-out, once** (11:59 UTC; [run log](evidence/retrieval-v2-heldout-run-log.json), exit 0).
   `run --split heldout` refuses to start without `--freeze`, or if the current fingerprint differs
   from the manifest. That check runs before any database connection. Nothing was tuned afterwards.
   The held-out set is now *used*: any future retriever change must be judged on new held-out
   queries (a `retrieval-v3`), not re-scored on this one.

## 5. Metrics

Document-level ranking. The retriever returns the top K=5 **chunks**, which are deduplicated to
documents in first-appearance order. A multi-chunk document can therefore occupy several slots,
leaving fewer than 5 distinct documents. This is the production retrieval budget and was kept
deliberately.

- **Recall@k**: relevant documents (any grade) in the top k ÷ all labelled relevant documents.
  For multi-relevant queries Recall@1 is capped below 1.
- **MRR@5**: 1 / rank of the first relevant document within 5, else 0.
- **NDCG@5**: gain `2^grade − 1` (3 for grade 2, 1 for grade 1), discount `log2(rank+1)`, with the
  ideal ranking taken from all labels.
- Means are macro-averaged over queries. Every table shows *n*.

**Tie sensitivity.** UUIDs break ties in pgvector ordering and in RRF. Each run therefore also
re-measures under 10 random UUID layouts in the same invocation, and that spread is reported. Only
the fixed layout is "official".

**Hybrid integrity.** For every query the harness independently recomputes `RRF(vector candidates,
lexical candidates)` and aborts if it differs from the retriever's hybrid output. It also counts
empty branches and hybrid lists identical to a single branch.

## 6. Results

### Dev (24 queries; used for development; 1.67 relevant and 1.08 grade-2 per query)

| Strategy | Recall@1 | Recall@3 | Recall@5 | MRR@5 | NDCG@5 | no relevant in top 5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Lexical | 0.5208 | 0.6493 | 0.6736 | 0.6958 | 0.6175 | 5 / 24 |
| Vector | 0.2188 | 0.4688 | 0.5417 | 0.3924 | 0.4188 | 10 / 24 |
| Hybrid | 0.3854 | 0.5313 | 0.6285 | 0.5292 | 0.5262 | 7 / 24 |

Before the lexical ranking change (dev run 1, `ts_rank_cd`), lexical MRR@5 was 0.4965 and hybrid
0.4840 ([report](evidence/retrieval-v2-dev-run1-ts_rank_cd.json)).

### Held-out: official (36 queries; 1.78 relevant and 1.17 grade-2 per query; 33 with hard negatives)

| Strategy | Recall@1 | Recall@3 | Recall@5 | MRR@5 | NDCG@5 | no relevant in top 5 | hard negative above first relevant |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Lexical** | **0.5319** | **0.6625** | **0.7644** | **0.7532** | **0.7124** | 4 / 36 | 3 / 33 |
| Vector | 0.3065 | 0.3931 | 0.4403 | 0.4375 | 0.4247 | 18 / 36 | 2 / 33 |
| Hybrid | 0.4116 | 0.5523 | 0.6773 | 0.6306 | 0.6103 | 7 / 36 | 3 / 33 |

Tie sensitivity over 10 random UUID layouts (min to max):
- lexical MRR@5 0.753–0.774;
- hybrid MRR@5 0.632–0.655 and Recall@3 0.566–0.622;
- vector does not change.

Every strategy gap below is larger than this spread.

By query type (held-out, MRR@5 / Recall@5; n = 6 each):

| Type | Lexical | Vector | Hybrid |
| --- | --- | --- | --- |
| identifier | 1.00 / 1.00 | 0.67 / 0.67 | 0.89 / 1.00 |
| explicit_terms | 0.92 / 0.92 | 0.75 / 0.83 | 0.89 / 0.92 |
| hard_negative | 0.83 / 0.83 | 0.58 / 0.67 | 0.71 / 0.83 |
| multi_relevant | 0.71 / 0.40 | 0.29 / 0.21 | 0.58 / 0.35 |
| ambiguous | 0.54 / 0.43 | 0.33 / 0.27 | 0.56 / 0.47 |
| paraphrase | 0.52 / 1.00 | **0.00 / 0.00** | 0.17 / 0.50 |

Paired comparison (held-out, MRR@5, bootstrap 10,000 resamples, seed 0;
[analysis](evidence/retrieval-v2-heldout-paired-analysis.json)):

| Pair | Mean difference | 95% CI | wins / losses / ties |
| --- | ---: | --- | --- |
| lexical − hybrid | +0.123 | [+0.045, +0.217] | 10 / 1 / 25 |
| lexical − vector | +0.316 | [+0.188, +0.455] | 18 / 1 / 17 |
| hybrid − vector | +0.193 | [+0.098, +0.299] | 15 / 1 / 20 |

Hybrid diagnostics (held-out):
- the lexical branch was empty on 0/36 queries, and the vector branch on 0/36;
- RRF recomputation mismatches: 0;
- hybrid equals vector-only on 2/36 (`ident-04`, `ident-06`): lexical returned exactly one
  candidate, which was also vector's #1, so the identical lists are legitimate, not degeneration;
- hybrid equals lexical-only on 0/36;
- hybrid beat *both* single strategies on one query (`ambig-10`) and fell below lexical on 10.

## 7. Failure analysis: why each strategy fails

### 7.1 Lexical ranking bias (found on dev; fixed before freeze)

Dev run 1 put the same three long documents (`postgres-pitr`, `incident-response-handbook`,
`payments-api-runbook`) in the lexical top 5 for most queries, including "the queue is stuck" and
`limit_req_fallback=on`.

The cause is the interaction of Phase 1's OR query with `ts_rank_cd`. Cover-density ranking is
built for proximity between *required* terms. Under OR, every occurrence of any question word
(including "the", "on", "do") is a cover, so ranking degenerates into counting occurrences, and
long chunks win. `ts_rank`'s OR path saturates per-term frequency and averages over query terms.

This was changed on dev with no new parameters. Dev MRR@5 went 0.4965 → 0.6958, and no dev query
got worse. IDF-weighted matching scored higher still (0.722) but needs extra per-query SQL, so it
is a recommendation only (§9).

### 7.2 No IDF: stopwords still count (lexical, residual)

`term-07` "Canary error rate threshold that triggers an automatic rollback":
- the correct `deploy-canary` shares `canary, error, rate`;
- the hard negative `api-5xx-triage` shares `an, error, that`.

Without inverse document frequency a stopword match weighs as much as "canary", and the hard
negative ranks first in all three strategies.

### 7.3 No stemming (lexical)

The same query says "automatic rollback". The canary document says "aborts and shifts all traffic
back **automatically**". `simple` neither stems nor maps synonyms, so "automatic" ≠ "automatically"
and "deployment" ≠ "deploy". `ambig-09` "deployment is blocked" retrieves nothing relevant in any
strategy; `postgres-migration-locks` says "blocks", not "blocked".

### 7.4 Titles are not indexed (all strategies)

`hardneg-05` "The build machines are out of disk space" fails everywhere. The word "disk" appears
only in the correct document's **title** ("CI runners running out of disk"). The content says "no
space left on device". Only chunk content is tokenized and embedded. Found on held-out, so it was
documented, not fixed.

### 7.5 Vocabulary mismatch without semantics (all strategies)

These fail because the right documents use different words:
- `ambig-04` "who do I contact about the rate limiter?": the ownership document says "request
  throttling … Edge team".
- `multi-04` "database schema changes": the documents say "migrations".
- `multi-01` "first half hour of a SEV1": lexical matched "hour" in the catalog stampede
  document ("top of every hour").

This is precisely what a semantic embedder is for. The fake one cannot provide it.

### 7.6 The fake vector branch is not semantic

The vector branch scored 0/6 on held-out paraphrases. The fake embedder hashes 1,456 distinct
tokens into 256 buckets (about 5.7 tokens per bucket), so unrelated words collide. Cosine is also
dominated by stopwords and document length.

Identifier `RateLimiterFailOpen` (`ident-02`) ranks `service-ownership` first by collision. The
vector branch also always returns the full 20 candidates, since every chunk has *some* distance,
so it never abstains.

### 7.7 Equal-weight RRF lets a weak branch outvote a strong one (hybrid)

`para-10` "Old Android phones and Java apps can no longer connect securely since we renewed our
public certificate":
- lexical ranks the correct `net-tls-chain` **#1**;
- hybrid drops it from the top 5 entirely.

The arithmetic: a document found by only one branch scores 1/61 ≈ 0.0164 at best. A document both
branches rank 3rd scores 2/63 ≈ 0.0317. Generic documents that appear in both noisy candidate
lists therefore beat a single-branch top hit. Long, vocabulary-rich runbooks act as "hubs" here.

The same pattern appears in `para-03`, `para-07`, `ident-03` and `hardneg-09` (hybrid below
lexical on 10/36 held-out queries). Hybrid helped only where lexical ranked poorly and vector
happened to agree (`ambig-10`).

**This does not argue for tuning RRF weights against this benchmark.** With a real semantic
embedder the vector branch would be the strong one on paraphrases. Branch weights should be chosen
on a dev set measured with that embedder.

### 7.8 Chunk budget and multi-chunk documents (all strategies)

On `multi-01`, lexical retrieved only 3 distinct documents in its 5 chunk slots: two long documents
took two slots each. Multi-relevant Recall@5 (held-out lexical 0.40) is partly this budget effect,
not only ranking.

## 8. Comparison summary

- **Lexical** wins on identifiers (1.00), explicit terms and hard negatives, and finds paraphrases
  only through incidental shared words (MRR 0.52). It fails on vocabulary mismatch, missing
  stemming and missing IDF.
- **Vector (fake)** carries little relevance signal beyond word overlap. It is useful here only as
  plumbing, and it is the reason hybrid underperforms.
- **Hybrid** lands between its branches in aggregate (below vector on only 1/36 queries), but
  under equal-weight RRF with a non-semantic vector branch it is worse than lexical alone
  (statistically clear at n=36). Its value has to be
  re-measured with a real embedder.

## 9. Limitations and recommendations

- **Fake embeddings.** Vector and hybrid numbers say nothing about a real embedding model. Running
  `PROVIDER=openai` against the same frozen dataset requires a new freeze (the embedding space is
  part of the fingerprint) and paid calls, so it was not done.
- **Size and authorship.** 36 held-out queries over 64 documents. One author wrote the documents,
  queries and labels, so labels reflect one person's judgement and were not double-annotated. The
  confidence intervals above do not include label noise.
- **Synthetic corpus.** Documents are short, clean and English-only. Real runbooks are longer,
  messier, multilingual and full of tables and code.
- **Document-level labels only.** There is no chunk-level ground truth for the 4 multi-chunk documents.
- **Shared corpus.** Corpus and dev are shared with held-out. Dev was used to choose the lexical
  ranking function, so the held-out estimate is honest for that choice, but this held-out set is
  now consumed.
- **Recommended next steps** (none implemented): index titles together with content; IDF/BM25-style
  weighting (custom IDF as in experiment V5, or a BM25 extension); a stemming or language-aware
  configuration (needs a schema migration); a real embedder; branch weighting chosen on dev with
  that embedder; and a fresh `retrieval-v3` held-out set to evaluate those changes.

## 10. Reproduce

```bash
# Dedicated database with schema v1 and the opspilot_app role, as in README §4.
export DATABASE_URL=postgresql+asyncpg://opspilot_app:...@localhost:5432/opspilot_bench
export TENANT_TOKENS='{"<token of 32+ chars>":"c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0"}' PROVIDER=fake
uv run python -m opspilot.benchmark validate
uv run python -m opspilot.benchmark run --split dev --seed --tie-replicates 10 --output dev.json
# Refuses unless code, data and configuration match the committed freeze manifest:
uv run python -m opspilot.benchmark run --split heldout --freeze evals/retrieval-v2/freeze.json \
  --seed --tie-replicates 10 --output heldout.json
uv run python scripts/analyze_benchmark.py heldout.json
```

The fixed-layout numbers are deterministic for a clean database. Rerunning the held-out command
reproduces a frozen result; it is not a new evaluation.
