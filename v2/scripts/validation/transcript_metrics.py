#!/usr/bin/env python3
"""Where each run's time went and how parallel the agent's work was, from the transcripts.

analyze_runs.py looks at what the agent sent to the devices. This looks at how it worked:

Per run:
    wall / api      minutes of the session (run_agent.py's monotonic timer) and minutes spent
                    waiting on the API
    session_min     the same span on the wall clock (agent_started to agent_ended), which is
                    what transcript timestamps are measured against
    tool_wait       minutes between each tool call and its result (SSH sessions, sleeps,
                    convergence waits); tool calls never overlap since no message has two
    multi_tool      assistant messages that issued more than one tool call at once
    device_calls    Bash calls that talk to the devices (netmiko inline, or a script it wrote)
    threaded_calls  the ones whose code runs devices concurrently (ThreadPoolExecutor, threads)
    ping_bug_min    first time the agent tested pings with '"0% packet loss" in out', which
                    also matches "100% packet loss" and makes blocked traffic look reachable

Run from v2/:
    python scripts/validation/transcript_metrics.py                      # results/runs/opus
    python scripts/validation/transcript_metrics.py --json metrics.json
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from verify_lab import PROJECT_ROOT  # noqa: E402

from transcript import extract_tool_calls, load_jsonl, run_files  # noqa: E402

THREADED = re.compile(r"ThreadPoolExecutor|threading\.Thread|concurrent\.futures")
DEVICE_CALL = re.compile(r"ConnectHandler|python3?\s+(?:-\S+\s+)*\S+\.py|from \w+ import")
PING_BUG = re.compile(r"""['"]0% packet loss['"]\s+(?:not\s+)?in\b""")


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def tool_wait_seconds(transcript):
    """Seconds between every tool_use and its tool_result."""
    started, total = {}, 0.0
    for e in transcript:
        msg = e.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list) or e.get("isSidechain"):
            continue
        for b in msg["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                started[b["id"]] = parse_ts(e["timestamp"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in started:
                total += (parse_ts(e["timestamp"]) - started.pop(b["tool_use_id"])).total_seconds()
    return total


def tools_per_message(transcript):
    """Tool calls per assistant API message (one message can span several transcript entries)."""
    counts = {}
    for e in transcript:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        msg = e.get("message") or {}
        n = sum(1 for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use")
        if n:
            counts[msg.get("id")] = counts.get(msg.get("id"), 0) + n
    return list(counts.values())


def run_metrics(run_dir):
    files = run_files(run_dir)
    transcript = load_jsonl(files["transcript"])
    with open(files["meta"]) as f:
        meta = json.load(f)
    calls = extract_tool_calls(transcript)
    t0 = parse_ts(calls[0].ts)

    # A Bash call that runs a script the agent wrote counts with that script's content
    scripts, device_calls, threaded_calls, ping_bug = {}, 0, 0, None
    for c in calls:
        if c.name == "Write" and c.input.get("file_path"):
            scripts[os.path.basename(c.input["file_path"])] = c.input.get("content", "")
        if PING_BUG.search(c.device_text()) and ping_bug is None:
            ping_bug = round((parse_ts(c.ts) - t0).total_seconds() / 60, 1)
        if c.name != "Bash":
            continue
        text = c.input.get("command", "")
        text += "".join(content for name, content in scripts.items() if name and name in text)
        if DEVICE_CALL.search(text):
            device_calls += 1
            threaded_calls += bool(THREADED.search(text))

    per_msg = tools_per_message(transcript)
    usage = meta["result"]["usage"]
    return {
        "run": os.path.basename(run_dir),
        "wall_min": round(meta["wall_seconds"] / 60, 2),
        # Same span on the wall clock, which the transcript timestamps use. wall_seconds is from
        # time.monotonic() and runs a few minutes short in some runs.
        "session_min": round((parse_ts(meta["agent_ended"]) - parse_ts(meta["agent_started"])).total_seconds() / 60, 2),
        "api_min": round(meta["result"]["duration_api_ms"] / 60000, 2),
        "tool_wait_min": round(tool_wait_seconds(transcript) / 60, 2),
        "tool_calls": len(calls),
        "messages_with_tools": len(per_msg),
        "multi_tool_messages": sum(1 for n in per_msg if n > 1),
        "device_calls": device_calls,
        "threaded_calls": threaded_calls,
        "ping_bug_min": ping_bug,
        "output_tokens": usage.get("output_tokens", 0),
        "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--label", default="opus")
    ap.add_argument("--json", help="write per-run metrics to this file")
    args = ap.parse_args()

    run_dirs = sorted(glob.glob(os.path.join(PROJECT_ROOT, "results", "runs", args.label, "run_*")))
    rows = [run_metrics(d) for d in run_dirs]
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)

    print(f"{'run':8} {'wall':>5} {'api':>5} {'tools':>5} {'calls':>5} {'multi':>5} {'dev':>4} {'thr':>4} {'ping bug':>8}")
    for r in rows:
        bug = "-" if r["ping_bug_min"] is None else f"{r['ping_bug_min']:.1f}"
        print(f"{r['run']:8} {r['wall_min']:5.1f} {r['api_min']:5.1f} {r['tool_wait_min']:5.1f} {r['tool_calls']:5} "
              f"{r['multi_tool_messages']:5} {r['device_calls']:4} {r['threaded_calls']:4} {bug:>8}")


if __name__ == "__main__":
    main()
