"""Descriptive chronological evaluation metrics, never a profitable-edge claim."""
import math

from .live_model import wilson


def metrics(db):
    simulations = db.execute(
        """SELECT mode,model_version,status,COUNT(*) n,SUM(net_pnl) pnl,
        SUM(net_pnl*net_pnl) sq FROM simulations GROUP BY mode,model_version,status"""
    ).fetchall()
    groups = {}
    for row in simulations:
        key = row["mode"] + " / " + row["model_version"]
        group = groups.setdefault(
            key,
            {
                "observed_outcomes": 0,
                "target_hits": 0,
                "stops": 0,
                "timeouts": 0,
                "unobserved": 0,
                "no_entries": 0,
                "pending": 0,
                "simulated_net_pnl": 0.0,
                "pnl_squared": 0.0,
            },
        )
        status = row["status"]
        if status in ("SIMULATED_TARGET", "SIMULATED_STOP", "SIMULATED_TIMEOUT"):
            group["observed_outcomes"] += row["n"]
            group[
                {
                    "SIMULATED_TARGET": "target_hits",
                    "SIMULATED_STOP": "stops",
                    "SIMULATED_TIMEOUT": "timeouts",
                }[status]
            ] += row["n"]
            group["simulated_net_pnl"] += row["pnl"] or 0
            group["pnl_squared"] += row["sq"] or 0
        elif status.startswith("UNOBSERVED"):
            group["unobserved"] += row["n"]
        elif status.startswith("SIMULATED_NO_ENTRY"):
            group["no_entries"] += row["n"]
        else:
            group["pending"] += row["n"]
    for group in groups.values():
        n = group["observed_outcomes"]
        group["target_rate_observed_only"] = group["target_hits"] / n if n else None
        group["target_rate_interval"] = wilson(group["target_hits"], n) if n else None
        group["mean_simulated_net_pnl"] = group["simulated_net_pnl"] / n if n else None
        sq = group.pop("pnl_squared")
        mean = group["mean_simulated_net_pnl"]
        half = (
            1.96 * math.sqrt(max(0, (sq - n * mean * mean) / (n - 1)) / n) if n and n > 1 else None
        )
        group["mean_pnl_approximate_interval"] = (
            [mean - half, mean + half] if half is not None else None
        )
    coverage = {}
    rows = db.execute(
        """SELECT mode,COALESCE(json_extract(payload,'$.model.version'),'uploaded-scenarios') version,
        COUNT(*) n,SUM(scalp!='WAIT') called,SUM(direction!='UNCLEAR') directional
        FROM predictions WHERE id IN (SELECT MIN(id) FROM predictions
            WHERE json_extract(payload,'$.stale')=0
            GROUP BY mode,ticker,strftime('%Y-%m-%dT%H:%M',timestamp))
        GROUP BY mode,version"""
    ).fetchall()
    for row in rows:
        coverage[row["mode"] + " / " + row["version"]] = {
            "opportunities": row["n"],
            "scalp_called": row["called"],
            "scalp_wait": row["n"] - row["called"],
            "scalp_call_fraction": row["called"] / row["n"],
            "directional_fraction": row["directional"] / row["n"],
        }
    return {
        "scalp_by_model_and_mode": groups,
        "coverage_by_model_and_mode": coverage,
        "sampling_unit": "First fresh prediction per contract/minute for call coverage. Settlement uses one supported direction per contract.",
        "uncertainty_note": "Approximate sampling intervals only; correlated observations and informative missing-data censoring can invalidate nominal coverage. Observed-only target rate excludes unknown paths and is not a validated live probability.",
        "edge_status": "edge unverified",
        "profitable_edge_demonstrated": False,
    }
