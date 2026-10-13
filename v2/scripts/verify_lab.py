#!/usr/bin/env python3
"""Three-layer verifier for the v2 lab.

Layer 1  host reachability: ping matrix and internet pings, run from the hosts
Layer 2  control-plane state: OSPF adjacencies, routing tables, BGP
Layer 3  config intent: live configs diffed against v2/configs, persistence, FRR sync

Run from inside v2/:
    python scripts/verify_lab.py                       # all layers, writes results/verify/run_NNN.json
    python scripts/verify_lab.py --layers 3 --no-file  # config only, stdout only
    python scripts/verify_lab.py --compare results/verify/run_000.json   # diff against a baseline run
    python scripts/verify_lab.py --from-raw results/verify/run_004.raw.json  # re-grade saved output offline

Each run writes run_NNN.json (checks and diffs) and run_NNN.raw.json (every command's output),
so a run can be re-graded after the verifier changes.
"""

import argparse
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verifier import layer1, layer2, layer3  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIGS = os.path.join(PROJECT_ROOT, "configs")
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "results", "verify")
STATUSES = ("pass", "fail", "flaky", "error")
WIDTH = 78


def next_run_number(output_dir):
    numbers = [int(m.group(1)) for f in os.listdir(output_dir) if (m := re.match(r"run_(\d{3})\.json$", f))]
    return max(numbers, default=0) + 1


def collect(layers, args):
    """Collect raw output. Layer 2 goes first so its convergence gate runs before anything else."""
    raw = {}
    if 2 in layers:
        print(f"Layer 2: collecting control-plane state (polls every {args.l2_interval}s until stable, "
              f"up to {args.l2_timeout}s)...", flush=True)
        raw["layer2"] = layer2.collect(interval=args.l2_interval, timeout=args.l2_timeout)
    if 1 in layers:
        print("Layer 1: running the ping matrix from the hosts...", flush=True)
        raw["layer1"] = layer1.collect(retry=not args.no_retry)
    if 3 in layers:
        print("Layer 3: reading device configs...", flush=True)
        raw["layer3"] = layer3.collect()
    return raw


def analyze(raw, configs_dir):
    results = {"checks": []}
    if "layer1" in raw:
        checks, matrix = layer1.analyze(raw["layer1"])
        results["checks"] += checks
        results["layer1"] = {"matrix": matrix}
    if "layer2" in raw:
        results["checks"] += layer2.analyze(raw["layer2"])
        results["layer2"] = {"converged": {n: d.get("converged") for n, d in raw["layer2"].items()}}
    if "layer3" in raw:
        checks, details = layer3.analyze(raw["layer3"], layer3.load_intent(configs_dir))
        results["checks"] += checks
        results["layer3"] = details
    results["connections"] = {
        name: data.get("error", "ok")
        for layer in raw.values() for name, data in layer.items()
        if isinstance(data, dict) and "error" in data
    }
    results["summary"] = summarize(results["checks"])
    return results


def summarize(checks):
    summary = {}
    for layer in (1, 2, 3):
        layer_checks = [c for c in checks if c["layer"] == layer]
        if not layer_checks:
            continue
        counts = Counter(c["status"] for c in layer_checks)
        by_category = {}
        for c in layer_checks:
            by_category.setdefault(c["category"], Counter())[c["status"]] += 1
        summary[f"L{layer}"] = {
            "total": len(layer_checks),
            **{s: counts.get(s, 0) for s in STATUSES},
            "categories": {k: dict(v) for k, v in by_category.items()},
        }
    return summary


def compare(results, baseline):
    """Changes relative to a baseline run (normally the broken lab before the agent ran)."""
    before = {c["id"]: c["status"] for c in baseline.get("checks", [])}
    now = {c["id"]: c["status"] for c in results["checks"]}
    common = set(before) & set(now)

    def l3_items(res):
        items = set()
        for device, detail in res.get("layer3", {}).items():
            for d in detail.get("diffs") or []:
                items.add(f"{device} {d['kind']} {d['path']}")
        return items

    l3_before, l3_now = l3_items(baseline), l3_items(results)
    return {
        "baseline_timestamp": baseline.get("timestamp"),
        # passed before the run, not now
        "collateral": sorted(i for i in common if before[i] == "pass" and now[i] != "pass"),
        # failed before the run, pass now
        "resolved": sorted(i for i in common if before[i] != "pass" and now[i] == "pass"),
        # config deviations the run introduced (workarounds, leftovers, collateral) and removed
        "config_introduced": sorted(l3_now - l3_before),
        "config_removed": sorted(l3_before - l3_now),
    }


def print_report(results):
    print("=" * WIDTH)
    print("  v2 LAB VERIFICATION")
    print("=" * WIDTH)
    names = {"L1": "Host reachability", "L2": "Control-plane state", "L3": "Config intent"}
    for key, s in results["summary"].items():
        extra = "".join(f", {s[k]} {k}" for k in ("flaky", "error") if s[k])
        print(f"\n  {key} {names[key]}: {s['pass']}/{s['total']} pass, {s['fail']} fail{extra}")
        for category, counts in sorted(s["categories"].items()):
            total = sum(counts.values())
            print(f"      {category:<22} {counts.get('pass', 0)}/{total}")
        bad = [c for c in results["checks"] if c["layer"] == int(key[1]) and c["status"] != "pass"]
        for c in bad:
            print(f"    [{c['status'].upper():5}] {c['id']}")
            if c["status"] == "error":
                print(f"            {c['detail']}")
            elif c["layer"] != 1:
                print(f"            expected {c['expected']}")
                print(f"            observed {c['observed']}")

    for device, detail in results.get("layer3", {}).items():
        sections = [("config vs intent", detail.get("diffs")), ("unsaved / unapplied", detail.get("unsaved")),
                    ("FRR vs VyOS config", detail.get("frr"))]
        if not any(items for _, items in sections):
            continue
        print(f"\n  --- {device} ---")
        for title, items in sections:
            for d in items or []:
                if "item" in d:
                    print(f"    {title}: {d['kind']} {d['path']}: {d['item']}")
                else:
                    value = d["live"] if d["kind"] == "extra" else d["intent"]
                    change = f" (intent {d['intent']}, live {d['live']})" if d["kind"] == "changed" else ""
                    shown = "" if value is True or d["kind"] == "changed" else f" = {value}"
                    print(f"    {title}: {d['kind']:7} {d['path']}{shown}{change}")

    if "comparison" in results:
        c = results["comparison"]
        print("\n  Compared with baseline:")
        for key in ("resolved", "collateral", "config_introduced", "config_removed"):
            print(f"    {key}: {len(c[key])}")
            for item in c[key]:
                print(f"      {item}")
    print("=" * WIDTH)


def main():
    parser = argparse.ArgumentParser(description="Three-layer verifier for the v2 lab.")
    parser.add_argument("--layers", default="123", help="Layers to run, e.g. 123 or 3 (default: 123)")
    parser.add_argument("--run-number", type=int, help="Run number for the output file (default: next free)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT, help="Where to write run_NNN.json")
    parser.add_argument("--no-file", action="store_true", help="Print only, don't write result files")
    parser.add_argument("--configs-dir", default=DEFAULT_CONFIGS, help="Intent configs (default: v2/configs)")
    parser.add_argument("--from-raw", metavar="RAW_JSON", help="Re-grade a saved run_NNN.raw.json instead of collecting")
    parser.add_argument("--compare", metavar="RUN_JSON", help="Baseline run_NNN.json to compare against")
    parser.add_argument("--l2-interval", type=int, default=30, help="Seconds between Layer 2 snapshots (default: 30)")
    parser.add_argument("--l2-timeout", type=int, default=180, help="Layer 2 convergence timeout in seconds (default: 180)")
    parser.add_argument("--no-retry", action="store_true", help="Don't re-ping failed pairs")
    args = parser.parse_args()

    layers = {int(c) for c in args.layers if c in "123"}
    started = datetime.now(timezone.utc).isoformat()
    if args.from_raw:
        with open(args.from_raw, encoding="utf-8") as f:
            saved = json.load(f)
        raw, started = saved["raw"], saved.get("timestamp", started)
        raw = {k: v for k, v in raw.items() if int(k[-1]) in layers}
    else:
        raw = collect(layers, args)

    results = {"timestamp": started, "layers": sorted(int(k[-1]) for k in raw), **analyze(raw, args.configs_dir)}
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            results["comparison"] = compare(results, json.load(f))

    print()
    print_report(results)

    if not args.no_file:
        os.makedirs(args.output_dir, exist_ok=True)
        run = args.run_number if args.run_number is not None else next_run_number(args.output_dir)
        results["run"] = run
        base = os.path.join(args.output_dir, f"run_{run:03d}")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        if not args.from_raw:
            with open(base + ".raw.json", "w", encoding="utf-8") as f:
                json.dump({"timestamp": started, "raw": raw}, f, indent=2)
        print(f"  Results saved to {os.path.relpath(base + '.json', PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
