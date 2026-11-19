"""Scheduled learning cycle for 8768: retrain -> shadow qualify -> controlled promotion.

    python -m src.signals_display.lifecycle_runner --journal J --state-dir S --commit SHA \
        --live-artifact A [--min-new N] [--port 8768] [--activator disabled]

Read-only against the live service and the journal. It writes only inside
--state-dir and the public status file <journal>.lifecycle-status.json (atomic,
private). Telegram messages are sent only for: training completed (with a
candidate or a rejection), a candidate qualified, a promotion, a rollback, and a
critical failure. Routine 15-minute updates are never announced.

The only activator shipped is DisabledActivator. Production switching needs a
verified guarded activator, deployed and approved separately. Until then the
controller blocks every promotion with ACTIVATION_DISABLED, and nothing changes.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from . import model_lifecycle as lc
from . import phone_alerts
from . import promotion as pr

STATUS_SUFFIX = ".lifecycle-status.json"
PROGRESS = "progress.json"


def _read_progress(state_dir):
    path = Path(state_dir) / PROGRESS
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
CADENCE_MINUTES = 30


def probe_live(port):
    """Read-only health sample of the live bot on 127.0.0.1."""
    opener = build_opener(ProxyHandler({}))

    def get(path):
        try:
            with opener.open(f"http://127.0.0.1:{port}{path}", timeout=8) as r:
                return r.status, json.loads(r.read() or b"{}")
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return None, {}
    hc, h = get("/healthz")
    rc, r = get("/readyz")
    return {"health": h.get("status") if hc in (200, 503) else None,
            "ready": r.get("status") if rc in (200, 503) else None,
            "signals_only": h.get("signals_only") is True, "workers_healthy": (h.get("workers") or {}).get("healthy"),
            "quote_age_s": None, "model_file_sha": h.get("model_file_sha")}


def probe_resources():
    mem_mib = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                mem_mib = int(line.split()[1]) // 1024
    except OSError:
        pass
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError):
        load1 = None
    st = os.statvfs(os.path.expanduser("~"))
    return {"mem_mib": mem_mib, "load1": load1, "disk_mib": st.f_bavail * st.f_frsize // 1048576}


def telegram_ready(config_loader=phone_alerts.load_config):
    cfg = config_loader()
    return "telegram" in phone_alerts.channels(cfg)


def messages(event, data):
    """Short, credential-free text. Checksums are shortened; nothing secret is ever included."""
    if event == "training_completed":
        return f"8768 learning: training cycle completed. Decision: {data.get('decision')}. " \
               f"Markets learned: {data.get('settled_markets')}. Live model unchanged unless promoted."
    if event == "candidate_qualified":
        return f"8768 learning: candidate {data.get('candidate_checksum')} qualified on unseen markets. " \
               "Promotion controller will decide next cycle."
    if event == "promoted":
        return f"8768 learning: model promoted to {data.get('candidate_checksum')} (previous {data.get('previous_checksum')}). Signals-only; no trades."
    if event == "rollback":
        return f"8768 learning: ROLLED BACK to previous model {data.get('previous_checksum')}. Reason: {data.get('reason')}."
    return f"8768 learning: CRITICAL failure: {data.get('reason') or data.get('error') or 'unknown'}. Live model unchanged."


def make_notifier(config_loader=phone_alerts.load_config, sender=phone_alerts.send):
    def notify(event, data):
        try:
            cfg = config_loader()
            text = messages(event, data)
            for channel in phone_alerts.channels(cfg):
                sender(channel, cfg, text)
            return True
        except Exception as exc:  # notifications must never stop learning; only the error type is kept
            return type(exc).__name__
    return notify


def _publish(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".lifecycle-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def run(journal, state_dir, commit, live_artifact, *, asof, min_new=lc.MIN_NEW_SETTLED, port=8768,
        activator=None, probe=probe_live, resources=probe_resources, notify=None, telegram=telegram_ready,
        seconds_to_opening=None):
    state_dir = Path(state_dir)
    activator = activator or pr.DisabledActivator()
    notify = notify or (lambda e, d: None)
    history = pr.History(state_dir)
    progress = _read_progress(state_dir)
    last = datetime.fromisoformat(progress["last_settled_cutoff"]) if progress.get("last_settled_cutoff") else None
    reports = list((state_dir / "reports").glob("report-*.json")) if (state_dir / "reports").exists() else []
    status = lc.retrain_cycle(journal, state_dir, asof, commit, last_cutoff=last, min_new=min_new,
                              candidates_evaluated=len(reports))
    if status.get("decision") != "SKIPPED_NOT_ENOUGH_NEW_DATA":
        _publish(state_dir / PROGRESS, {"last_settled_cutoff": asof.isoformat()})
        notify("training_completed", {"decision": status.get("decision"), "settled_markets": status.get("settled_markets")})
    if status.get("decision") == "CANDIDATE_QUALIFIED":
        notify("candidate_qualified", {"candidate_checksum": (status.get("candidate_checksum") or "")[:12]})
    samples = [probe(port)]
    if hasattr(activator, "bootstrap") and getattr(activator, "enabled", False):
        activator.bootstrap(live_artifact)          # record the current production model as the rollback reference
    promo = pr.promotion_cycle(state_dir, status, activator, live_artifact_path=live_artifact,
                               health_samples=samples, resources=resources(), telegram_ok=telegram(),
                               seconds_to_opening=seconds_to_opening, notify=notify, now=asof)
    entries = history.entries()
    active = pr._read_active(state_dir)
    rollback = next((e for e in reversed(entries) if e["event"] in ("ROLLED_BACK", "ACTIVATION_FAILED")), None)
    out = {
        "format": lc.FORMAT, "generated_at": asof.isoformat(), "commit": commit,
        "next_cycle_after": (asof.replace(microsecond=0)).isoformat(),
        "cadence_minutes": CADENCE_MINUTES,
        "current_production_model": (active or {}).get("candidate_checksum") or "original (no promotion yet)",
        "live_model": "UNCHANGED" if promo.get("result") != "PROMOTED" else "PROMOTED",
        "markets_learned_from": status.get("settled_markets"),
        "new_settled_markets": status.get("new_settled_markets"),
        "last_training_cycle": asof.isoformat(),
        "retrain_decision": status.get("decision"),
        "reasons": status.get("reasons", []),
        "candidate": {"checksum": (status.get("candidate_checksum") or "")[:12] or None,
                      "markets_scored": status.get("markets_scored")},
        "promotion": {"result": promo.get("result"), "detail": promo.get("detail")},
        "rollback": {"status": "ROLLED_BACK" if rollback and rollback["event"] == "ROLLED_BACK" else
                     ("ACTIVATION_FAILED" if rollback else "none"),
                     "last_event_at": rollback["at"] if rollback else None},
        "history_chain_verified": history.verify_chain(),
        "history_entries": len(entries),
        "activation": "ENABLED" if activator.enabled else "DISABLED (no verified guarded activator configured)",
        "errors": [e["data"].get("error") for e in entries[-5:] if e["event"] == "CONTROLLER_ERROR"],
        "live_probe": samples[0],
    }
    _publish(Path(f"{journal}{STATUS_SUFFIX}"), out)
    return out


def build_guarded(args):
    """Guarded activator from a restart configuration, or None (disabled).

    kind "rehearsal": the isolated stand-in restarter (never the live port).
    kind "production": the captured live launch (argv, cwd) and a private environment file. Accepted
    only for ports other than the retired 8765/8767, and only with the authorization file whose
    checksum covers this exact configuration. Otherwise the activator stays disabled."""
    from . import guarded_activator as ga
    from . import activation_rehearsal as rh
    from . import production_restart as prs
    if args.restart_config is None or not args.restart_config.is_file():
        return None
    cfg = json.loads(args.restart_config.read_text())
    port = int(cfg.get("port", 0))
    if port in (8765, 8767):
        return None
    if cfg.get("kind") == "rehearsal":
        if port == int(args.port):
            return None
        restarter = rh.ProcessRestarter(port, cfg["pidfile"], cfg["log"])
        config = {"restart": "rehearsal-process", "port": port}
    elif cfg.get("kind") == "production":
        captured = {"port": port, "argv": cfg["argv"], "cwd": cfg["cwd"]}
        restarter = prs.ProductionRestarter(captured, cfg["env_file"], cfg["log"])
        config = {"restart": "production-process", "port": port, "argv": cfg["argv"], "cwd": cfg["cwd"]}
    else:
        return None
    return ga.GuardedActivator(args.state_dir, authorization_file=args.state_dir / "promotion-authorization.txt",
                               config=config, restarter=restarter,
                               probe=lambda: probe_live(port))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--live-artifact", type=Path, required=True)
    parser.add_argument("--min-new", type=int, default=lc.MIN_NEW_SETTLED)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--activator", choices=["disabled", "guarded"], default="disabled")
    parser.add_argument("--restart-config", type=Path, help="JSON; only kind=rehearsal is accepted")
    args = parser.parse_args(argv)
    activator = None
    if args.activator == "guarded":
        activator = build_guarded(args)
    now = datetime.now(timezone.utc)
    since = (now.minute % 15) * 60 + now.second
    out = run(args.journal, args.state_dir, args.commit, args.live_artifact, asof=now, min_new=args.min_new,
              port=args.port, notify=make_notifier(), seconds_to_opening=900 - since, activator=activator)
    print(json.dumps({k: out[k] for k in ("retrain_decision", "promotion", "live_model", "activation")}, default=str))


if __name__ == "__main__":
    main()
