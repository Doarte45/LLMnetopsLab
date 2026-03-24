#!/usr/bin/env python3
"""Parse Claude Code transcript JSONL files and compute process-level metrics.

Reads JSONL transcript files from a directory and computes:
- Time to completion
- Token consumption (input, output, cache)
- Tool/CLI usage counts and error rates
- Behavioral analysis (device access order, diagnostic strategy, command validity)

Usage:
    python scripts/parse_transcripts.py results/cctranscripts/ [--format json|table] [--output summary.json]
"""

import argparse
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median, stdev

# Ensure stdout can handle Unicode on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# Opus 4.6 pricing ($ per million tokens)
PRICING = {
    "input_tokens": 5.00,
    "cache_write_5m": 6.25,
    "cache_write_1h": 10.00,
    "cache_read": 0.50,
    "output_tokens": 25.00,
}

# Router IP mapping
ROUTER_IPS = {
    "192.168.122.5": "Branch1",
    "192.168.122.6": "HQ",
    "192.168.122.7": "Branch2",
}

# Keywords for classifying commands into phases
DIAGNOSE_KEYWORDS = re.compile(
    r"\b(show|display|get|cat|print|ping|traceroute|monitor|log|status|neighbor|route|interface|ip\s+route)\b",
    re.IGNORECASE,
)
FIX_KEYWORDS = re.compile(
    r"\b(set|delete|configure|commit|save|load|merge|copy|write)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    """Format seconds as Xm Ys."""
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


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


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_transcripts(directory: Path) -> dict[str, list[dict]]:
    """Load all JSONL transcript files from a directory.

    Returns dict mapping run name -> list of parsed JSON entries.
    Skips 'base.jsonl' (system prompt template, not a run).
    """
    transcripts: dict[str, list[dict]] = {}
    for path in sorted(directory.glob("*.jsonl")):
        if path.stem == "base":
            continue
        entries = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"WARNING: Could not parse {path.name}: {exc}, skipping", file=sys.stderr)
            continue
        if not entries:
            print(f"WARNING: {path.name} is empty, skipping", file=sys.stderr)
            continue
        transcripts[path.stem] = entries

    if not transcripts:
        print("ERROR: No valid transcript files found.", file=sys.stderr)
        sys.exit(1)
    return transcripts


# ---------------------------------------------------------------------------
# Per-run extraction
# ---------------------------------------------------------------------------

def extract_duration(entries: list[dict]) -> float | None:
    """Extract run duration in seconds.

    Prefers the system/turn_duration entry; falls back to timestamp delta.
    """
    for entry in entries:
        if entry.get("type") == "system" and entry.get("subtype") == "turn_duration":
            ms = entry.get("durationMs")
            if ms is not None:
                return ms / 1000.0

    # Fallback: first to last timestamp
    timestamps = []
    for entry in entries:
        ts = entry.get("timestamp", "")
        if ts:
            timestamps.append(ts)
    if len(timestamps) >= 2:
        from datetime import datetime, timezone

        try:
            fmt = "%Y-%m-%dT%H:%M:%S"
            t0 = datetime.strptime(timestamps[0][:19], fmt)
            t1 = datetime.strptime(timestamps[-1][:19], fmt)
            delta = (t1 - t0).total_seconds()
            if delta > 0:
                return delta
        except ValueError:
            pass
    return None


def extract_tokens(entries: list[dict]) -> dict:
    """Sum token usage across all assistant messages in a run."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        usage = entry.get("message", {}).get("usage", {})
        totals["input_tokens"] += usage.get("input_tokens", 0)
        totals["output_tokens"] += usage.get("output_tokens", 0)
        totals["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)
        totals["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0)
        # 5m / 1h cache write breakdown
        cache_creation = usage.get("cache_creation", {})
        totals["cache_write_5m_tokens"] += cache_creation.get("ephemeral_5m_input_tokens", 0)
        totals["cache_write_1h_tokens"] += cache_creation.get("ephemeral_1h_input_tokens", 0)

    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["cache_creation_input_tokens"]
        + totals["cache_read_input_tokens"]
    )
    return totals


def compute_cost(tokens: dict) -> dict:
    """Compute cost in USD from token counts using Opus 4.6 pricing."""
    input_cost = tokens["input_tokens"] * PRICING["input_tokens"] / 1_000_000
    cache_5m_cost = tokens["cache_write_5m_tokens"] * PRICING["cache_write_5m"] / 1_000_000
    cache_1h_cost = tokens["cache_write_1h_tokens"] * PRICING["cache_write_1h"] / 1_000_000
    cache_read_cost = tokens["cache_read_input_tokens"] * PRICING["cache_read"] / 1_000_000
    output_cost = tokens["output_tokens"] * PRICING["output_tokens"] / 1_000_000
    total = input_cost + cache_5m_cost + cache_1h_cost + cache_read_cost + output_cost
    return {
        "input": round(input_cost, 6),
        "cache_write_5m": round(cache_5m_cost, 6),
        "cache_write_1h": round(cache_1h_cost, 6),
        "cache_read": round(cache_read_cost, 6),
        "output": round(output_cost, 6),
        "total": round(total, 6),
    }


def extract_tool_usage(entries: list[dict]) -> dict:
    """Extract tool call counts and error rates."""
    tool_counts: Counter = Counter()
    total_tool_calls = 0
    error_count = 0
    total_results = 0

    for entry in entries:
        # Count tool_use blocks in assistant messages
        if entry.get("type") == "assistant":
            for block in entry.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    total_tool_calls += 1
                    tool_counts[block.get("name", "Unknown")] += 1

        # Count errors in tool results
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        total_results += 1
                        if block.get("is_error", False):
                            error_count += 1

    return {
        "total_tool_calls": total_tool_calls,
        "tool_breakdown": dict(tool_counts.most_common()),
        "error_count": error_count,
        "total_results": total_results,
        "error_rate": error_count / total_results if total_results else 0.0,
    }


def _extract_bash_commands(entries: list[dict]) -> list[str]:
    """Extract ordered list of Bash command strings from assistant messages."""
    commands = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        for block in entry.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                cmd = block.get("input", {}).get("command", "")
                if cmd:
                    commands.append(cmd)
    return commands


def _classify_command(cmd: str, is_after_first_fix: bool) -> str:
    """Classify a bash command as diagnose, fix, or verify.

    After the first fix command is seen, subsequent diagnostic-like commands
    (show, ping) are reclassified as 'verify'.
    """
    has_fix = bool(FIX_KEYWORDS.search(cmd))
    has_diag = bool(DIAGNOSE_KEYWORDS.search(cmd))

    if has_fix:
        return "fix"
    if has_diag:
        return "verify" if is_after_first_fix else "diagnose"
    # Default: if after first fix treat as verify, else diagnose
    return "verify" if is_after_first_fix else "diagnose"


def extract_behavior(entries: list[dict]) -> dict:
    """Extract behavioral metrics: device access order, strategy phases, retries."""
    commands = _extract_bash_commands(entries)

    # --- Device access sequence ---
    access_order = []
    seen_ips = set()
    for cmd in commands:
        for ip, name in ROUTER_IPS.items():
            if ip in cmd and name not in seen_ips:
                access_order.append(name)
                seen_ips.add(name)

    # --- Phase classification ---
    phases = []
    first_fix_seen = False
    for cmd in commands:
        phase = _classify_command(cmd, first_fix_seen)
        if phase == "fix":
            first_fix_seen = True
        phases.append(phase)

    phase_counts = Counter(phases)
    total = len(phases) if phases else 1  # avoid div by zero

    # --- Retry detection ---
    # A retry is when the same command (normalized) appears after an error
    error_tool_ids = set()
    for entry in entries:
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result" and block.get("is_error", False):
                        error_tool_ids.add(block.get("tool_use_id", ""))

    # Map tool_use_id to command text
    tool_id_to_cmd: dict[str, str] = {}
    for entry in entries:
        if entry.get("type") == "assistant":
            for block in entry.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_id_to_cmd[block.get("id", "")] = block.get("input", {}).get("command", "")

    # Count commands that were retried after failure
    failed_cmds = [tool_id_to_cmd.get(tid, "") for tid in error_tool_ids if tid in tool_id_to_cmd]
    all_cmds_set = set(commands)
    retries = sum(1 for fc in failed_cmds if fc and commands.count(fc) > 1)

    return {
        "device_access_order": access_order,
        "total_bash_commands": len(commands),
        "phase_counts": {
            "diagnose": phase_counts.get("diagnose", 0),
            "fix": phase_counts.get("fix", 0),
            "verify": phase_counts.get("verify", 0),
        },
        "phase_pcts": {
            "diagnose": round(phase_counts.get("diagnose", 0) / total * 100, 1),
            "fix": round(phase_counts.get("fix", 0) / total * 100, 1),
            "verify": round(phase_counts.get("verify", 0) / total * 100, 1),
        },
        "retries": retries,
        "api_errors": sum(
            1 for e in entries
            if e.get("type") == "system" and e.get("subtype") == "api_error"
        ),
    }


def compute_run_metrics(name: str, entries: list[dict]) -> dict:
    """Compute all metrics for a single run."""
    tokens = extract_tokens(entries)
    return {
        "name": name,
        "duration": extract_duration(entries),
        "tokens": tokens,
        "cost": compute_cost(tokens),
        "tool_usage": extract_tool_usage(entries),
        "behavior": extract_behavior(entries),
    }


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def compute_aggregate(per_run: list[dict]) -> dict:
    """Compute aggregate statistics across all runs."""
    n = len(per_run)

    # --- Duration ---
    durations = [r["duration"] for r in per_run if r["duration"] is not None]

    # --- Tokens ---
    token_keys = [
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens",
        "cache_read_input_tokens", "total_tokens",
    ]
    token_stats = {}
    for key in token_keys:
        vals = [r["tokens"][key] for r in per_run]
        token_stats[key] = stat_block([float(v) for v in vals])

    # --- Cost ---
    cost_keys = ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output", "total"]
    cost_stats = {}
    for key in cost_keys:
        vals = [r["cost"][key] for r in per_run]
        cost_stats[key] = stat_block(vals)
    total_cost_all_runs = sum(r["cost"]["total"] for r in per_run)

    # --- Tool usage ---
    tool_call_counts = [r["tool_usage"]["total_tool_calls"] for r in per_run]
    error_rates = [r["tool_usage"]["error_rate"] for r in per_run]

    # Aggregate tool breakdown
    all_tools: Counter = Counter()
    for r in per_run:
        for tool, count in r["tool_usage"]["tool_breakdown"].items():
            all_tools[tool] += count

    # --- Behavior ---
    bash_counts = [r["behavior"]["total_bash_commands"] for r in per_run]

    # Device access order frequency
    access_patterns: Counter = Counter()
    for r in per_run:
        pattern = " → ".join(r["behavior"]["device_access_order"]) or "(none)"
        access_patterns[pattern] += 1

    # Phase breakdowns
    phase_pcts = {
        "diagnose": [r["behavior"]["phase_pcts"]["diagnose"] for r in per_run],
        "fix": [r["behavior"]["phase_pcts"]["fix"] for r in per_run],
        "verify": [r["behavior"]["phase_pcts"]["verify"] for r in per_run],
    }

    retry_counts = [r["behavior"]["retries"] for r in per_run]
    api_error_counts = [r["behavior"]["api_errors"] for r in per_run]

    return {
        "total_runs": n,
        "duration": {
            "stats": stat_block(durations),
            "available": len(durations),
        },
        "tokens": token_stats,
        "cost": {
            "per_run": cost_stats,
            "total_all_runs": round(total_cost_all_runs, 4),
        },
        "tool_usage": {
            "total_calls": stat_block([float(v) for v in tool_call_counts]),
            "error_rate": stat_block(error_rates),
            "tool_breakdown": dict(all_tools.most_common()),
        },
        "behavior": {
            "bash_commands": stat_block([float(v) for v in bash_counts]),
            "access_patterns": dict(access_patterns.most_common()),
            "phase_pcts": {k: stat_block(v) for k, v in phase_pcts.items()},
            "retries": stat_block([float(v) for v in retry_counts]),
            "api_errors": sum(api_error_counts),
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
    n = agg["total_runs"]

    lines.append(f"{'=' * 90}")
    lines.append(f"  Claude Code Transcript Analysis - {n} runs")
    lines.append(f"{'=' * 90}")

    # --- Per-run summary ---
    lines.append("")
    lines.append("PER-RUN SUMMARY")
    lines.append(f"{'-' * 100}")
    header = (
        f"{'Run':<10} {'Duration':>8} {'OutTok':>7} {'InTok':>7}"
        f" {'CacheCr':>7} {'CacheRd':>8} {'Cost':>7} {'Tools':>5} {'Errs':>4}"
        f"  {'Access Order':<25} {'D/F/V'}"
    )
    lines.append(header)
    lines.append(f"{'-' * 100}")
    for r in per_run:
        dur = fmt_duration(r["duration"]) if r["duration"] is not None else "N/A"
        tok = r["tokens"]
        cost = r["cost"]["total"]
        tu = r["tool_usage"]
        beh = r["behavior"]
        access = "→".join(beh["device_access_order"]) or "-"
        pc = beh["phase_counts"]
        dfv = f"{pc['diagnose']}/{pc['fix']}/{pc['verify']}"
        lines.append(
            f"{r['name']:<10} {dur:>7} {tok['output_tokens']:>7,} {tok['input_tokens']:>7,}"
            f" {tok['cache_creation_input_tokens']:>7,} {tok['cache_read_input_tokens']:>8,}"
            f" ${cost:>6.3f} {tu['total_tool_calls']:>5} {tu['error_count']:>4}"
            f"  {access:<25} {dfv}"
        )

    # --- Duration stats ---
    lines.append("")
    lines.append("DURATION (seconds)")
    lines.append(f"{'-' * 90}")
    ds = agg["duration"]["stats"]
    if ds["mean"] is not None:
        lines.append(
            f"  Mean: {fmt_duration(ds['mean'])}  Median: {fmt_duration(ds['median'])}"
            f"  Stdev: {fmt_duration(ds['stdev'])}  Min: {fmt_duration(ds['min'])}  Max: {fmt_duration(ds['max'])}"
            f"  (n={agg['duration']['available']})"
        )
    else:
        lines.append("  No duration data available")

    # --- Token stats ---
    lines.append("")
    lines.append("TOKEN CONSUMPTION")
    lines.append(f"{'-' * 90}")
    for key in ["input_tokens", "output_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens", "total_tokens"]:
        ts = agg["tokens"][key]
        label = key.replace("_", " ").title()
        if ts["mean"] is not None:
            sd_part = f"  stdev={ts['stdev']:>10,.1f}" if ts["stdev"] is not None else ""
            lines.append(
                f"  {label:<30} mean={ts['mean']:>10,.1f}"
                f"  median={ts['median']:>10,.1f}"
                f"{sd_part}"
                f"  min={ts['min']:>8,.0f}  max={ts['max']:>10,.0f}"
            )

    # --- Cost ---
    lines.append("")
    lines.append("COST (USD, Opus 4.6 pricing)")
    lines.append(f"{'-' * 100}")
    cost_labels = [
        ("input", "Base Input"),
        ("cache_write_5m", "Cache Write (5m)"),
        ("cache_write_1h", "Cache Write (1h)"),
        ("cache_read", "Cache Read"),
        ("output", "Output"),
        ("total", "Total"),
    ]
    for key, label in cost_labels:
        cs = agg["cost"]["per_run"][key]
        if cs["mean"] is not None:
            sd_part = f"  stdev=${cs['stdev']:.4f}" if cs["stdev"] is not None else ""
            lines.append(
                f"  {label:<20} mean=${cs['mean']:.4f}"
                f"  median=${cs['median']:.4f}"
                f"{sd_part}"
                f"  min=${cs['min']:.4f}  max=${cs['max']:.4f}"
            )
    lines.append(f"  {'─' * 60}")
    lines.append(f"  Total across all {n} runs: ${agg['cost']['total_all_runs']:.4f}")

    # --- Tool usage ---
    lines.append("")
    lines.append("TOOL USAGE")
    lines.append(f"{'-' * 90}")
    tc = agg["tool_usage"]["total_calls"]
    if tc["mean"] is not None:
        lines.append(
            f"  Calls per run:  mean={tc['mean']:.1f}  median={tc['median']:.1f}"
            f"  min={tc['min']:.0f}  max={tc['max']:.0f}"
        )
    er = agg["tool_usage"]["error_rate"]
    if er["mean"] is not None:
        lines.append(
            f"  Error rate:     mean={er['mean']:.1%}  median={er['median']:.1%}"
            f"  min={er['min']:.1%}  max={er['max']:.1%}"
        )
    lines.append("  Tool breakdown (total across all runs):")
    for tool, count in agg["tool_usage"]["tool_breakdown"].items():
        lines.append(f"    {tool:<15} {count:>4}")

    # --- Behavioral analysis ---
    lines.append("")
    lines.append("BEHAVIORAL ANALYSIS")
    lines.append(f"{'-' * 90}")

    # Access patterns
    lines.append("  Device access order:")
    for pattern, count in agg["behavior"]["access_patterns"].items():
        bar = "#" * count
        lines.append(f"    {pattern:<35} {bar} ({count}/{n})")

    # Phase breakdown
    lines.append("")
    lines.append("  Strategy phase breakdown (% of Bash commands):")
    for phase in ["diagnose", "fix", "verify"]:
        ps = agg["behavior"]["phase_pcts"][phase]
        if ps["mean"] is not None:
            lines.append(
                f"    {phase.capitalize():<10} mean={ps['mean']:>5.1f}%"
                f"  median={ps['median']:>5.1f}%"
                f"  min={ps['min']:>5.1f}%  max={ps['max']:>5.1f}%"
            )

    # Retries
    rs = agg["behavior"]["retries"]
    if rs["mean"] is not None:
        lines.append(
            f"\n  Retries (re-run after error): mean={rs['mean']:.1f}"
            f"  max={rs['max']:.0f}"
        )

    # API errors
    total_api_errors = agg["behavior"]["api_errors"]
    lines.append(f"  API errors (total across all runs): {total_api_errors}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parse Claude Code transcript JSONL files and compute process-level metrics."
    )
    parser.add_argument(
        "transcripts_dir", type=Path,
        help="Directory containing JSONL transcript files",
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

    if not args.transcripts_dir.is_dir():
        print(f"ERROR: {args.transcripts_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    transcripts = load_transcripts(args.transcripts_dir)
    per_run = [compute_run_metrics(name, entries) for name, entries in transcripts.items()]
    aggregate = compute_aggregate(per_run)

    if args.format == "json":
        output = format_json(aggregate, per_run)
    else:
        output = format_table(aggregate, per_run)

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Output written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
