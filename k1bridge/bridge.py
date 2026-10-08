"""OTD K-1 -> OpenTax bridge with fail-closed governance gates.

Gates, in order (any failure blocks the run and nothing is reported as a result):
  G1 OTD document passes the reference validator (otd-spec validate_otd.py)
  G2 preparer identity matches the visible digits of the masked IDs on the K-1
  G3 every non-empty OTD value has a disposition (direct/attested/review/info)
  G4 preflight: inputs the engine is known to need are present
  G5 the engine accepts every form entry
  G6 no engine node failed during compute (OpenTax drops such nodes and only warns)
  G7 1040 lines reconcile to the K-1 values that were sent
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .mapping import IDENTITY_PREFIXES, NODE_SOURCES, RECONCILE, RULES

BRIDGE_VERSION = "0.2.0"
TOLERANCE = 1.0  # dollars; the engine rounds to whole dollars on several lines


class Blocked(Exception):
    def __init__(self, gate: str, reasons: list[str]):
        super().__init__(f"{gate}: {'; '.join(reasons)}")
        self.gate, self.reasons = gate, reasons


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- OTD reading
def otd_nodes(body):
    if isinstance(body, dict):
        if "semantic" in body and "value" in body:
            yield body
        for v in body.values():
            yield from otd_nodes(v)
    elif isinstance(body, list):
        for v in body:
            yield from otd_nodes(v)


def is_empty(value) -> bool:
    return value is None or value is False or value == "" or value == {} or value == []


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def visible_tail(masked: str) -> str:
    """'**-**65189' -> '65189'; 'XX-XXX8572' -> '8572'."""
    m = re.search(r"(\d+)$", masked or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- gates
def g1_validate(otd_path: Path, validator: Path | None, python: str) -> dict:
    if validator is None:
        raise Blocked("G1", ["no OTD validator configured (--otd-validator); refusing to trust the document"])
    proc = subprocess.run([python, str(validator.resolve()), "--input", str(otd_path.resolve())],
                          capture_output=True, text=True, cwd=validator.parents[3])
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
        raise Blocked("G1", [f"validator exit {proc.returncode}"] + tail)
    return {"validator": str(validator), "exit": 0}


def g2_identity(by_id: dict, facts: dict) -> dict:
    reasons = []
    ident = facts.get("identity") or {}
    ein, tin = digits(ident.get("partnership_ein", "")), digits(ident.get("recipient_tin", ""))
    for label, full, masked in (("partnership_ein", ein, by_id.get("partnership.ein", "")),
                                ("recipient_tin", tin, by_id.get("partner.identifying_number", ""))):
        if len(full) != 9:
            reasons.append(f"{label} missing or not 9 digits in facts")
        elif not full.endswith(visible_tail(masked)):
            reasons.append(f"{label} does not match the digits visible on the K-1 ({masked})")
    entity = by_id.get("partner.entity_type")
    if entity != "individual":
        override = facts.get("recipient_override") or {}
        if not override.get("reason"):
            reasons.append(f"K-1 recipient is '{entity}', not an individual; a 1040 needs a "
                           "recipient_override with a reason")
    if reasons:
        raise Blocked("G2", reasons)
    return {"partnership_ein": ein, "recipient_tin": tin, "recipient_entity_on_k1": entity,
            "override": facts.get("recipient_override")}


def g3_dispositions(nodes: list[dict], facts: dict, naive: bool):
    """Return (k1 input, ledger). Raises Blocked on any unresolved item."""
    k1: dict = {}
    ledger, reasons = [], []
    attest = facts.get("attestations") or {}
    decisions = facts.get("review_decisions") or {}
    for n in nodes:
        sid, value = n["semantic"]["id"], n["value"]
        entry = {"semantic_id": sid, "box": "", "value": value}
        if sid.startswith(IDENTITY_PREFIXES):
            entry.update(disposition="identity", note="Parts I/II descriptive; never sent to engine")
            ledger.append(entry)
            continue
        if is_empty(value):
            entry.update(disposition="empty")
            ledger.append(entry)
            continue
        rule = RULES.get(sid)
        if rule is None:
            entry.update(disposition="UNCLASSIFIED")
            reasons.append(f"{sid} = {value!r} has no rule in the bridge (fail closed)")
            ledger.append(entry)
            continue
        entry["box"] = rule.box
        if naive:  # what a plain wrapper does: copy what fits, ignore the rest
            if rule.kind == "direct":
                amount = rule.sign * value
                k1[rule.target] = k1.get(rule.target, 0) + amount if rule.sums_into else amount
                entry.update(disposition="sent", target=rule.target)
            else:
                entry.update(disposition="dropped")
            ledger.append(entry)
            continue
        if rule.kind == "direct":
            amount = rule.sign * value
            if rule.sums_into:
                k1[rule.target] = k1.get(rule.target, 0) + amount
            else:
                k1[rule.target] = amount
            entry.update(disposition="sent", target=rule.target, sent=amount,
                         transform="sign flipped" if rule.sign < 0 else ("summed" if rule.sums_into else ""))
        elif rule.kind == "attested":
            evidence = attest.get(rule.attest_key)
            if not evidence and reviewer_excluded(decisions.get(sid)):
                d = decisions[sid]
                entry.update(disposition="excluded-by-reviewer", reviewer=d["reviewer"], reason=d["reason"])
            elif not evidence:
                entry.update(disposition="BLOCKED")
                reasons.append(f"box {rule.box} ({sid} = {value}) needs preparer attestation "
                               f"'{rule.attest_key}': {rule.note}")
            else:
                problem = apply_attestation(k1, rule, value, evidence)
                if problem:
                    entry.update(disposition="BLOCKED")
                    reasons.append(f"box {rule.box}: {problem}")
                else:
                    entry.update(disposition="sent+attested", target=rule.target or rule.attest_key,
                                 attestation=rule.attest_key)
        elif rule.kind == "review":
            d = decisions.get(sid)
            if not reviewer_excluded(d):
                entry.update(disposition="BLOCKED", note=rule.note)
                reasons.append(f"box {rule.box} ({sid} = {value}) needs a reviewer decision: {rule.note}")
            else:
                entry.update(disposition="excluded-by-reviewer", reviewer=d["reviewer"], reason=d["reason"])
        else:
            entry.update(disposition="info", note=rule.note)
        ledger.append(entry)
    if reasons:
        raise Blocked("G3", reasons)
    return k1, ledger


def reviewer_excluded(d: dict | None) -> bool:
    return bool(d) and d.get("decision") == "exclude" and bool(d.get("reason")) and bool(d.get("reviewer"))


def apply_attestation(k1: dict, rule, value, evidence: dict) -> str | None:
    key = rule.attest_key
    if key == "box7_royalty_reporting":
        k1[rule.target] = value
        k1[key] = evidence
    elif key == "box11_code_s_nonportfolio_capital":
        split = evidence.get("short_term_gain_loss", 0) + evidence.get("long_term_gain_loss", 0)
        if abs(split - value) > TOLERANCE:
            return f"attested 11S short+long split {split} does not equal K-1 value {value}"
        k1[key] = evidence
    elif key == "foreign_tax":
        k1[rule.target] = abs(value)
        k1.update(evidence)
    else:
        return f"no handler for attestation {key}"
    return None


def g4_preflight(k1: dict, facts: dict) -> dict:
    reasons = []
    if (k1.get("box5_interest", 0) or k1.get("box6a_ordinary_dividends", 0)) and not facts.get("schedule_b_part_iii"):
        reasons.append("interest/dividends present: Schedule B Part III answers (foreign accounts, "
                       "foreign trust) are required; OpenTax drops Schedule B without them")
    if not (facts.get("filer") or {}).get("filing_status"):
        reasons.append("filer.filing_status missing")
    if reasons:
        raise Blocked("G4", reasons)
    return {"checked": ["schedule_b_part_iii", "filing_status"]}


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, binary: Path, state_dir: Path):
        # Resolve now: the engine runs with cwd=state_dir, where relative paths break.
        self.binary, self.state_dir = binary.resolve(), state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[dict] = []

    def run(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run([str(self.binary), *args], capture_output=True, text=True, cwd=self.state_dir)
        out = (proc.stdout + proc.stderr).strip()
        self.calls.append({"args": [a if not a.startswith("{") else "<json>" for a in args],
                           "exit": proc.returncode, "error": out if out.startswith("Error") else None})
        return proc.returncode, out

    def json(self, *args: str):
        code, out = self.run(*args)
        if code != 0 or out.startswith("Error"):
            raise Blocked("G5", [f"engine {' '.join(args[:2])}: {out[:400]}"])
        return json.loads(out)

    def version(self) -> str:
        return self.run("version")[1]


def build_return(engine: Engine, facts: dict, k1: dict, naive: bool) -> dict:
    rid = engine.json("return", "create", "--year", "2025")["returnId"]
    entries = [("general", {"filing_status": facts.get("filer", {}).get("filing_status", "single")})]
    if not naive and facts.get("schedule_b_part_iii"):
        entries.append(("schedule_b_part_iii", facts["schedule_b_part_iii"]))
    entries.append(("k1_partnership", k1))
    errors = []
    for node_type, data in entries:
        code, out = engine.run("form", "add", "--returnId", rid, "--node_type", node_type, json.dumps(data))
        if code != 0 or out.startswith("Error"):
            errors.append(f"{node_type}: {out[:600]}")
    if errors and not naive:
        raise Blocked("G5", errors)
    result = engine.json("return", "get", "--returnId", rid)
    result["_entry_errors"] = errors
    return result


def g6_node_failures(result: dict) -> list[str]:
    failures = []
    for w in result.get("warnings", []):
        if "EXECUTOR_NODE_FAILURE" not in w:
            continue
        m = re.search(r"EXECUTOR_NODE_FAILURE\] (\w+):", w)
        src = NODE_SOURCES.get(m.group(1), "unknown K-1 source") if m else "unknown K-1 source"
        failures.append(f"engine dropped {m.group(1) if m else 'a node'} (fed by {src}) — {w.split('compute() threw for node')[-1].split(':', 1)[-1].strip()}")
    if failures:
        raise Blocked("G6", failures)
    return [w for w in result.get("warnings", [])]


def g7_reconcile(result: dict, k1: dict) -> list[dict]:
    lines, rows, reasons = result.get("lines", {}), [], []
    for line, parts in RECONCILE.items():
        expected = 0.0
        for p in parts:
            sign = -1 if p.startswith("-") else 1
            expected += sign * float(k1.get(p.lstrip("-"), 0) or 0)
        if line == "line7_capital_gain" and "box11_code_s_nonportfolio_capital" in k1:
            s = k1["box11_code_s_nonportfolio_capital"]
            expected += s.get("short_term_gain_loss", 0) + s.get("long_term_gain_loss", 0)
        actual = float(lines.get(line, 0) or 0)
        sources = [p.lstrip("-") for p in parts if k1.get(p.lstrip("-"))]
        if any(float(k1.get(p.lstrip("-"), 0) or 0) < 0 for p in parts):
            rows.append({"line": line, "expected": expected, "actual": actual, "status": "manual",
                         "note": "losses present: limitation rules apply, not auto-reconciled"})
            continue
        ok = abs(expected - actual) <= TOLERANCE
        rows.append({"line": line, "expected": expected, "actual": actual, "sources": sources,
                     "status": "ok" if ok else "MISMATCH", "gap": round(actual - expected, 2)})
        if not ok:
            reasons.append(f"{line}: engine {actual:,.0f} vs K-1 {expected:,.0f} "
                           f"(gap {actual - expected:,.0f}; from {', '.join(sources) or 'none'})")
    if reasons:
        raise Blocked("G7", reasons)
    return rows


# ---------------------------------------------------------------- orchestration
def run(args) -> int:
    otd_path, out = Path(args.otd), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    facts = yaml.safe_load(Path(args.facts).read_text()) if args.facts else {}
    doc = yaml.safe_load(otd_path.read_text())
    nodes = list(otd_nodes(doc["body"]))
    by_id = {n["semantic"]["id"]: n["value"] for n in nodes}

    audit = {
        "bridge_version": BRIDGE_VERSION,
        "mode": "naive" if args.naive else "governed",
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "inputs": {"otd": {"path": str(otd_path), "sha256": sha256(otd_path),
                           "document_id": doc["otd"]["document_id"],
                           "taxonomy": f"{doc['otd']['taxonomy']['id']}@{doc['otd']['taxonomy']['version']}"},
                   "facts": {"path": args.facts, "sha256": sha256(Path(args.facts)) if args.facts else None,
                             "attested_by": facts.get("attested_by"), "attested_at": str(facts.get("attested_at"))},
                   "engine": {"binary": args.engine, "sha256": sha256(Path(args.engine)),
                              "commit": args.engine_commit}},
        "gates": {},
    }
    engine = Engine(Path(args.engine), Path(tempfile.mkdtemp(prefix="engine-", dir=out)))
    ledger, result = [], None
    try:
        if args.naive:
            k1, ledger = g3_dispositions(nodes, {}, naive=True)
            ident = facts.get("identity") or {}
            k1.update(partnership_name=by_id["partnership.name_address"].split("\n")[0],
                      partnership_ein=digits(ident.get("partnership_ein", "")),
                      recipient_tin=digits(ident.get("recipient_tin", "")),
                      source_document_reference=f"otd:{doc['otd']['document_id']}")
            result = build_return(engine, facts, k1, naive=True)
            audit["verdict"] = "REPORTED"  # a naive wrapper reports whatever the engine says
        else:
            audit["gates"]["G1_otd_valid"] = g1_validate(otd_path, Path(args.otd_validator) if args.otd_validator else None,
                                                         args.python)
            ident = g2_identity(by_id, facts)
            audit["gates"]["G2_identity"] = {**ident, "partnership_ein": "*****" + ident["partnership_ein"][-4:],
                                             "recipient_tin": "*****" + ident["recipient_tin"][-4:]}
            k1, ledger = g3_dispositions(nodes, facts, naive=False)
            audit["gates"]["G3_dispositions"] = "every non-empty value classified"
            k1.update(partnership_name=by_id["partnership.name_address"].split("\n")[0],
                      partnership_ein=ident["partnership_ein"],  # engine internals need bare digits
                      recipient_tin=ident["recipient_tin"],
                      source_document_reference=f"otd:{doc['otd']['document_id']}")
            audit["gates"]["G4_preflight"] = g4_preflight(k1, facts)
            result = build_return(engine, facts, k1, naive=False)
            audit["gates"]["G5_engine_accepted"] = "all entries accepted"
            audit["engine_warnings"] = g6_node_failures(result)
            audit["gates"]["G6_no_node_failures"] = "no EXECUTOR_NODE_FAILURE"
            audit["reconciliation"] = g7_reconcile(result, k1)
            audit["gates"]["G7_reconciled"] = "all reconcilable lines match"
            excluded = [e for e in ledger if e.get("disposition") == "excluded-by-reviewer"]
            audit["verdict"] = "COMPUTED_WITH_EXCLUSIONS" if excluded else "COMPLETE"
    except Blocked as b:
        audit["verdict"] = "BLOCKED"
        audit["blocked"] = {"gate": b.gate, "reasons": b.reasons}
    audit["engine_version"] = engine.version()
    audit["engine_calls"] = engine.calls
    audit["ledger"] = ledger
    if result is not None:
        audit["summary"] = result.get("summary")
        audit["engine_warnings_raw"] = result.get("warnings", [])
        (out / "engine_return.json").write_text(json.dumps(result, indent=2))
    (out / "audit.json").write_text(json.dumps(audit, indent=2, default=str))
    (out / "audit.md").write_text(render_md(audit))
    print(render_console(audit))
    return {"COMPLETE": 0, "COMPUTED_WITH_EXCLUSIONS": 3, "REPORTED": 0, "BLOCKED": 2}[audit["verdict"]]


def money(v) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def render_console(a: dict) -> str:
    s = a.get("summary") or {}
    lines = [f"[{a['mode']}] verdict: {a['verdict']}"]
    if a["verdict"] == "BLOCKED":
        lines.append(f"  blocked at {a['blocked']['gate']}:")
        lines += [f"   - {r}" for r in a["blocked"]["reasons"]]
    if s and a["verdict"] != "BLOCKED":
        lines.append(f"  total income {money(s.get('line9_total_income'))}  AGI {money(s.get('line11_agi'))}  "
                     f"total tax {money(s.get('line24_total_tax'))}")
    if a["mode"] == "naive" and a.get("engine_warnings_raw"):
        lines.append(f"  ({len(a['engine_warnings_raw'])} engine warning(s) not inspected)")
    return "\n".join(lines)


def render_md(a: dict) -> str:
    md = [f"# K-1 → Form 1040 bridge audit", "",
          f"- **Verdict:** `{a['verdict']}`  ·  mode `{a['mode']}`  ·  bridge {a['bridge_version']}  ·  {a['run_at']}",
          f"- **OTD:** `{a['inputs']['otd']['document_id']}` ({a['inputs']['otd']['taxonomy']}), sha256 `{a['inputs']['otd']['sha256'][:16]}…`",
          f"- **Facts:** attested by {a['inputs']['facts']['attested_by']} on {a['inputs']['facts']['attested_at']}",
          f"- **Engine:** OpenTax `{a.get('engine_version')}` commit `{a['inputs']['engine']['commit']}`, sha256 `{a['inputs']['engine']['sha256'][:16]}…`", ""]
    if a["verdict"] == "BLOCKED":
        md += [f"## Blocked at {a['blocked']['gate']}", ""] + [f"- {r}" for r in a["blocked"]["reasons"]] + [""]
    if a.get("gates"):
        md += ["## Gates passed", ""] + [f"- **{k}**: {v if isinstance(v, str) else json.dumps(v)}" for k, v in a["gates"].items()] + [""]
    if a.get("summary") and a["verdict"] != "BLOCKED":
        md += ["## Result", "", "| Line | Amount |", "|---|---:|"]
        md += [f"| {k} | {money(v)} |" for k, v in a["summary"].items()] + [""]
    if a.get("reconciliation"):
        md += ["## Reconciliation (K-1 → 1040)", "", "| Line | From K-1 | Engine | Status |", "|---|---:|---:|---|"]
        md += [f"| {r['line']} | {money(r['expected'])} | {money(r['actual'])} | {r['status']} |" for r in a["reconciliation"]] + [""]
    if a.get("ledger"):
        md += ["## Disposition of every K-1 value", "", "| Box | OTD id | Value | Disposition | Detail |", "|---|---|---:|---|---|"]
        for e in a["ledger"]:
            if e["disposition"] in ("identity", "empty"):
                continue
            detail = e.get("target") or e.get("reason") or e.get("note") or ""
            if e.get("reviewer"):
                detail = f"{e['reviewer']}: {detail}"
            md += [f"| {e['box']} | {e['semantic_id']} | {money(e['value'])} | {e['disposition']} | {detail} |"]
        md += [""]
    if a.get("engine_warnings_raw"):
        md += ["## Engine warnings", ""] + [f"- {w}" for w in a["engine_warnings_raw"]] + [""]
    return "\n".join(md)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="OTD K-1 -> OpenTax 1040 bridge with governance gates")
    p.add_argument("--otd", required=True, help="OTD K-1 YAML document")
    p.add_argument("--facts", help="preparer facts / attestations YAML")
    p.add_argument("--out", required=True, help="output directory for audit and engine state")
    p.add_argument("--engine", default=os.environ.get("OPENTAX_BIN", "opentax"))
    p.add_argument("--engine-commit", default=os.environ.get("OPENTAX_COMMIT", "unknown"))
    p.add_argument("--otd-validator", default=os.environ.get("OTD_VALIDATOR"))
    p.add_argument("--python", default=sys.executable, help="python used to run the OTD validator")
    p.add_argument("--naive", action="store_true", help="behave like an ungoverned wrapper (demo contrast)")
    args = p.parse_args(argv)
    engine = Path(args.engine) if os.sep in args.engine else Path(shutil.which(args.engine) or args.engine)
    if not (engine.is_file() and os.access(engine, os.X_OK)):
        print(f"error: OpenTax engine not found or not executable: {args.engine}", file=sys.stderr)
        return 1
    args.engine = str(engine.resolve())
    for name in ("otd", "facts"):
        if getattr(args, name) and not Path(getattr(args, name)).is_file():
            print(f"error: --{name} file not found: {getattr(args, name)}", file=sys.stderr)
            return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
