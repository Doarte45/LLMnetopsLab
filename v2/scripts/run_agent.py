#!/usr/bin/env python3
"""Run one unattended Claude Code session against the v2 lab, end to end.

Runs inside WSL (planning/agent-runs.md). For run N:
  1. reset     lab back to correct (revert faults if needed), confirmed 211/211
  2. break     inject all 12 faults, read back, record the broken baseline
  3. agent     fresh directory with only CLAUDE.md, headless claude -p "Fix the network"
  4. readback  Layer 3 right after the agent exits (catches v1-style config reverts)
  5. verify    all three layers, compared against the baseline
  6. readback  Layer 3 again, to see whether anything changed during the verify
  7. score     per-fault verdicts (score_run.py)
  8. collect   transcript, stream output, meta.json, answer-key access flags

Driver output is staged outside the agent's directory while the agent runs (the baseline is
a partial answer key) and moved into <run dir>/results afterwards. A copy of results and
transcript goes to v2/results/runs/<label>/run_NNN in the repo.

    python3 scripts/run_agent.py 0                 # try run: ~/claudenetops/opus/run_000
    python3 scripts/run_agent.py 1 --label opus
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fault_lab  # noqa: E402
import score_run  # noqa: E402
import verify_lab  # noqa: E402
from faults.catalog import FAULTS, OSPF_REVERT_SETTLE, OSPF_SETTLE  # noqa: E402
from verifier import layer3  # noqa: E402

PROMPT = "Fix the network"
CLAUDE_MD = os.path.join(verify_lab.PROJECT_ROOT, "claude-prompt", "CLAUDE.md")
REPO_RUNS = os.path.join(verify_lab.PROJECT_ROOT, "results", "runs")
AGENT_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",       # no claude.ai connectors (Gmail, Drive, Notion...)
    "DISABLE_AUTOUPDATER": "1",                   # one Claude Code version for the whole batch
    "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP": "1",  # never swap the requested model
}
# Strings in the transcript that suggest the agent looked outside its directory for answers
ANSWER_KEY_MARKERS = ("/mnt/c", "LLMnetopsLab", "claudenetops/", "fault_lab", "catalog.py", "verify_lab",
                      "-broken.", "results/verify", "/.claude/projects")

log = fault_lab.log


def save_json(path, data):
    fault_lab.save_json(path, data)


def now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Lab
# ---------------------------------------------------------------------------

def lab_is_clean():
    results, _ = fault_lab.run_verifier({1, 2, 3})
    return not fault_lab.not_passing(results), results


def clean_empty_route_map_sets():
    """Delete empty "policy route-map X rule N set" nodes on VyOS.

    VyOS keeps the empty parent after its only leaf (local-preference) is deleted. A previous
    agent can leave one behind; fault_lab refuses to revert a device with config no fault
    explains, so remove these before reverting. Nothing else is cleaned automatically.
    """
    vyos = [n for n in fault_lab.FAULT_DEVICES if fault_lab.layer3.DEVICE_BY_NAME[n]["platform"] == "vyos"]
    for device, rb in fault_lab.read_back(vyos).items():
        for d in rb.get("diffs") or []:
            parts = d["path"].split(".")
            if d["kind"] == "extra" and d["live"] is True and len(parts) == 4 and parts[0] == "route_maps" \
                    and parts[3] == "set":
                cmd = f"delete policy route-map {parts[1]} {parts[2]} set"
                log(f"  removing empty leftover on {device}: {cmd}")
                fault_lab.push(device, [cmd])


# Stock VyOS boot scripts (md5). The verifier doesn't read these and nothing restores them, so an
# agent's edit would survive into later runs and take effect on the next reboot (run_020 wrote one
# that re-applies a vtysh filter-list on Hub-B).
BOOT_SCRIPTS = {
    "/config/scripts/vyos-postconfig-bootup.script": "52f5657a7cdf4d21ff528b894556e72b",
    "/config/scripts/vyos-preconfig-bootup.script": "8fba3d905d321b1cf99a725d67c73464",
}


def check_boot_scripts():
    """Raise LabError if any VyOS router's /config/scripts differs from stock (changed, added, removed)."""
    problems = []
    for name, device in fault_lab.layer3.DEVICE_BY_NAME.items():
        if device["platform"] != "vyos":
            continue
        out = fault_lab.run_op(name, "find /config/scripts -type f -exec md5sum {} +")
        live = {path: md5 for md5, path in (line.split(None, 1) for line in out.splitlines() if line.strip())}
        for path in sorted(set(live) | set(BOOT_SCRIPTS)):
            if live.get(path) != BOOT_SCRIPTS.get(path):
                state = "missing" if path not in live else "not stock" if path in BOOT_SCRIPTS else "added"
                problems.append(f"{name} {path} {state}")
    if problems:
        raise fault_lab.LabError("boot scripts differ from stock: " + "; ".join(problems)
                                 + ". Restore them by hand (copy the file from another VyOS router).")
    log("  boot scripts stock on every VyOS router")


def reset_lab():
    """Bring the lab back to the correct config, or raise LabError."""
    log("Reset: checking the lab...")
    check_boot_scripts()
    clean, results = lab_is_clean()
    if clean:
        log("  clean (211/211)")
        return
    log(f"  not clean: {fault_lab.describe_bad(results)}")
    clean_empty_route_map_sets()
    reverted = False
    for f in reversed(FAULTS):
        reverted |= fault_lab.revert_fault(f)
    if reverted:
        fault_lab.wait(OSPF_REVERT_SETTLE, "OSPF re-convergence")
    clean, results = lab_is_clean()
    if not clean:
        raise fault_lab.LabError("lab still not clean after reverting every fault: "
                                 f"{fault_lab.describe_bad(results)}. Fix by hand (or reload base configs).")
    log("  clean (211/211)")


def break_lab(staging):
    """Inject all 12 faults and record the broken baseline. Returns the baseline results."""
    for f in FAULTS:
        fault_lab.inject_fault(f)
    fault_lab.wait(OSPF_SETTLE, "OSPF dead interval")
    log("Recording the broken baseline...")
    results, raw = fault_lab.run_verifier({1, 2, 3})
    results["note"] = "broken baseline: run_agent.py"
    save_json(os.path.join(staging, "baseline.json"), results)
    save_json(os.path.join(staging, "baseline.raw.json"), {"timestamp": results["timestamp"], "raw": raw})
    log("  baseline: " + ", ".join(f"{k} {v['pass']}/{v['total']}" for k, v in results["summary"].items()))
    return results


def readback(staging, name):
    """Layer 3 only: every device's running/saved/FRR config, right now."""
    started = now()
    raw = {"layer3": layer3.collect()}
    results = {"timestamp": started, **verify_lab.analyze(raw, verify_lab.DEFAULT_CONFIGS)}
    save_json(os.path.join(staging, f"{name}.json"), results)
    save_json(os.path.join(staging, f"{name}.raw.json"), {"timestamp": started, "raw": raw})
    return results


def l3_items(results):
    return sorted(f"{dev} {d['kind']} {d['path']} = {d['live']}"
                  for dev, detail in results.get("layer3", {}).items() for d in detail.get("diffs") or [])


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def claude_version():
    out = subprocess.run(["claude", "--version"], capture_output=True, text=True, env={**os.environ, **AGENT_ENV})
    return out.stdout.strip()


def preflight(args):
    """One tiny real API call (auth status alone doesn't catch an expired token), outside any run dir."""
    probe = os.path.join(args.root, ".staging", "probe")
    os.makedirs(probe, exist_ok=True)
    out = subprocess.run(["claude", "-p", "Reply with OK", "--model", args.model, "--effort", args.effort,
                          "--no-session-persistence", "--output-format", "json"],
                         cwd=probe, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
                         env={**os.environ, **AGENT_ENV})
    try:
        result = json.loads(out.stdout)
    except ValueError:
        raise SystemExit(f"preflight: no JSON from claude: {out.stdout[:300]} {out.stderr[:300]}")
    if result.get("is_error"):
        raise SystemExit(f"preflight failed: {result.get('result')}")
    models = sorted((result.get("modelUsage") or {}).keys())
    if not any(m.startswith(args.model) for m in models):
        raise SystemExit(f"preflight: asked for {args.model}, got {models}")
    log(f"Preflight: Claude Code answers, model {models}")


def run_claude(run_dir, staging, args):
    session = str(uuid.uuid4())
    cmd = ["claude", "-p", PROMPT, "--model", args.model, "--effort", args.effort,
           "--dangerously-skip-permissions", "--session-id", session,
           "--output-format", "stream-json", "--verbose"]
    log(f"Agent: {' '.join(cmd)}")
    log(f"  cwd {run_dir}, timeout {args.timeout} min")
    started, timed_out = time.monotonic(), False
    with open(os.path.join(staging, "stream.ndjson"), "w", encoding="utf-8") as out, \
            open(os.path.join(staging, "stderr.txt"), "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, cwd=run_dir, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                env={**os.environ, **AGENT_ENV})
        try:
            code = proc.wait(timeout=args.timeout * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
    elapsed = time.monotonic() - started
    log(f"  exited {code} after {elapsed / 60:.1f} min" + (" (TIMED OUT)" if timed_out else ""))
    return {"session_id": session, "exit_code": code, "timed_out": timed_out, "wall_seconds": round(elapsed, 1)}


def stream_result(staging):
    """The final 'result' event of the stream-json output (cost, duration, turns)."""
    result = None
    with open(os.path.join(staging, "stream.ndjson"), encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                result = event
    return result or {}


def find_transcript(session):
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session}.jsonl"))
    return hits[0] if hits else None


def transcript_facts(path, run_dir):
    """Model(s) used, first-call usage (harness prompt fingerprint), compactions, and
    answer-key markers in the agent's own tool calls (its own directory excluded)."""
    models, first_usage, markers, compactions = set(), None, {}, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and "compact" in str(event.get("subtype", "")):
                compactions += 1
            msg = event.get("message") if event.get("type") == "assistant" else None
            if not isinstance(msg, dict):
                continue
            if msg.get("model"):
                models.add(msg["model"])
            if first_usage is None and msg.get("usage"):
                first_usage = msg["usage"]
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    # The session's own folder (where Claude Code saves large tool output) doesn't count
                    text = (json.dumps(block.get("input")).replace(os.path.dirname(path), "<own session>")
                            .replace(run_dir, "<run dir>"))
                    for m in ANSWER_KEY_MARKERS:
                        if m in text:
                            markers[m] = markers.get(m, 0) + 1
    return {"models": sorted(models), "first_call_usage": first_usage, "answer_key_markers": markers,
            "compactions": compactions}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="One unattended agent run against the v2 lab.")
    parser.add_argument("run", type=int, help="Run number (0 for a try run)")
    parser.add_argument("--label", default="opus", help="Batch name: ~/claudenetops/<label>/run_NNN (default: opus)")
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout", type=int, default=60, help="Minutes before the agent is stopped (default: 60)")
    parser.add_argument("--root", default=os.path.expanduser("~/claudenetops"))
    args = parser.parse_args()

    name = f"run_{args.run:03d}"
    run_dir = os.path.join(args.root, args.label, name)
    staging = os.path.join(args.root, ".staging", args.label, name)
    repo_dir = os.path.join(REPO_RUNS, args.label, name)
    if os.path.exists(run_dir):
        raise SystemExit(f"{run_dir} already exists; pick another run number")
    os.makedirs(staging, exist_ok=True)

    meta = {"run": args.run, "label": args.label, "prompt": PROMPT, "model_requested": args.model,
            "effort": args.effort, "claude_code_version": claude_version(), "agent_env": AGENT_ENV,
            "started": now()}
    log(f"{name}: {meta['claude_code_version']}, {args.model}, effort {args.effort}")

    preflight(args)
    reset_lab()
    baseline = break_lab(staging)

    os.makedirs(run_dir)
    shutil.copy(CLAUDE_MD, os.path.join(run_dir, "CLAUDE.md"))
    meta["agent_started"] = now()
    meta.update(run_claude(run_dir, staging, args))
    meta["agent_ended"] = now()

    log("Read-back right after the agent...")
    first = readback(staging, "readback_end")

    log("Full verify...")
    vargs = argparse.Namespace(l2_interval=30, l2_timeout=180, no_retry=False)
    raw = verify_lab.collect({1, 2, 3}, vargs)
    results = {"timestamp": now(), "layers": [1, 2, 3], **verify_lab.analyze(raw, verify_lab.DEFAULT_CONFIGS)}
    results["comparison"] = verify_lab.compare(results, baseline)
    results["run"] = args.run
    save_json(os.path.join(staging, f"{name}.json"), results)
    save_json(os.path.join(staging, f"{name}.raw.json"), {"timestamp": results["timestamp"], "raw": raw})
    verify_lab.print_report(results)

    log("Read-back after the verify...")
    second = readback(staging, "readback_after_verify")
    drift = sorted(set(l3_items(first)) ^ set(l3_items(results)) | set(l3_items(results)) ^ set(l3_items(second)))
    meta["config_drift_after_agent"] = drift

    intent = layer3.load_intent(verify_lab.DEFAULT_CONFIGS)
    symptoms = score_run.load_symptoms(score_run.DEFAULT_SYMPTOMS)
    s = {"source": f"{name}.json", "graded_from": "raw", "timestamp": results["timestamp"],
         **score_run.score(results, intent, symptoms, baseline)}
    save_json(os.path.join(staging, f"{name}.score.json"), s)
    score_run.print_run(name, s)

    stream = stream_result(staging)
    meta["result"] = {k: stream.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                                 "duration_api_ms", "total_cost_usd", "usage", "modelUsage")}
    transcript = find_transcript(meta["session_id"])
    meta["transcript"] = transcript
    if transcript:
        meta.update(transcript_facts(transcript, run_dir))
        shutil.copy(transcript, os.path.join(staging, f"r{args.run:03d}.jsonl"))
    meta["score"] = {"fixed": s["fixed"], "verdicts": s["verdicts"], "workarounds": len(s["workarounds"]),
                     "collateral": len(s.get("collateral", [])), "unsaved": s["unsaved"],
                     "frr_out_of_sync": s["frr_out_of_sync"]}
    meta["ended"] = now()
    save_json(os.path.join(staging, "meta.json"), meta)

    # Results into the run directory (agent is done) and a copy into the repo
    shutil.copytree(staging, os.path.join(run_dir, "results"))
    shutil.copytree(staging, repo_dir, dirs_exist_ok=True)
    shutil.rmtree(staging)

    log("=" * fault_lab.WIDTH)
    log(f"{name} done: {s['fixed']}/12 fixed, cost ${meta['result'].get('total_cost_usd')}, "
        f"{meta['wall_seconds'] / 60:.1f} min, models {meta.get('models')}")
    if meta.get("answer_key_markers"):
        log(f"  CHECK: transcript mentions {meta['answer_key_markers']}")
    if drift:
        log(f"  CHECK: config changed between read-backs: {drift}")
    if meta["timed_out"] or meta["exit_code"]:
        log(f"  CHECK: exit {meta['exit_code']}, timed out {meta['timed_out']}")
    log(f"  results: {run_dir}/results and {os.path.relpath(repo_dir, verify_lab.PROJECT_ROOT)}")
    # A session that didn't end normally (usage limit, API error, timeout) isn't a valid run;
    # exit non-zero so a batch loop stops instead of starting the next one
    if meta["timed_out"] or meta["exit_code"] or meta["result"].get("is_error")             or meta["result"].get("subtype") != "success":
        log(f"  INVALID RUN: agent session ended with {meta['result'].get('subtype')}")
        sys.exit(2)


if __name__ == "__main__":
    main()
