# Changelog

## 0.3.0 (2026-10-17)

- Attestations must carry a `provenance` block (source, who or what retrieved it, when, and a SHA-256
  of the source when a tool retrieved it). A missing or incomplete block blocks the run at G3.
- Provenance is stripped before the engine call and recorded in `audit.json` (`provenance`, and per
  ledger row) and `audit.md` (new "Evidence provenance" and "Summary" sections).
- A run that ends `BLOCKED` writes no computed figures: no `summary` in the audit and no
  `engine_return.json` (commit 429419d, covered by a fake-engine test for G6 and G7).
- Apache-2.0 licence, NOTICE and security policy (commit 66b5848).
- Breaking for existing facts files: add a `provenance` block to every attestation.
- Tests: 46 pass without an engine, plus 7 that need OpenTax or are data-dependent (53 collected).

## 0.2.0 (2026-10-08)

- First public version: seven fail-closed gates, an audit trail per run, and a synthetic K-1 demo.
