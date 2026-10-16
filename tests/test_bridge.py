"""Gate tests. Unit tests need only PyYAML; integration tests run when
OPENTAX_BIN and OTD_VALIDATOR point at a real engine and validator."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from k1bridge import bridge
from k1bridge.bridge import Blocked
from k1bridge.mapping import RULES

ROOT = Path(__file__).resolve().parents[1]
OTD = ROOT / "tests/fixtures/synthetic-k1.otd.yaml"
FACTS = ROOT / "demo/facts.synthetic.yaml"


@pytest.fixture
def doc():
    return yaml.safe_load(OTD.read_text())


@pytest.fixture
def facts():
    return yaml.safe_load(FACTS.read_text())


def nodes_and_ids(doc):
    nodes = list(bridge.otd_nodes(doc["body"]))
    return nodes, {n["semantic"]["id"]: n["value"] for n in nodes}


# ---------------------------------------------------------------- G2 identity
def test_identity_passes_with_matching_digits(doc, facts):
    _, by_id = nodes_and_ids(doc)
    assert bridge.g2_identity(by_id, facts)["partnership_ein"] == "990065189"


def test_identity_blocks_when_digits_differ_from_k1(doc, facts):
    _, by_id = nodes_and_ids(doc)
    facts["identity"]["partnership_ein"] = "99-0065188"
    with pytest.raises(Blocked) as e:
        bridge.g2_identity(by_id, facts)
    assert e.value.gate == "G2" and "partnership_ein" in e.value.reasons[0]


def test_identity_blocks_non_individual_without_override(doc, facts):
    _, by_id = nodes_and_ids(doc)
    del facts["recipient_override"]
    with pytest.raises(Blocked, match="not an individual"):
        bridge.g2_identity(by_id, facts)


# ---------------------------------------------------------------- G3 dispositions
def test_every_value_gets_a_disposition(doc, facts):
    nodes, _ = nodes_and_ids(doc)
    k1, ledger = bridge.g3_dispositions(nodes, facts, naive=False)
    assert len(ledger) == len(nodes)
    assert not [e for e in ledger if e["disposition"] in ("BLOCKED", "UNCLASSIFIED")]
    assert k1["box5_interest"] == 434000
    assert "box12_section_179" not in k1  # reviewer-excluded values never reach the engine


def test_unknown_otd_item_blocks(doc, facts):
    nodes, _ = nodes_and_ids(doc)
    nodes.append({"semantic": {"id": "other_income.brand_new_code"}, "value": 1.0})
    with pytest.raises(Blocked, match="brand_new_code"):
        bridge.g3_dispositions(nodes, facts, naive=False)


@pytest.mark.parametrize("sid", [s for s, r in RULES.items() if r.kind == "review"])
def test_missing_review_decision_blocks(doc, facts, sid):
    nodes, by_id = nodes_and_ids(doc)
    if bridge.is_empty(by_id.get(sid)):
        pytest.skip(f"{sid} empty on the synthetic K-1")
    facts["review_decisions"].pop(sid, None)
    with pytest.raises(Blocked) as e:
        bridge.g3_dispositions(nodes, facts, naive=False)
    assert any(sid in r for r in e.value.reasons)


def test_decision_without_reviewer_blocks(doc, facts):
    nodes, _ = nodes_and_ids(doc)
    del facts["review_decisions"]["foreign_taxes_paid_accrued"]["reviewer"]
    with pytest.raises(Blocked, match="foreign_taxes_paid_accrued"):
        bridge.g3_dispositions(nodes, facts, naive=False)


def test_missing_attestation_blocks(doc, facts):
    nodes, _ = nodes_and_ids(doc)
    del facts["attestations"]["box7_royalty_reporting"]
    with pytest.raises(Blocked, match="box 7"):
        bridge.g3_dispositions(nodes, facts, naive=False)


def test_attested_split_must_reconcile_to_k1(doc, facts):
    nodes, _ = nodes_and_ids(doc)
    facts["attestations"]["box11_code_s_nonportfolio_capital"]["long_term_gain_loss"] = 700000
    with pytest.raises(Blocked, match="11S"):
        bridge.g3_dispositions(nodes, facts, naive=False)


def test_naive_mode_drops_without_complaint(doc):
    nodes, _ = nodes_and_ids(doc)
    _, ledger = bridge.g3_dispositions(nodes, {}, naive=True)
    assert any(e["disposition"] == "dropped" for e in ledger)


# ---------------------------------------------------------------- G4 preflight
def test_schedule_b_answers_required_with_interest(facts):
    del facts["schedule_b_part_iii"]
    with pytest.raises(Blocked, match="Schedule B"):
        bridge.g4_preflight({"box5_interest": 1.0}, facts)


# ---------------------------------------------------------------- G6 / G7
def test_node_failure_is_reported_in_k1_terms():
    result = {"warnings": ['[EXECUTOR_NODE_FAILURE] schedule_b: compute() threw for node "schedule_b": '
                           'Schedule B needs an explicit foreign-account answer']}
    with pytest.raises(Blocked) as e:
        bridge.g6_node_failures(result)
    assert e.value.gate == "G6" and "boxes 5 / 6a" in e.value.reasons[0]


def test_plain_warnings_pass_g6():
    assert bridge.g6_node_failures({"warnings": ["Schedule D is present but no Form 8949"]})


def test_reconcile_catches_missing_income():
    k1 = {"box5_interest": 434000, "box6a_ordinary_dividends": 253000}
    result = {"lines": {"line2b_taxable_interest": 0, "line3b_ordinary_dividends": 253000}}
    with pytest.raises(Blocked) as e:
        bridge.g7_reconcile(result, k1)
    assert "line2b_taxable_interest" in e.value.reasons[0] and "-434,000" in e.value.reasons[0]


def test_reconcile_skips_lines_with_losses():
    rows = bridge.g7_reconcile({"lines": {"line7_capital_gain": -3000}}, {"box8_net_st_cap_gain": -50000})
    assert next(r for r in rows if r["line"] == "line7_capital_gain")["status"] == "manual"


# ---------------------------------------------------------------- integration
ENGINE = os.environ.get("OPENTAX_BIN")
VALIDATOR = os.environ.get("OTD_VALIDATOR")
needs_engine = pytest.mark.skipif(not (ENGINE and VALIDATOR), reason="set OPENTAX_BIN and OTD_VALIDATOR")


def run_cli(tmp_path, otd=OTD, facts=FACTS, *extra):
    proc = subprocess.run([sys.executable, "-m", "k1bridge.bridge", "--otd", str(otd), "--facts", str(facts),
                           "--out", str(tmp_path / "out"), *extra], capture_output=True, text=True, cwd=ROOT)
    audit = json.loads((tmp_path / "out/audit.json").read_text())
    return proc.returncode, audit


@needs_engine
def test_governed_run_reconciles(tmp_path):
    code, audit = run_cli(tmp_path)
    assert code == 3 and audit["verdict"] == "COMPUTED_WITH_EXCLUSIONS"
    assert all(r["status"] == "ok" for r in audit["reconciliation"])
    assert audit["summary"]["line9_total_income"] == 3024900


@needs_engine
def test_relative_otd_path_validates(tmp_path):
    code, audit = run_cli(tmp_path, OTD.relative_to(ROOT))
    assert code == 3 and "G1_otd_valid" in audit["gates"]


@needs_engine
def test_relative_engine_path_works(tmp_path):
    rel = os.path.relpath(ENGINE, ROOT)
    code, audit = run_cli(tmp_path, OTD, FACTS, "--engine", rel)
    assert code == 3 and audit["verdict"] == "COMPUTED_WITH_EXCLUSIONS"


def test_missing_engine_is_a_clean_error(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "k1bridge.bridge", "--otd", str(OTD), "--facts", str(FACTS),
                           "--out", str(tmp_path / "out"), "--engine", "./no-such-opentax"],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 1 and "engine not found" in proc.stderr and "Traceback" not in proc.stderr


@needs_engine
def test_naive_run_reports_short_income(tmp_path):
    code, audit = run_cli(tmp_path, OTD, FACTS, "--naive")
    assert code == 0 and audit["summary"]["line9_total_income"] < 3024900
    assert any("EXECUTOR_NODE_FAILURE" in w for w in audit["engine_warnings_raw"])


@needs_engine
def test_missing_schedule_b_blocks_before_engine(tmp_path):
    f = yaml.safe_load(FACTS.read_text())
    del f["schedule_b_part_iii"]
    p = tmp_path / "facts.yaml"
    p.write_text(yaml.safe_dump(f))
    code, audit = run_cli(tmp_path, OTD, p)
    assert code == 2 and audit["blocked"]["gate"] == "G4" and "summary" not in audit


@needs_engine
def test_tampered_otd_blocks_at_validator(tmp_path):
    text = OTD.read_text().replace("value: 123500.0", "value: 999999999.0", 1)
    p = tmp_path / "tampered.otd.yaml"
    p.write_text(text)
    code, audit = run_cli(tmp_path, p)
    assert code == 2 and audit["blocked"]["gate"] == "G1"


# ------------------------------------------- BLOCKED leaves no figures (fake engine)
FAKE_ENGINE = '''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a[:2] == ["return", "create"]:
    print(json.dumps({"returnId": "r1"}))
elif a[:2] == ["return", "get"]:
    print(open(os.environ["FAKE_RESULT"]).read())
elif a[:1] == ["version"]:
    print("fake-engine")
'''
FAKE_RESULTS = {
    "G6": {"summary": {"line9_total_income": 1598400}, "lines": {},
           "warnings": ["[EXECUTOR_NODE_FAILURE] schedule_b: compute() threw for node schedule_b: missing answer"]},
    "G7": {"summary": {"line9_total_income": 1598400}, "lines": {}, "warnings": []},
}


@pytest.mark.parametrize("gate", ["G6", "G7"])
def test_blocked_after_engine_run_leaves_no_figures(tmp_path, monkeypatch, gate):
    engine = tmp_path / "opentax"
    engine.write_text(FAKE_ENGINE)
    engine.chmod(0o755)
    validator = tmp_path / "a/b/c/validate_otd.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("import sys; sys.exit(0)\n")
    result = tmp_path / "result.json"
    result.write_text(json.dumps(FAKE_RESULTS[gate]))
    monkeypatch.setenv("FAKE_RESULT", str(result))
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, "-m", "k1bridge.bridge", "--otd", str(OTD), "--facts", str(FACTS),
                           "--out", str(out), "--engine", str(engine), "--otd-validator", str(validator)],
                          capture_output=True, text=True, cwd=ROOT)
    audit = json.loads((out / "audit.json").read_text())
    assert proc.returncode == 2 and audit["verdict"] == "BLOCKED" and audit["blocked"]["gate"] == gate
    assert "summary" not in audit
    assert not (out / "engine_return.json").exists()
    for text in (proc.stdout, (out / "audit.json").read_text(), (out / "audit.md").read_text()):
        assert "1598400" not in text and "1,598,400" not in text
