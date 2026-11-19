"""Isolated activation rehearsal: real processes, synthetic artifacts, no live service.

  python -m src.signals_display.activation_rehearsal rehearse --workdir DIR
  python -m src.signals_display.activation_rehearsal serve --port P --artifact A --marker M [--fail-for SHA]

The rehearsal runs a stand-in service process per scenario. It loads the artifact file it
was given, reports that file's sha256, and can be made unhealthy for one artifact. The
ProcessRestarter stops and starts ONLY the process whose pid it recorded and whose command
line carries the rehearsal marker. Nothing here reads or writes Oracle, the live journal,
the production model, or any port other than the rehearsal's own ephemeral ports.

What is REAL in a scenario: artifact verification, the atomic pointer, the process restart,
the model-file check, the restore, the controller's history and recovery. What is
INJECTED: post-switch failure samples in the automatic-rollback scenario (the restart and
restore after them are real).
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import guarded_activator as ga
from . import lifecycle_runner as lr
from . import model_lifecycle as lc
from . import opening_models
from . import promotion as pr

MARKER = "kxbtc-rehearsal-service"
REPO = Path(__file__).resolve().parents[2]
SAFE_NOW = datetime(2026, 11, 19, 3, 4, 0, tzinfo=timezone.utc)   # 4 min after a boundary: outside the window


# ---------- stand-in service ---------------------------------------------------------------------

def serve(port, artifact, fail_for=None):
    sha = ga.file_sha(artifact)
    healthy = sha != fail_for

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/healthz"):
                body = {"status": "HEALTHY" if healthy else "DEGRADED", "signals_only": True,
                        "workers": {"healthy": healthy}, "model_file_sha": sha, "build_commit": "rehearsal"}
            elif self.path.startswith("/readyz"):
                body = {"status": "READY" if healthy else "NOT_READY", "signals_only": True}
            else:
                body = {}
            data = json.dumps(body).encode()
            status = 200 if (healthy or not self.path.startswith(("/healthz", "/readyz"))) else 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ---------- restart and probe --------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class ProcessRestarter:
    """Restarts only the rehearsal process it started (pid recorded, marker verified)."""

    def __init__(self, port, pidfile, log, python=sys.executable):
        self.port, self.pidfile, self.log, self.python = port, Path(pidfile), Path(log), python
        self.fail_for = None
        self.restarts = 0

    def _stop(self):
        try:
            pid = int(self.pidfile.read_text())
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError):
            return
        if MARKER.encode() not in cmd:
            return                                        # never signal a process we did not start
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists():
                return
            time.sleep(0.1)

    def __call__(self, artifact_path):
        self._stop()
        args = [self.python, "-m", "src.signals_display.activation_rehearsal", "serve", "--port", str(self.port),
                "--artifact", str(artifact_path), "--marker", MARKER]
        if self.fail_for:
            args += ["--fail-for", self.fail_for]
        with open(self.log, "ab") as log:
            proc = subprocess.Popen(args, cwd=REPO, stdout=log, stderr=log, start_new_session=True)
        self.pidfile.write_text(str(proc.pid))
        self.restarts += 1
        for _ in range(100):
            if _listening(self.port):
                return True
            time.sleep(0.05)
        return False


def probe_port(port):
    return lr.probe_live(port)


# ---------- synthetic artifacts ------------------------------------------------------------------

def _synthetic_rows(n=200):
    from datetime import timedelta
    rows = []
    for i in range(n):
        start = datetime(2026, 10, 20, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
        y = i % 2
        rows.append(dict(
            ticker=f"KXBTC15M-R{i}", timestamp=start.isoformat(), opens_at=start.isoformat(),
            closes_at=(start + timedelta(minutes=15)).isoformat(),
            label_available_at=(start + timedelta(minutes=15, seconds=1)).isoformat(),
            features={"distance_scaled": (2 * y - 1) * 0.001, "return_1m": (2 * y - 1) * 0.01},
            outcome=y, baseline=0.5, market=None, remaining=900, regime="rehearsal"))
    return rows


def make_artifact(label):
    candidate, _report = opening_models.evaluate(_synthetic_rows(), used_test_contracts=set())
    if candidate is None:
        raise RuntimeError("synthetic training produced no artifact")
    candidate.update(created_at="2026-11-01T00:00:00+00:00", rehearsal_label=label)
    return candidate


def store_candidate(state_dir, artifact, cutoff):
    body = {"format": lc.FORMAT, "kind": "CANDIDATE", "training_cutoff": cutoff.isoformat(),
            "commit": "rehearsal", "artifact": artifact}
    body["checksum"] = lc.canonical_checksum(body)
    cand = state_dir / "candidates" / f"candidate-{body['checksum'][:16]}.json"
    cand_sha = lc.write_once(cand, body)
    report = {"format": lc.FORMAT, "kind": "EVALUATION", "candidate_checksum": body["checksum"],
              "candidate_file_sha256": cand_sha, "decision": "CANDIDATE_QUALIFIED"}
    report["checksum"] = lc.canonical_checksum(report)
    lc.write_once(state_dir / "reports" / f"report-{report['checksum'][:16]}.json", report)
    return {"decision": "CANDIDATE_QUALIFIED", "candidate_file": str(cand),
            "candidate_checksum": body["checksum"], "report_checksum": report["checksum"]}, body


# ---------- scenarios ----------------------------------------------------------------------------

RESOURCES = {"mem_mib": 4096, "load1": 1.0, "disk_mib": 20000}


class Harness:
    def __init__(self, workdir, python=sys.executable):
        self.root = Path(workdir)
        self.python = python
        self.prod = make_artifact("production")
        self.cand = make_artifact("candidate")
        self.prod_path = self.root / "prod" / "signals_opening_model.json"
        self.prod_path.parent.mkdir(parents=True, exist_ok=True)
        self.prod_path.write_text(json.dumps(self.prod, sort_keys=True))
        self.results = {}
        self.services = []

    def fresh(self, name, authorized=True):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        config = {"restart": "rehearsal-process", "port": port}
        auth = d / "promotion-authorization.txt"
        if authorized:
            auth.write_text(ga.AUTH_PREFIX + ga.activator_checksum(config))
        restarter = ProcessRestarter(port, d / "service.pid", d / "service.log", self.python)
        act = ga.GuardedActivator(d, authorization_file=auth, config=config, restarter=restarter,
                                  probe=lambda: probe_port(port), clock=lambda: SAFE_NOW, sleep=lambda s: None)
        restarter(self.prod_path)                    # start the service on the production model
        self.services.append(restarter)
        act.bootstrap(self.prod_path)
        cand, _ = store_candidate(d, self.cand, SAFE_NOW)     # candidate and report live in the scenario's own state
        return {"dir": d, "port": port, "restarter": restarter, "act": act, "cand": cand}

    def probe(self, env):
        return probe_port(env["port"])

    def cycle(self, env, status, post=None):
        return pr.promotion_cycle(env["dir"], status, env["act"], live_artifact_path=self.prod_path,
                                  health_samples=[self.probe(env)], resources=RESOURCES, telegram_ok=True,
                                  seconds_to_opening=600, notify=lambda e, d: None, now=SAFE_NOW,
                                  post_samples=post)

    def check(self, name, ok, detail):
        self.results[name] = {"pass": bool(ok), "detail": detail}

    def run(self):
        cand_sha = ga.file_sha(store_candidate(self.root / "cand_probe", self.cand, SAFE_NOW)[0]["candidate_file"])
        prod_sha = ga.file_sha(self.prod_path)

        # S1 successful activation with verified model file
        e = self.fresh("s1_success")
        try:
            res = self.cycle(e, e["cand"])
            served = self.probe(e).get("model_file_sha")
            self.check("S1_success_activation",
                       res["result"] == "PROMOTED" and served == cand_sha
                       and json.loads((e["dir"] / ga.POINTER).read_text())["file_sha"] == cand_sha
                       and pr.History(e["dir"]).verify_chain(),
                       {"result": res, "served_model_sha": served})
        finally:
            e["restarter"]._stop()

        # S2 post-switch verification fails: previous model restored and verified
        e = self.fresh("s2_failed_activation")
        try:
            e["restarter"].fail_for = cand_sha           # the new model reports unhealthy
            res = self.cycle(e, e["cand"])
            served = self.probe(e).get("model_file_sha")
            self.check("S2_failed_activation_restores_previous",
                       res["result"] == "ROLLED_BACK" and served == prod_sha
                       and any(x["event"] == "ACTIVATION_FAILED" for x in pr.History(e["dir"]).entries()),
                       {"result": res, "served_model_sha": served})
        finally:
            e["restarter"]._stop()

        # S3 automatic rollback after a promotion, driven by sustained post-switch failures
        e = self.fresh("s3_automatic_rollback")
        try:
            bad = [{"health": "DEGRADED", "ready": "NOT_READY", "signals_only": True}] * 3
            res = self.cycle(e, e["cand"], post=lambda: bad)
            served = self.probe(e).get("model_file_sha")
            self.check("S3_automatic_rollback",
                       res["result"] == "ROLLED_BACK" and served == prod_sha
                       and any(x["event"] == "ROLLED_BACK" for x in pr.History(e["dir"]).entries()),
                       {"result": res, "served_model_sha": served, "injected_failure_samples": 3})
        finally:
            e["restarter"]._stop()

        # S4 crash recovery: crashed after the switch -> resolved as promoted, never promoted twice
        e = self.fresh("s4_crash_after_switch")
        try:
            e["act"]._switch_to({"checksum": e["cand"]["candidate_checksum"], "file_sha": cand_sha,
                                 "artifact": e["cand"]["candidate_file"]})
            pr.History(e["dir"]).append("PROMOTION_STARTED", {"candidate_checksum": e["cand"]["candidate_checksum"],
                                                             "previous_checksum": "production:" + prod_sha})
            res = self.cycle(e, e["cand"])
            restarts_before = e["restarter"].restarts
            again = self.cycle(e, e["cand"])
            self.check("S4_crash_recovery_no_double_promotion",
                       res["result"] == "ALREADY_PROMOTED" and again["result"] == "ALREADY_PROMOTED"
                       and e["restarter"].restarts == restarts_before,
                       {"first": res, "second": again})
        finally:
            e["restarter"]._stop()

        # S5 crash before the switch: resolved as aborted, then the candidate can still be promoted
        e = self.fresh("s5_crash_before_switch")
        try:
            pr.History(e["dir"]).append("PROMOTION_STARTED", {"candidate_checksum": e["cand"]["candidate_checksum"],
                                                             "previous_checksum": "production:" + prod_sha})
            res = self.cycle(e, e["cand"])
            events = [x["event"] for x in pr.History(e["dir"]).entries()]
            self.check("S5_crash_before_switch_recovers",
                       "RECOVERED_ABORTED" in events and res["result"] == "PROMOTED",
                       {"result": res, "events_tail": events[-3:]})
        finally:
            e["restarter"]._stop()

        # S6 unknown live model after an interruption: stop, change nothing
        e = self.fresh("s6_unknown_live")
        try:
            (e["dir"] / ga.POINTER).unlink()
            pr.History(e["dir"]).append("PROMOTION_STARTED", {"candidate_checksum": e["cand"]["candidate_checksum"],
                                                             "previous_checksum": "production:" + prod_sha})
            res = self.cycle(e, e["cand"])
            self.check("S6_unknown_live_model_blocks", res["result"] == "BLOCKED" and e["restarter"].restarts == 1,
                       {"result": res})
        finally:
            e["restarter"]._stop()

        # S7 authorization missing: disabled, nothing changes
        e = self.fresh("s7_not_authorized", authorized=False)
        try:
            res = self.cycle(e, e["cand"])
            self.check("S7_no_authorization_no_change",
                       res["result"] == "BLOCKED" and "ACTIVATION_DISABLED" in res["detail"]
                       and self.probe(e).get("model_file_sha") == prod_sha, {"result": res})
        finally:
            e["restarter"]._stop()

        # S8 not qualified: never promoted
        e = self.fresh("s8_not_qualified")
        try:
            res = self.cycle(e, dict(e["cand"], decision="REJECTED_PERFORMANCE"))
            self.check("S8_not_qualified_never_promoted",
                       res["result"] == "NOT_PROMOTED" and self.probe(e).get("model_file_sha") == prod_sha,
                       {"result": res})
        finally:
            e["restarter"]._stop()

        ok = all(v["pass"] for v in self.results.values())
        return {"result": "PASS" if ok else "FAIL", "scenarios": self.results,
                "live_service_touched": False, "ports_used": "ephemeral rehearsal ports only"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rehearse")
    r.add_argument("--workdir", type=Path, required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, required=True)
    sv.add_argument("--artifact", "--opening-model-artifact", dest="artifact", type=Path, required=True)
    sv.add_argument("--marker", required=True)
    sv.add_argument("--fail-for")
    args = p.parse_args(argv)
    if args.cmd == "serve":
        serve(args.port, args.artifact, args.fail_for)
        return
    out = Harness(args.workdir).run()
    print(json.dumps(out, indent=1, sort_keys=True, default=str))
    raise SystemExit(0 if out["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
