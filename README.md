# k1-tax-bridge

Moves a partnership Schedule K-1 from the [Open Tax Document](https://github.com/opentaxdocument/otd-spec)
standard into Filed's open-source [OpenTax](https://github.com/filedcom/opentax) Form 1040 engine,
and refuses to report a number it cannot account for.

## Why it exists

OpenTax is deliberately fail-closed about evidence, but when a form node lacks it the
engine **drops that node and still returns a result**, recording the failure only in
`warnings`. On the synthetic K-1, a plain wrapper reports total income of **$1,598,400**.
The K-1 actually supports **$3,024,900**. $687,000 of interest and dividends vanish because
Schedule B lacked a foreign-account answer, and the $738,700 box 11S gain is never sent.

## Gates (any failure → `BLOCKED`, exit 2, no result reported)

| Gate | Check |
|---|---|
| G1 | OTD document passes the otd-spec reference validator |
| G2 | Preparer-supplied EIN/TIN match the digits visible on the masked K-1; non-individual recipients need an explicit override |
| G3 | Every non-empty K-1 value has a disposition: `sent`, `sent+attested`, `excluded-by-reviewer` (named reviewer + reason), or `info`. Unknown items block. |
| G4 | Preflight: inputs the engine is known to need (Schedule B Part III answers, filing status) |
| G5 | Engine accepts every form entry |
| G6 | No `EXECUTOR_NODE_FAILURE` during compute, reported back in K-1 box terms |
| G7 | 1040 lines 2b, 3a, 3b, 7, 8 reconcile to the K-1 values that were sent (±$1) |

Verdicts: `COMPLETE` (exit 0), `COMPUTED_WITH_EXCLUSIONS` (exit 3: computed, but reviewer
exclusions mean it is **not** a complete return), `BLOCKED` (exit 2). `--naive` mimics an
ungoverned wrapper for comparison and always reports.

A `BLOCKED` run writes no computed figures: no `summary` in the audit and no `engine_return.json`.

Each run writes `audit.json`, `audit.md` (gates, reconciliation, a disposition row per
K-1 value, input/engine hashes) and `engine_return.json`.

## Run

```bash
export OPENTAX_BIN=/opt/opentax-test/opentax/opentax
export OTD_VALIDATOR=/opt/opentax-test/otd-spec/skills/k1-otd/scripts/validate_otd.py
export OPENTAX_COMMIT=$(git -C /opt/opentax-test/opentax rev-parse --short HEAD)
PYTHON=/opt/opentax-test/venv/bin/python demo/run_demo.sh
/opt/opentax-test/venv/bin/python -m pytest -q tests
```

The facts file (`demo/facts.synthetic.yaml`) carries identity, Schedule B answers,
attestations for evidence-gated boxes, and reviewer decisions. Everything in it is synthetic.

## Scope and limits

- Tested against OpenTax `0c6ae76` (TY2025) and otd-spec `be6452a` (taxonomy `irs-k1-1065-2025@2025.1.1`).
- One K-1 per return, no other income sources; reconciliation assumes that.
- Boxes 12, 13H and 17 are review items because the engine's Forms 4562, 4952 and 6251
  reject aggregate K-1 totals. The bridge cannot compute them yet.
- Not a filing tool. OpenTax is AGPL v3 and is invoked here as a separate process;
  commercial hosted use of the engine needs Filed's commercial licence.
- `tests/fixtures/synthetic-k1.otd.yaml` is generated from otd-spec's synthetic example (CC BY 4.0).

## License

Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). The OpenTax engine it drives is a
separate AGPL v3 project and is not included. Security reports: see [SECURITY.md](SECURITY.md).
