# retrieval-v2 dataset

| File | Content | Mutability |
| --- | --- | --- |
| `corpus.json` | 70 documents (acme 64, globex 6 sentinels), addressed by `key` | Immutable since `authoring-log.json` |
| `dev.json` | 24 queries for development and parameter choices | May be corrected with a documented reason (never was) |
| `heldout.json` | 36 queries, run once against `freeze.json` | Immutable; now consumed, so evaluate future changes on a new split |
| `authoring-log.json` | SHA-256 of all three files before any retrieval run | Record |
| `freeze.json` | Dataset, source and configuration fingerprint used by the held-out run | Record |

Labels: `relevance` maps document keys to `2` (directly answers) or `1` (partial/supporting);
`hard_negatives` lists known close-but-wrong documents. Methodology and results:
[docs/evaluation/retrieval-v2.md](../../docs/evaluation/retrieval-v2.md).
