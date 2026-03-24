#!/usr/bin/env python3
"""Parse verify_configs.py JSON output and compute statistics across runs.

Reads result files from a directory and computes fix-rate metrics,
per-error detection rates, per-router health, and config divergence.

Usage:
    python scripts/parse_results.py results/verify/ [--format json|table] [--output summary.json]
"""

import argparse
import io
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median, stdev

# Ensure stdout can handle Unicode on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# The 6 known error IDs (must match verify_configs.py KNOWN_ERRORS)
ALL_ERROR_IDS = {1, 2, 3, 4, 5, 6}

ALL_ROUTERS = ["HQ", "Branch1", "Branch2"]

REQUIRED_KEYS = {"timestamp", "errors", "routers", "score"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_runs(directory: Path) -> list[dict]:
    """Load all JSON verify result files from a directory."""
    runs = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARNING: Could not parse {path.name}, skipping", file=sys.stderr)
            continue
        missing = REQUIRED_KEYS - set(data.keys())
        if missing:
            print(f"WARNING: {path.name} missing keys {missing}, skipping", file=sys.stderr)
            continue
        data["_filename"] = path.name
        runs.append(data)
    if not runs:
        print("ERROR: No valid result files found.", file=sys.stderr)
        sys.exit(1)
    return runs


# ---------------------------------------------------------------------------
# Per-run metrics
# ---------------------------------------------------------------------------

def compute_error_statuses(run: dict) -> dict:
    """Extract per-error status and overall score.

    Returns dict with fixed/fixed_alt/broken/unknown ID lists and score info.
    FIXED_ALT counts as fixed (error resolved via acceptable alternate command).
    """
    fixed = []
    fixed_alt = []
    broken = []
    unknown = []
    for err in run["errors"]:
        eid = err["id"]
        status = err["status"]
        if status == "FIXED":
            fixed.append(eid)
        elif status == "FIXED_ALT":
            fixed_alt.append(eid)
        elif status == "BROKEN":
            broken.append(eid)
        else:
            unknown.append(eid)

    return {
        "fixed": sorted(fixed),
        "fixed_alt": sorted(fixed_alt),
        "broken": sorted(broken),
        "unknown": sorted(unknown),
        "fixed_count": run["score"]["fixed"],
        "total": run["score"]["total"],
        "percentage": run["score"]["percentage"],
    }


def compute_router_health(run: dict) -> dict[str, dict]:
    """Per-router connection status, config match, and command divergence."""
    result = {}
    for name in ALL_ROUTERS:
        router = run["routers"].get(name)
        if router is None:
            result[name] = {
                "connection_ok": False,
                "config_matches": False,
                "extra_count": 0,
                "missing_count": 0,
                "extra_commands": [],
                "missing_commands": [],
            }
            continue

        connection_ok = router.get("connection") == "ok"
        result[name] = {
            "connection_ok": connection_ok,
            "config_matches": router.get("config_matches_answer_key", False),
            "extra_count": len(router.get("extra_commands", [])),
            "missing_count": len(router.get("missing_commands", [])),
            "extra_commands": router.get("extra_commands", []),
            "missing_commands": router.get("missing_commands", []),
        }
    return result


def compute_run_metrics(run: dict) -> dict:
    """Compute all metrics for a single run."""
    return {
        "filename": run["_filename"],
        "timestamp": run["timestamp"],
        "error_statuses": compute_error_statuses(run),
        "router_health": compute_router_health(run),
    }


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def safe_stdev(values: list[float]) -> float | None:
    """Standard deviation, or None if fewer than 2 values."""
    return stdev(values) if len(values) >= 2 else None


def stat_block(vals: list[float]) -> dict:
    """Compute mean, median, stdev, min, max for a list of values."""
    if not vals:
        return {"mean": None, "median": None, "stdev": None, "min": None, "max": None}
    return {
        "mean": round(mean(vals), 3),
        "median": round(median(vals), 3),
        "stdev": round(sd, 3) if (sd := safe_stdev(vals)) is not None else None,
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
    }


def compute_aggregate_metrics(per_run: list[dict], runs: list[dict]) -> dict:
    """Compute aggregate statistics across all runs."""
    n = len(per_run)

    # --- Score distribution ---
    pct_vals = [r["error_statuses"]["percentage"] for r in per_run]
    score_counts: dict[int, int] = Counter(
        r["error_statuses"]["fixed_count"] for r in per_run
    )
    perfect = sum(1 for r in per_run if r["error_statuses"]["percentage"] == 100)

    # --- Per-error fix rate ---
    per_error: dict[int, dict] = {}
    # Get descriptions from first run
    desc_map = {e["id"]: e["description"] for e in runs[0]["errors"]}
    for eid in sorted(ALL_ERROR_IDS):
        fixed = sum(
            1 for r in per_run if eid in r["error_statuses"]["fixed"]
        )
        fixed_alt = sum(
            1 for r in per_run if eid in r["error_statuses"]["fixed_alt"]
        )
        broken = sum(
            1 for r in per_run if eid in r["error_statuses"]["broken"]
        )
        unknown = n - fixed - fixed_alt - broken
        total_fixed = fixed + fixed_alt
        per_error[eid] = {
            "description": desc_map.get(eid, ""),
            "fixed": fixed,
            "fixed_alt": fixed_alt,
            "broken": broken,
            "unknown": unknown,
            "fix_rate": total_fixed / n if n else 0.0,
        }

    # --- Per-router stats ---
    per_router: dict[str, dict] = {}
    for name in ALL_ROUTERS:
        conn_ok = sum(1 for r in per_run if r["router_health"][name]["connection_ok"])
        matches = sum(1 for r in per_run if r["router_health"][name]["config_matches"])
        extra_counts = [r["router_health"][name]["extra_count"] for r in per_run]
        missing_counts = [r["router_health"][name]["missing_count"] for r in per_run]
        per_router[name] = {
            "connection_success_rate": conn_ok / n if n else 0.0,
            "config_match_rate": matches / n if n else 0.0,
            "extra_commands": stat_block(extra_counts),
            "missing_commands": stat_block(missing_counts),
        }

    # --- Config divergence ---
    extra_freq: Counter = Counter()
    missing_freq: Counter = Counter()
    extra_by_router: dict[str, Counter] = {name: Counter() for name in ALL_ROUTERS}
    missing_by_router: dict[str, Counter] = {name: Counter() for name in ALL_ROUTERS}

    for r in per_run:
        for name in ALL_ROUTERS:
            rh = r["router_health"][name]
            for cmd in rh["extra_commands"]:
                extra_freq[cmd] += 1
                extra_by_router[name][cmd] += 1
            for cmd in rh["missing_commands"]:
                missing_freq[cmd] += 1
                missing_by_router[name][cmd] += 1

    divergence_extra = []
    for cmd, count in extra_freq.most_common():
        routers = [name for name in ALL_ROUTERS if cmd in extra_by_router[name]]
        divergence_extra.append({
            "command": cmd,
            "count": count,
            "routers": routers,
        })

    divergence_missing = []
    for cmd, count in missing_freq.most_common():
        routers = [name for name in ALL_ROUTERS if cmd in missing_by_router[name]]
        divergence_missing.append({
            "command": cmd,
            "count": count,
            "routers": routers,
        })

    return {
        "total_runs": n,
        "score_distribution": {
            "stats": stat_block(pct_vals),
            "histogram": {str(k): v for k, v in sorted(score_counts.items(), reverse=True)},
            "perfect_runs": perfect,
            "imperfect_runs": n - perfect,
        },
        "per_error_fix_rate": per_error,
        "per_router_stats": per_router,
        "config_divergence": {
            "extra_commands": divergence_extra,
            "missing_commands": divergence_missing,
        },
        "overall": {
            "total_runs": n,
            "total_errors_checked": n * len(ALL_ERROR_IDS),
            "total_errors_fixed": sum(r["error_statuses"]["fixed_count"] for r in per_run),
            "overall_fix_rate": (
                sum(r["error_statuses"]["fixed_count"] for r in per_run)
                / (n * len(ALL_ERROR_IDS))
                if n else 0.0
            ),
            "perfect_run_rate": perfect / n if n else 0.0,
        },
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_json(aggregate: dict, per_run: list[dict]) -> str:
    """Machine-readable JSON output."""
    return json.dumps({"aggregate": aggregate, "per_run": per_run}, indent=2)


def format_table(aggregate: dict, per_run: list[dict]) -> str:
    """Human-readable table output."""
    lines = []
    agg = aggregate

    lines.append(f"{'=' * 78}")
    lines.append(f"  OSPF Lab Verification Report - {agg['total_runs']} runs")
    lines.append(f"{'=' * 78}")

    # --- Per-run summary ---
    lines.append("")
    lines.append("PER-RUN SUMMARY")
    lines.append(f"{'-' * 78}")
    header = (
        f"{'File':<18} {'Score':>5} {'%':>4}  "
        f"{'HQ':>4} {'Br1':>4} {'Br2':>4}  "
        f"{'Extra':>5} {'Miss':>5} {'Note':<4}"
    )
    lines.append(header)
    lines.append(f"{'-' * 78}")
    for r in per_run:
        es = r["error_statuses"]
        rh = r["router_health"]
        total_extra = sum(rh[name]["extra_count"] for name in ALL_ROUTERS)
        total_missing = sum(rh[name]["missing_count"] for name in ALL_ROUTERS)
        has_alt = len(es["fixed_alt"]) > 0
        note = "*" if has_alt else ""

        def router_status(name: str) -> str:
            if not rh[name]["connection_ok"]:
                return "FAIL"
            return "ok" if rh[name]["config_matches"] else "diff"

        lines.append(
            f"{r['filename']:<18} {es['fixed_count']:>3}/{es['total']}"
            f" {es['percentage']:>3}%"
            f"  {router_status('HQ'):>4}"
            f" {router_status('Branch1'):>4}"
            f" {router_status('Branch2'):>4}"
            f"  {total_extra:>5} {total_missing:>5} {note:<4}"
        )
    lines.append(f"  (* = used alternate command variant)")

    # --- Score distribution ---
    lines.append("")
    lines.append("SCORE DISTRIBUTION")
    lines.append(f"{'-' * 78}")
    hist = agg["score_distribution"]["histogram"]
    for score_str in sorted(hist.keys(), key=lambda x: int(x), reverse=True):
        count = hist[score_str]
        bar = "#" * count
        lines.append(f"  {score_str:>2}/6: {bar} ({count}/{agg['total_runs']})")
    lines.append(
        f"  Perfect runs: {agg['score_distribution']['perfect_runs']}/{agg['total_runs']}"
        f" ({agg['score_distribution']['perfect_runs'] / agg['total_runs']:.0%})"
    )

    # --- Per-error fix rate ---
    lines.append("")
    lines.append("PER-ERROR FIX RATE")
    lines.append(f"{'-' * 78}")
    n = agg["total_runs"]
    for eid, info in agg["per_error_fix_rate"].items():
        rate = info["fix_rate"]
        total_fixed = info["fixed"] + info["fixed_alt"]
        filled = int(rate * 20)
        bar = "#" * filled + "." * (20 - filled)
        breakdown = f"{total_fixed}/{n}"
        if info["fixed_alt"] > 0:
            breakdown += f" ({info['fixed']} exact, {info['fixed_alt']} alt)"
        lines.append(
            f"  Error #{eid}: {bar} {rate:>4.0%}"
            f" ({breakdown})"
            f"  {info['description']}"
        )

    # --- Per-router stats ---
    lines.append("")
    lines.append("PER-ROUTER STATS")
    lines.append(f"{'-' * 78}")
    for name in ALL_ROUTERS:
        rs = agg["per_router_stats"][name]
        lines.append(f"  {name}:")
        lines.append(f"    Connection success: {rs['connection_success_rate']:.0%}")
        lines.append(f"    Config match rate:  {rs['config_match_rate']:.0%}")
        extra = rs["extra_commands"]
        missing = rs["missing_commands"]
        if extra["mean"] is not None:
            lines.append(
                f"    Extra commands:     mean={extra['mean']:.1f}"
                f"  max={extra['max']:.0f}"
            )
        if missing["mean"] is not None:
            lines.append(
                f"    Missing commands:   mean={missing['mean']:.1f}"
                f"  max={missing['max']:.0f}"
            )

    # --- Config divergence ---
    extra_cmds = agg["config_divergence"]["extra_commands"]
    missing_cmds = agg["config_divergence"]["missing_commands"]
    if extra_cmds or missing_cmds:
        lines.append("")
        lines.append("CONFIG DIVERGENCE")
        lines.append(f"{'-' * 78}")
        if extra_cmds:
            lines.append("  Extra commands (present but not in answer key):")
            for entry in extra_cmds:
                routers = ", ".join(entry["routers"])
                lines.append(
                    f"    [{entry['count']:>3}/{n}] {entry['command']}"
                    f"  ({routers})"
                )
        if missing_cmds:
            lines.append("  Missing commands (in answer key but absent):")
            for entry in missing_cmds:
                routers = ", ".join(entry["routers"])
                lines.append(
                    f"    [{entry['count']:>3}/{n}] {entry['command']}"
                    f"  ({routers})"
                )

    # --- Overall summary ---
    ov = agg["overall"]
    lines.append("")
    lines.append("OVERALL SUMMARY")
    lines.append(f"{'-' * 78}")
    lines.append(f"  Total runs:          {ov['total_runs']}")
    lines.append(f"  Total errors checked: {ov['total_errors_checked']}")
    lines.append(f"  Total errors fixed:   {ov['total_errors_fixed']}")
    lines.append(f"  Overall fix rate:     {ov['overall_fix_rate']:.1%}")
    lines.append(f"  Perfect run rate:     {ov['perfect_run_rate']:.1%}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parse OSPF lab verify results and compute statistics."
    )
    parser.add_argument(
        "results_dir", type=Path,
        help="Directory containing verify JSON result files",
    )
    parser.add_argument(
        "--format", choices=["json", "table"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write output to file instead of stdout",
    )

    args = parser.parse_args()

    if not args.results_dir.is_dir():
        print(f"ERROR: {args.results_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    runs = load_runs(args.results_dir)

    per_run_metrics = [compute_run_metrics(run) for run in runs]
    aggregate = compute_aggregate_metrics(per_run_metrics, runs)

    if args.format == "json":
        output = format_json(aggregate, per_run_metrics)
    else:
        output = format_table(aggregate, per_run_metrics)

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Output written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
