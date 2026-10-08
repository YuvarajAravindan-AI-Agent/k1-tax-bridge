"""Public live demo for the K-1 bridge: fixed synthetic scenarios, real runs.

Visitors choose a scenario id from a fixed list; nothing they send reaches the
bridge or the engine except that id. Each run happens in a fresh temp dir.

    python app.py            # serves on 127.0.0.1:8320 (Caddy maps /k1-bridge/)
"""
import copy
import json
import os
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
BRIDGE = Path(os.environ.get("K1_BRIDGE_DIR", "/opt/opentax-test/bridge"))
PYTHON = os.environ.get("K1_PYTHON", "/opt/opentax-test/venv/bin/python")
ENGINE = os.environ.get("OPENTAX_BIN", "/opt/opentax-test/opentax/opentax")
VALIDATOR = os.environ.get("OTD_VALIDATOR", "/opt/opentax-test/otd-spec/skills/k1-otd/scripts/validate_otd.py")
ENGINE_COMMIT = os.environ.get("OPENTAX_COMMIT", "0c6ae76")
OTD = BRIDGE / "tests/fixtures/synthetic-k1.otd.yaml"
FACTS = BRIDGE / "demo/facts.synthetic.yaml"

RUN_SLOTS = threading.BoundedSemaphore(2)  # 4-core box shared with other services
PER_IP = 12  # runs per IP per 10 minutes
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


# ------------------------------------------------------------------ scenarios
def _drop(path):
    def f(facts, otd):
        d = facts
        for k in path[:-1]:
            d = d[k]
        d.pop(path[-1])
    return f


def _set(path, value):
    def f(facts, otd):
        d = facts
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = value
    return f


def _tamper(facts, otd):
    otd["text"] = otd["text"].replace("value: 123500.0", "value: 999999999.0", 1)


SCENARIOS = {
    "naive": dict(title="Plain wrapper", gate=None, naive=True,
                  what="Copies the K-1 values that fit straight into the engine and reports whatever comes back. "
                       "No gates, no warnings read."),
    "governed": dict(title="Governed, full preparer facts", gate=None,
                     what="All seven gates on, with the complete synthetic preparer facts: identity, Schedule B answers, "
                          "attestations and named reviewer decisions."),
    "no_schedule_b": dict(title="Schedule B answers withheld", gate="G4", mutate=_drop(["schedule_b_part_iii"]),
                          what="The preparer facts omit the foreign-account and foreign-trust answers. "
                               "The engine would silently drop Schedule B (−$687,000 of income)."),
    "wrong_ein": dict(title="Partnership EIN mistyped", gate="G2",
                      mutate=_set(["identity", "partnership_ein"], "99-0065188"),
                      what="The preparer typed …188 but the K-1 shows …189 in its masked EIN."),
    "missing_review": dict(title="Reviewer decision missing", gate="G3",
                           mutate=_drop(["review_decisions", "foreign_taxes_paid_accrued"]),
                           what="Nobody has decided what to do with box 21 foreign taxes ($267,500). "
                                "The bridge refuses to drop it silently."),
    "split_mismatch": dict(title="Attested split doesn't add up", gate="G3",
                           mutate=_set(["attestations", "box11_code_s_nonportfolio_capital", "long_term_gain_loss"], 700000),
                           what="The preparer's box 11S short/long-term split totals $700,000; the K-1 says $738,700."),
    "tampered": dict(title="K-1 document tampered", gate="G1", tamper=True,
                     what="Qualified dividends edited to $999,999,999, more than total ordinary dividends. "
                          "The K-1 standard's own validator rejects it."),
}


def run_scenario(sid: str) -> dict:
    sc = SCENARIOS[sid]
    facts = yaml.safe_load(FACTS.read_text())
    otd = {"text": OTD.read_text()}
    if sc.get("mutate"):
        sc["mutate"](facts, otd)
    if sc.get("tamper"):
        _tamper(facts, otd)
    with tempfile.TemporaryDirectory(prefix="k1demo-") as tmp:
        tmp = Path(tmp)
        (tmp / "facts.yaml").write_text(yaml.safe_dump(facts, sort_keys=False))
        (tmp / "k1.otd.yaml").write_text(otd["text"])
        cmd = [PYTHON, "-m", "k1bridge.bridge", "--otd", str(tmp / "k1.otd.yaml"), "--facts", str(tmp / "facts.yaml"),
               "--out", str(tmp / "out"), "--engine", ENGINE, "--engine-commit", ENGINE_COMMIT,
               "--otd-validator", VALIDATOR, "--python", PYTHON]
        if sc.get("naive"):
            cmd.append("--naive")
        t0 = time.monotonic()
        proc = subprocess.run(cmd, cwd=BRIDGE, capture_output=True, text=True, timeout=90,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8"})
        elapsed = time.monotonic() - t0
        audit_path = tmp / "out/audit.json"
        if not audit_path.exists():
            raise RuntimeError(f"bridge exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
        audit = json.loads(audit_path.read_text())
    return shape(sid, audit, proc.returncode, elapsed)


def shape(sid: str, a: dict, exit_code: int, elapsed: float) -> dict:
    """Public view of an audit: no local paths, no IDs beyond the masked digits."""
    order = ["G1_otd_valid", "G2_identity", "G3_dispositions", "G4_preflight",
             "G5_engine_accepted", "G6_no_node_failures", "G7_reconciled"]
    blocked = a.get("blocked", {}).get("gate")
    gates = []
    for key in order:
        g = key[:2]
        status = "pass" if key in a.get("gates", {}) else ("blocked" if g == blocked else "not run")
        gates.append({"gate": g, "name": key[3:].replace("_", " "), "status": status})
    ledger = [{k: e.get(k) for k in ("box", "semantic_id", "value", "disposition", "target", "reviewer", "reason", "note")}
              for e in a.get("ledger", []) if e.get("disposition") not in ("identity", "empty")]
    return {
        "scenario": sid, "title": SCENARIOS[sid]["title"], "what": SCENARIOS[sid]["what"],
        "expected_gate": SCENARIOS[sid]["gate"], "mode": a["mode"], "verdict": a["verdict"],
        "exit_code": exit_code, "seconds": round(elapsed, 2), "run_at": a["run_at"],
        "blocked": a.get("blocked"), "gates": gates if a["mode"] == "governed" else [],
        "summary": a.get("summary") if a["verdict"] != "BLOCKED" else None,
        "reconciliation": a.get("reconciliation", []), "ledger": ledger,
        "engine_warnings": a.get("engine_warnings_raw", []),
        "engine": {"version": a.get("engine_version"), "commit": a["inputs"]["engine"]["commit"],
                   "sha256": a["inputs"]["engine"]["sha256"][:16]},
        "otd": {"document_id": a["inputs"]["otd"]["document_id"], "taxonomy": a["inputs"]["otd"]["taxonomy"],
                "sha256": a["inputs"]["otd"]["sha256"][:16]},
    }


# ------------------------------------------------------------------ http
def client_ip(h) -> str:
    fwd = h.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or h.client_address[0]


def allow(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= PER_IP:
            return False
        q.append(now)
        return True


TYPES = {".html": "text/html; charset=utf-8", ".mp4": "video/mp4", ".vtt": "text/vtt", ".jpg": "image/jpeg",
         ".png": "image/png", ".svg": "image/svg+xml", ".css": "text/css", ".js": "text/javascript"}


class Handler(BaseHTTPRequestHandler):
    server_version = "k1demo"
    sys_version = ""

    def log_message(self, fmt, *args):  # one line per request, no query strings with personal data exist
        print(f"{client_ip(self)} {fmt % args}", flush=True)

    def send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        cache = {"application/json": "no-store", "text/html; charset=utf-8": "no-cache"}.get(ctype, "public, max-age=300")
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code, obj):
        self.send(code, json.dumps(obj).encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/scenarios":
            return self.json(200, [{"id": k, "title": v["title"], "what": v["what"], "gate": v["gate"]}
                                   for k, v in SCENARIOS.items()])
        if url.path == "/api/health":
            return self.json(200, {"ok": True})
        if url.path == "/api/run":
            return self.json(405, {"error": "use POST"})
        name = "index.html" if url.path in ("", "/") else url.path.lstrip("/")
        f = (STATIC / name).resolve()
        if STATIC not in f.parents or not f.is_file():
            return self.send(404, b"not found", "text/plain")
        if f.suffix == ".mp4":
            return self.send_video(f)
        return self.send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))

    def send_video(self, f: Path):
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else size - 1
            end = min(end, size - 1, start + 4 * 1024 * 1024 - 1)
        with open(f, "rb") as fh:
            fh.seek(start)
            body = fh.read(end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/run":
            return self.json(404, {"error": "not found"})
        sid = (parse_qs(url.query).get("scenario") or [""])[0]
        if sid not in SCENARIOS:
            return self.json(400, {"error": "unknown scenario"})
        if not allow(client_ip(self)):
            return self.json(429, {"error": "Too many runs from your address. Try again in a few minutes."})
        if not RUN_SLOTS.acquire(timeout=30):
            return self.json(503, {"error": "The demo is busy. Try again in a moment."})
        try:
            return self.json(200, run_scenario(sid))
        except Exception as e:  # report, never leak a traceback
            print("run failed:", sid, repr(e)[:400], flush=True)
            return self.json(500, {"error": "The run failed on the server. It has been logged."})
        finally:
            RUN_SLOTS.release()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8320"))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
