#!/usr/bin/env python3
"""Per-fault scoring of an agent run (planning/faults-and-verifier.md, "Scoring").

A verifier run says which checks fail; this says which of the 12 faults the agent fixed.

Config verdict, from the fault's own Layer 3 paths (faults/catalog.py l3_diff) on its device:
    clean      nothing differs from intent at any of the fault's paths
    injected   exactly the injected difference is still there         -> not_fixed
    partial    part of the injected difference is gone, nothing new   -> partial
    changed    the paths still differ, but not as injected            -> changed
               (a wrong value, or a workaround in the fault's place)

A clean fault must also pass its symptom checks: the Layer 1/2 checks that failed when the
fault was injected on its own (fault_lab.py test, results/faults/reference_single_faults). A failing symptom
check is excused ("masked") when another fault that is still not clean also breaks it, e.g. #4
(no HQ-Edge adjacency) still unfixed hides whether #10's 1.1.1.1 pings work. Anything left
over makes the verdict "ineffective": the config looks right but the network doesn't behave.
That usually means a VyOS commit that never reached FRR (frr_sync), but collateral damage
elsewhere can cause it too, so check the run's collateral list.

Run-level results are reported next to the fault count, not in it:
    workarounds   Layer 3 differences no fault explains (extra or changed config)
    collateral    checks that passed on the broken baseline and fail now (needs --baseline)
    unsaved       running config that differs from saved (VyOS) or from the interfaces file (Alpine)
    frr_sync      VyOS devices whose committed config isn't what FRR is running

By default each run is re-graded from its run_NNN.raw.json with the current verifier, so
verifier fixes apply to old runs. fault_lab.py test records (fault_NN.json) are accepted too.

Run from inside v2/:
    python scripts/score_run.py results/verify/run_005.json --baseline results/verify/run_004.json
    python scripts/score_run.py runs/run_0*/verify/run_*.json --baseline-name baseline.json
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from faults.catalog import FAULTS, diff_key, expected_diffs  # noqa: E402
from verifier.layer3 import load_intent  # noqa: E402
from verify_lab import DEFAULT_CONFIGS, PROJECT_ROOT, WIDTH, analyze  # noqa: E402

DEFAULT_SYMPTOMS = os.path.join(PROJECT_ROOT, "results", "faults", "reference_single_faults")
VERDICTS = ("fixed", "ineffective", "changed", "partial", "not_fixed", "error")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_results(path, configs_dir, regrade=True):
    """Return (results, graded_from) for a verify_lab.py run or a fault_lab.py test record."""
    data = load_json(path)
    if "results" in data and "raw" in data:
        results, raw = data["results"], data["raw"]
    else:
        raw_path = path[:-len(".json")] + ".raw.json"
        results, raw = data, (load_json(raw_path)["raw"] if os.path.exists(raw_path) else None)
    if regrade and raw is not None:
        return {**analyze(raw, configs_dir), "timestamp": results.get("timestamp")}, "raw"
    return results, "saved"


def load_symptoms(symptoms_dir):
    """{fault num: Layer 1/2 check IDs that fail with only that fault injected}.

    symptoms_dir is a fault_lab.py test run, or a JSON file of the table itself (test fixture).
    """
    if symptoms_dir.endswith(".json"):
        return {int(n): ids for n, ids in load_json(symptoms_dir).items()}
    symptoms = {}
    for f in FAULTS:
        record = load_json(os.path.join(symptoms_dir, f"fault_{f.num:02d}.json"))
        symptoms[f.num] = sorted(c["id"] for c in record["results"]["checks"]
                                 if c["layer"] in (1, 2) and c["status"] != "pass")
    return symptoms


def config_state(f, diffs, intent):
    """Classify the fault's own Layer 3 paths: clean, injected, partial or changed."""
    paths = {path for _, path in f.l3_diff}
    live = {diff_key(d) for d in diffs if d["path"] in paths}
    injected = expected_diffs(f.device, [f.num], intent)
    if not live:
        return "clean"
    if live == injected:
        return "injected"
    return "partial" if live < injected else "changed"


def score(results, intent, symptoms, baseline=None):
    status = {c["id"]: c["status"] for c in results["checks"]}
    failing = {i for i, s in status.items() if s != "pass"}
    layer3 = results.get("layer3", {})

    states = {}
    for f in FAULTS:
        detail = layer3.get(f.device, {})
        states[f.num] = "error" if "error" in detail else config_state(f, detail.get("diffs") or [], intent[f.device])
    not_clean = {n for n, s in states.items() if s != "clean"}

    faults = {}
    for f in FAULTS:
        state = states[f.num]
        broken_now = [c for c in symptoms[f.num] if c in failing]
        masked = {c: sorted(m for m in not_clean if m != f.num and c in symptoms[m]) for c in broken_now}
        unexplained = [c for c in broken_now if not masked[c]]
        if state == "clean":
            verdict = "ineffective" if unexplained else "fixed"
        else:
            verdict = {"injected": "not_fixed", "error": "error"}.get(state, state)
        faults[f.num] = {
            "device": f.device,
            "title": f.title,
            "verdict": verdict,
            "config": state,
            "config_diffs": [d for d in layer3.get(f.device, {}).get("diffs") or []
                             if d["path"] in {p for _, p in f.l3_diff}],
            "symptoms_failing": unexplained,
            "symptoms_masked": {c: ms for c, ms in masked.items() if ms},
            "saved": status.get(f"L3.persisted.{f.device}") == "pass",
            "frr_sync": status.get(f"L3.frr_sync.{f.device}"),
        }

    workarounds = []
    for device, detail in layer3.items():
        fault_paths = {p for f in FAULTS if f.device == device for _, p in f.l3_diff}
        workarounds += [{"device": device, **d} for d in detail.get("diffs") or [] if d["path"] not in fault_paths]

    def bad(prefix):
        return sorted(i[len(prefix):] for i in failing if i.startswith(prefix))

    counts = Counter(v["verdict"] for v in faults.values())
    out = {
        "fixed": counts["fixed"],
        "total": len(FAULTS),
        "verdicts": {v: counts[v] for v in VERDICTS if counts[v]},
        "faults": faults,
        "workarounds": workarounds,
        "unsaved": bad("L3.persisted."),
        "frr_out_of_sync": bad("L3.frr_sync."),
        "connection_errors": results.get("connections", {}),
    }
    if baseline is not None:
        before = {c["id"]: c["status"] for c in baseline["checks"]}
        out["collateral"] = sorted(i for i in failing if before.get(i) == "pass")
    out["perfect"] = out["fixed"] == out["total"]
    out["clean"] = (out["perfect"] and not workarounds and not out["unsaved"] and not out["frr_out_of_sync"]
                    and not out.get("collateral"))
    return out


def print_run(name, s):
    collateral = len(s["collateral"]) if "collateral" in s else "n/a"
    print(f"\n  {name}: {s['fixed']}/{s['total']} fixed   workarounds {len(s['workarounds'])}   "
          f"collateral {collateral}   unsaved {len(s['unsaved'])}   frr out of sync {len(s['frr_out_of_sync'])}")
    for num, f in s["faults"].items():
        notes = []
        if f["verdict"] in ("changed", "partial"):
            notes += [f"{d['kind']} {d['path']} (intent {d['intent']}, live {d['live']})" for d in f["config_diffs"]]
        if f["verdict"] == "ineffective":
            notes.append(f"{len(f['symptoms_failing'])} symptom checks still fail: "
                         + ", ".join(f["symptoms_failing"][:3]) + (" ..." if len(f["symptoms_failing"]) > 3 else ""))
        if f["verdict"] == "fixed" and f["symptoms_masked"]:
            by = sorted({m for ms in f["symptoms_masked"].values() for m in ms})
            notes.append(f"{len(f['symptoms_masked'])} symptom checks masked by #" + ", #".join(map(str, by)))
        if f["config"] == "clean" and not f["saved"]:
            notes.append("not saved")
        print(f"    #{num:<2} {f['device']:<9} {f['verdict']:<12} {notes[0] if notes else ''}")
        for note in notes[1:]:
            print(f"    {'':<25}{note}")
    for w in s["workarounds"]:
        print(f"    workaround: {w['device']} {w['kind']} {w['path']} (intent {w['intent']}, live {w['live']})")
    for i in s.get("collateral", []):
        print(f"    collateral: {i}")


def print_summary(scored):
    print(f"\n  {len(scored)} runs: per-fault fix rate")
    for f in FAULTS:
        verdicts = Counter(s["faults"][f.num]["verdict"] for _, s in scored)
        others = ", ".join(f"{v} {verdicts[v]}" for v in VERDICTS if v != "fixed" and verdicts[v])
        print(f"    #{f.num:<2} {f.device:<9} {verdicts['fixed']:>3}/{len(scored)}   {others}")
    fixed = sum(s["fixed"] for _, s in scored)
    print(f"\n    fixed {fixed}/{len(scored) * len(FAULTS)} faults, "
          f"{sum(s['perfect'] for _, s in scored)} perfect runs, {sum(s['clean'] for _, s in scored)} clean runs")


def main():
    parser = argparse.ArgumentParser(description="Score which of the 12 faults a run fixed.")
    parser.add_argument("runs", nargs="+", help="verify_lab.py run_NNN.json files (globs are expanded)")
    parser.add_argument("--baseline", help="Broken-baseline run_NNN.json for every run (collateral damage)")
    parser.add_argument("--baseline-name",
                        help="Baseline file name looked up next to each run instead (one baseline per run)")
    parser.add_argument("--symptoms", default=DEFAULT_SYMPTOMS,
                        help="fault_lab.py test run with every fault measured on its own (default: reference_single_faults)")
    parser.add_argument("--configs-dir", default=DEFAULT_CONFIGS, help="Intent configs (default: v2/configs)")
    parser.add_argument("--no-regrade", action="store_true", help="Use the saved checks instead of the raw output")
    parser.add_argument("--no-file", action="store_true", help="Don't write run_NNN.score.json next to each run")
    parser.add_argument("--quiet", action="store_true", help="Only print the summary")
    args = parser.parse_args()

    paths = sorted(p for pattern in args.runs for p in (glob.glob(pattern) or [pattern])
                   if not p.endswith((".raw.json", ".score.json")))
    intent = load_intent(args.configs_dir)
    symptoms = load_symptoms(args.symptoms)
    shared_baseline = load_results(args.baseline, args.configs_dir, not args.no_regrade)[0] if args.baseline else None

    scored = []
    for path in paths:
        baseline = shared_baseline
        if args.baseline_name:
            baseline_path = os.path.join(os.path.dirname(path), args.baseline_name)
            baseline = load_results(baseline_path, args.configs_dir, not args.no_regrade)[0]
        results, graded_from = load_results(path, args.configs_dir, not args.no_regrade)
        s = {"source": os.path.abspath(path), "graded_from": graded_from, "timestamp": results.get("timestamp"),
             **score(results, intent, symptoms, baseline)}
        scored.append((path, s))
        if not args.quiet:
            print_run(os.path.relpath(path), s)
        if not args.no_file:
            with open(path[:-len(".json")] + ".score.json", "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2, default=str)

    if len(scored) > 1 or args.quiet:
        print_summary(scored)
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
