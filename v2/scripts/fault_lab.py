#!/usr/bin/env python3
"""Inject the v2 faults into the live lab and check that each one behaves as predicted.

Run from inside v2/:
    python scripts/fault_lab.py list                  # the 12 faults
    python scripts/fault_lab.py status                # which faults are on the lab right now
    python scripts/fault_lab.py test 4                # inject, verify, grade, revert, re-verify
    python scripts/fault_lab.py test all              # the same for 1..12, one at a time
    python scripts/fault_lab.py regrade results/faults/run_001   # re-grade saved runs offline
    python scripts/fault_lab.py inject 4 6            # inject and leave in place
    python scripts/fault_lab.py revert 4 6
    python scripts/fault_lab.py inject all            # broken lab for an agent run (saves a baseline run)
    python scripts/fault_lab.py revert all            # back to the correct lab

Every inject and revert is read back: the device's Layer 3 diff must equal the diff computed
from the catalog's config edits (so the live command did what the broken config file says),
and on VyOS the change must be saved and rendered into FRR.

test grades each fault against the catalog's expect/may globs and writes
results/faults/run_NNN/fault_NN.json (verifier results plus raw output) and summary.json.
A test refuses to start on a lab that isn't clean, and stops the sweep if a revert doesn't bring
the lab back to clean, so one bad fault can't contaminate the next.

This script and faults/ are an answer key. Keep them out of the directory Claude Code runs from.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_lab  # noqa: E402
from faults.catalog import (  # noqa: E402
    BY_NUM, FAULT_DEVICES, FAULTS, OSPF_REVERT_SETTLE, OSPF_SETTLE, diff_key, expected_diffs, fault_states, grade,
)
from faults.push import PushError, push, run_op  # noqa: E402
from verifier import layer3  # noqa: E402

FAULTS_OUTPUT = os.path.join(verify_lab.PROJECT_ROOT, "results", "faults")
INTENT = layer3.load_intent(verify_lab.DEFAULT_CONFIGS)
READBACK_ATTEMPTS = 3
READBACK_DELAY = 5
CLEAN_RETRY_DELAY = 60
WIDTH = verify_lab.WIDTH


class LabError(Exception):
    pass


class NotInFrr(LabError):
    """The VyOS config matches but FRR's running config doesn't."""


def log(msg=""):
    print(msg, flush=True)


def wait(seconds, why):
    if seconds > 0:
        log(f"  waiting {seconds}s ({why})...")
        time.sleep(seconds)


# ---------------------------------------------------------------------------
# Device state (Layer 3 read-back)
# ---------------------------------------------------------------------------

def read_back(devices):
    """Return {device: {"diffs": [...], "checks": {id: check}}} or {"error": ...}."""
    raw = layer3.collect(devices)
    checks, details = layer3.analyze(raw, INTENT)
    out = {}
    for name in devices:
        if "error" in raw[name]:
            out[name] = {"error": raw[name]["error"]}
        else:
            out[name] = {"diffs": details[name]["diffs"], "unsaved": details[name].get("unsaved") or [],
                         "frr": details[name].get("frr") or [], "raw": raw[name],
                         "checks": {c["id"]: c for c in checks if c["device"] == name}}
    return out


def _detail_lines(rb):
    """The individual differences behind failing persistence and FRR checks."""
    lines = []
    for title, items in (("FRR vs VyOS config", rb.get("frr")), ("unsaved", rb.get("unsaved"))):
        for d in items or []:
            if "item" in d:
                lines.append(f"  {title}: {d['kind']} {d['path']}: {d['item']}")
            else:
                lines.append(f"  {title}: {d['kind']} {d['path']} (running {d.get('intent')}, other {d.get('live')})")
    return lines


def fmt_key(key):
    kind, path, intent, live = key
    return f"{kind} {path} (intent {intent}, live {live})"


def confirm(device, nums):
    """Read the device back until it shows exactly the given faults, saved and applied."""
    want = expected_diffs(device, nums, INTENT[device])
    problems, only_frr = [], False
    for attempt in range(READBACK_ATTEMPTS):
        if attempt:
            time.sleep(READBACK_DELAY)
        rb = read_back([device])[device]
        if "error" in rb:
            problems = [f"read-back failed: {rb['error']}"]
            continue
        got = {diff_key(d) for d in rb["diffs"]}
        failing = [cid for cid, c in rb["checks"].items() if not cid.startswith("L3.config.") and c["status"] != "pass"]
        problems = [f"missing from device: {fmt_key(k)}" for k in sorted(want - got)]
        problems += [f"not expected on device: {fmt_key(k)}" for k in sorted(got - want)]
        only_frr = not problems and failing == [f"L3.frr_sync.{device}"]
        problems += [f"{cid}: {rb['checks'][cid]['status']} ({rb['checks'][cid]['observed'] or rb['checks'][cid]['detail']})"
                     for cid in failing]
        if not problems:
            return
        problems += _detail_lines(rb)
    if "raw" in rb:
        os.makedirs(FAULTS_OUTPUT, exist_ok=True)
        save_json(os.path.join(FAULTS_OUTPUT, f"readback_{device}_{datetime.now():%Y%m%d-%H%M%S}.json"), rb)
    raise (NotInFrr if only_frr else LabError)(f"{device} read-back doesn't match faults {sorted(nums) or 'none'}:\n    " + "\n    ".join(problems))


def _prepare(f, want_state, force):
    """Check the device before a change. Returns the faults currently on it, or None to skip."""
    rb = read_back([f.device])[f.device]
    if "error" in rb:
        raise LabError(f"can't read {f.device}: {rb['error']}")
    states, unexplained = fault_states(f.device, rb["diffs"], INTENT[f.device])
    if states[f.num] == want_state:
        log(f"  fault {f.num} is already {want_state} on {f.device}, skipping")
        return None
    if unexplained and not force:
        raise LabError(f"{f.device} has config that no fault explains (use --force to go ahead):\n    "
                       + "\n    ".join(fmt_key(k) for k in sorted(unexplained)))
    if states[f.num] == "partial" and want_state == "injected" and not force:
        raise LabError(f"fault {f.num} is partially on {f.device} already; revert it first")
    return [n for n, s in states.items() if s == "injected" and n != f.num]


def _apply(f, commands, undo, others, after):
    push(f.device, commands)
    for device, cmd in f.kick:
        run_op(device, cmd)
    try:
        confirm(f.device, others + after)
    except NotInFrr as e:
        # The VyOS config is right but FRR never got it (seen on the first commit after a
        # router boots). Toggling the change and committing it again gets it applied.
        log(f"  VyOS committed but FRR didn't apply it; toggling the change and committing again\n    {e}")
        push(f.device, undo)
        push(f.device, commands)
        confirm(f.device, others + after)


def inject_fault(f, force=False):
    log(f"Injecting fault {f.num} on {f.device}: {f.title}")
    others = _prepare(f, "injected", force)
    if others is None:
        return False
    _apply(f, f.inject, f.revert, others, [f.num])
    log("  injected, read-back matches")
    return True


def revert_fault(f, force=False):
    log(f"Reverting fault {f.num} on {f.device}")
    others = _prepare(f, "clean", force)
    if others is None:
        return False
    _apply(f, f.revert, f.inject, others, [])
    log("  reverted, read-back matches")
    return True


# ---------------------------------------------------------------------------
# Verifier runs and grading
# ---------------------------------------------------------------------------

def run_verifier(layers):
    args = SimpleNamespace(l2_interval=30, l2_timeout=180, no_retry=False)
    started = datetime.now(timezone.utc).isoformat()
    raw = verify_lab.collect(layers, args)
    results = {"timestamp": started, "layers": sorted(int(k[-1]) for k in raw),
               **verify_lab.analyze(raw, verify_lab.DEFAULT_CONFIGS)}
    return results, raw


def not_passing(results):
    return sorted(c["id"] for c in results["checks"] if c["status"] != "pass")


def describe_bad(results):
    """Summarize non-passing checks, keeping real failures apart from devices that couldn't be read."""
    failed = sorted(c["id"] for c in results["checks"] if c["status"] in ("fail", "flaky"))
    errors = [c for c in results["checks"] if c["status"] == "error"]
    parts = []
    if failed:
        parts.append(f"{len(failed)} checks fail: " + ", ".join(failed[:15]) + (" ..." if len(failed) > 15 else ""))
    if errors:
        unreachable = sorted({n for n, e in results.get("connections", {}).items()})
        parts.append(f"{len(errors)} checks could not be collected"
                     + (f" (no connection to {', '.join(unreachable)})" if unreachable else ""))
    return "; ".join(parts)


def require_clean(layers, force, what):
    log(f"Checking that the lab is clean ({what})...")
    results, _ = run_verifier(layers)
    bad = not_passing(results)
    if bad:
        msg = describe_bad(results)
        if not force:
            raise LabError(f"the lab isn't clean ({what}); {msg}. Fix it or use --force.")
        log(f"  --force: going ahead anyway; {msg}")
    else:
        log("  clean")


def next_dir(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    numbers = [int(m.group(1)) for d in os.listdir(output_dir) if (m := re.match(r"run_(\d{3})$", d))]
    path = os.path.join(output_dir, f"run_{max(numbers, default=0) + 1:03d}")
    os.makedirs(path)
    return path


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def fault_meta(f):
    return {"num": f.num, "device": f.device, "title": f.title, "expect": f.expect, "may": f.may}


def print_grade(g, verbose=True):
    counts = "  ".join(f"{k} {len(v)}" for k, v in g["failing_by_layer"].items())
    log(f"  verdict: {g['verdict'].upper()}   failing checks: {counts}")
    if not verbose:
        return
    for key, label in (("missed", "predicted to fail but passed"), ("unexpected", "failed but not predicted"),
                       ("errors", "could not be collected"), ("unmatched_globs", "globs matching no check"),
                       ("may_failed", "allowed to fail and did")):
        if g[key]:
            log(f"    {label} ({len(g[key])}):")
            for item in g[key]:
                log(f"      {item}")


def print_summary(records):
    log("=" * WIDTH)
    log("  FAULT x LAYER (measured)")
    log("=" * WIDTH)
    log(f"  {'#':>2}  {'Device':<9} {'L1':>4} {'L2':>4} {'L3':>4}  {'missed':>6} {'unexp.':>6}  {'verdict':<9} after revert")
    for r in records:
        f = r["fault"]
        g = r.get("grade")
        if g is None:
            log(f"  {f['num']:>2}  {f['device']:<9} {'-':>4} {'-':>4} {'-':>4}  {'-':>6} {'-':>6}  {'not run':<9} {(r.get('error') or '-').splitlines()[0]}")
            continue
        n = {k: len(v) for k, v in g["failing_by_layer"].items()}
        bad = r.get("post_revert_failing")
        after = "not checked" if bad is None else ("clean" if not bad else f"{len(bad)} failing")
        log(f"  {f['num']:>2}  {f['device']:<9} {n.get('L1', '-'):>4} {n.get('L2', '-'):>4} {n.get('L3', '-'):>4}  "
            f"{len(g['missed']):>6} {len(g['unexpected']):>6}  {g['verdict']:<9} {after}")
    log("=" * WIDTH)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def parse_nums(values):
    if values == ["all"]:
        return [f.num for f in FAULTS]
    try:
        nums = [int(v) for v in values]
    except ValueError:
        raise SystemExit("faults are numbers 1-12 or 'all'")
    unknown = [n for n in nums if n not in BY_NUM]
    if unknown:
        raise SystemExit(f"unknown fault(s): {unknown}")
    return nums


def cmd_list(_args):
    for f in FAULTS:
        log(f"  {f.num:>2}  {f.device:<9} {f.title}")


def cmd_status(_args):
    rb = read_back(FAULT_DEVICES)
    for device in FAULT_DEVICES:
        if "error" in rb[device]:
            log(f"  {device}: unreachable ({rb[device]['error']})")
            continue
        states, unexplained = fault_states(device, rb[device]["diffs"], INTENT[device])
        for num, state in states.items():
            log(f"  {num:>2}  {device:<9} {state}")
        for key in sorted(unexplained):
            log(f"      {device} also differs from intent: {fmt_key(key)}")
        for cid, c in rb[device]["checks"].items():
            if not cid.startswith("L3.config.") and c["status"] != "pass":
                log(f"      {cid}: {c['status']}")


def cmd_inject(args):
    nums = parse_nums(args.faults)
    if args.faults != ["all"]:
        for n in nums:
            inject_fault(BY_NUM[n], args.force)
        return

    require_clean(args.layers, args.force, "before breaking it")
    for n in nums:
        inject_fault(BY_NUM[n], args.force)
    wait(OSPF_SETTLE, "OSPF dead interval")
    log("Recording the broken baseline...")
    results, raw = run_verifier(args.layers)
    results["note"] = "broken baseline: fault_lab.py inject all"
    log()
    verify_lab.print_report(results)
    os.makedirs(verify_lab.DEFAULT_OUTPUT, exist_ok=True)
    run = verify_lab.next_run_number(verify_lab.DEFAULT_OUTPUT)
    results["run"] = run
    base = os.path.join(verify_lab.DEFAULT_OUTPUT, f"run_{run:03d}")
    save_json(base + ".json", results)
    save_json(base + ".raw.json", {"timestamp": results["timestamp"], "raw": raw})
    rel = os.path.relpath(base + ".json", verify_lab.PROJECT_ROOT)
    log(f"\nAll {len(nums)} faults injected. Broken baseline saved to {rel}")
    log(f"After the agent run:  python scripts/verify_lab.py --compare {rel}")


def cmd_revert(args):
    nums = parse_nums(args.faults)
    for n in reversed(nums):
        revert_fault(BY_NUM[n], args.force)
    if args.faults == ["all"]:
        wait(OSPF_REVERT_SETTLE, "OSPF re-convergence")
        require_clean(args.layers, False, "after reverting everything")


def cmd_test(args):
    nums = parse_nums(args.faults)
    run_dir = next_dir(FAULTS_OUTPUT)
    log(f"Results go to {os.path.relpath(run_dir, verify_lab.PROJECT_ROOT)}")
    require_clean(args.layers, args.force, "preflight")
    records = []
    try:
        for n in nums:
            record = test_one(BY_NUM[n], args)
            records.append(record)
            save_json(os.path.join(run_dir, f"fault_{n:02d}.json"), record)
            if record.get("stop"):
                log(f"\nStopping: {record['stop']}")
                break
    finally:
        save_json(os.path.join(run_dir, "summary.json"),
                  [{k: r.get(k) for k in ("fault", "grade", "error", "post_revert_failing")} for r in records])
        if records:
            print_summary(records)


def test_one(f, args):
    log()
    log("-" * WIDTH)
    record = {"fault": fault_meta(f), "started": datetime.now(timezone.utc).isoformat()}
    try:
        try:
            inject_fault(f)
            wait(f.settle, "letting the fault take effect")
            log("Running the verifier with the fault in place...")
            results, raw = run_verifier(args.layers)
            record.update(grade=grade(f, results["checks"]), results=results, raw=raw)
            print_grade(record["grade"])
        except (LabError, PushError) as e:
            record["error"] = str(e)
            log(f"  ERROR: {e}")
        finally:
            revert_fault(f)
    except (LabError, PushError) as e:
        record["error"] = f"{record.get('error', '')}\nrevert failed: {e}".strip()
        record["stop"] = f"fault {f.num} could not be reverted; fix {f.device} by hand ({e})"
        return record

    wait(f.revert_settle, "re-convergence after the revert")
    log("Checking the lab is clean again...")
    post, _ = run_verifier(args.layers)
    bad = not_passing(post)
    if bad:
        wait(CLEAN_RETRY_DELAY, f"{len(bad)} checks still failing, giving it more time")
        post, _ = run_verifier(args.layers)
        bad = not_passing(post)
    record["post_revert_failing"] = bad
    if bad:
        record["stop"] = f"the lab isn't clean after reverting fault {f.num}: {describe_bad(post)}"
    else:
        log("  clean")
    return record


def cmd_regrade(args):
    records = []
    for name in sorted(os.listdir(args.run_dir)):
        if not re.match(r"fault_\d{2}\.json$", name):
            continue
        with open(os.path.join(args.run_dir, name), encoding="utf-8") as fh:
            record = json.load(fh)
        f = BY_NUM[record["fault"]["num"]]
        if "raw" in record:
            results = verify_lab.analyze(record["raw"], verify_lab.DEFAULT_CONFIGS)
            record["grade"] = grade(f, results["checks"])
            log(f"\nFault {f.num} ({f.device}):")
            print_grade(record["grade"])
        records.append(record)
    print_summary(records)


def main():
    parser = argparse.ArgumentParser(description="Inject v2 faults and check how the verifier sees them.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List the faults")
    sub.add_parser("status", help="Show which faults are on the lab")
    for name, help_text in (("inject", "Inject faults and leave them ('all': broken lab for an agent run)"),
                            ("revert", "Revert faults"),
                            ("test", "Inject, verify, grade and revert each fault in turn")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("faults", nargs="+", help="fault numbers or 'all'")
        p.add_argument("--force", action="store_true",
                       help="go ahead on a lab that isn't clean or a device with unexplained config")
        p.add_argument("--layers", default="123", help="verifier layers to run (default: 123)")
    p = sub.add_parser("regrade", help="Re-grade a saved test run with the current catalog")
    p.add_argument("run_dir")
    args = parser.parse_args()
    if hasattr(args, "layers"):
        args.layers = {int(c) for c in args.layers if c in "123"}

    commands = {"list": cmd_list, "status": cmd_status, "inject": cmd_inject, "revert": cmd_revert,
                "test": cmd_test, "regrade": cmd_regrade}
    try:
        commands[args.command](args)
    except (LabError, PushError) as e:
        log(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
