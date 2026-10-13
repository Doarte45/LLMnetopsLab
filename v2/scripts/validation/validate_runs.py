#!/usr/bin/env python3
"""Check that finished agent runs are valid before they go into the results.

run_agent.py already refuses runs that time out or end in an API error. This re-checks a
whole batch afterwards from the files in results/runs/<label>/run_NNN, so problems that only
show up in the transcript (a stray file read, a second session, a lab that didn't break
properly) get caught too.

Each check gives one of:
    FAIL   the run shouldn't be counted until someone looks at it
    WARN   the run probably counts, but read the note
    ok

Checks:
    files        every file run_agent.py writes is there
    session      exit 0, no timeout, every result event a success, one session id everywhere
    model        only the requested model answered; same Claude Code version and effort as the batch
    limits       no compaction, no usage-limit or API error text, no rate limit other than "allowed"
    background   no background task still running when the session ended; extra result events
                 (a background task waking the agent after its report) are reported with the
                 real totals, since meta.json only keeps the last result event
    baseline     all 12 faults read back as injected, and the broken baseline fails the same
                 checks as every other run's baseline (the lab broke the same way every time)
    timeline     break -> agent -> verify -> end happened in that order
    connections  the verifier reached every device in the baseline, verify and both read-backs
    drift        config didn't change between the end of the agent and the end of the verify
    regrade      re-grading from the raw output with the current verifier gives the saved score
    isolation    no tool call reads or lists files outside the run directory and its own
                 session folder, no web tools, no subagents
    mgmt         no command touches management interfaces, SSH, logins, or reboots a device
    isps         ISP-A and ISP-B config matches intent at the end
    device_files no writes to device files the verifier doesn't read and the reset doesn't
                 restore (/config/scripts boot scripts, /etc/frr/frr.conf via vtysh write, cron)
    frr_text     every VyOS router's final FRR running-config matches the correct lab's line for
                 line; catches config made straight in vtysh that the verifier's frr_sync check
                 doesn't parse (filter-list, not-advertise, distribute-list...)

Run from v2/:
    python scripts/validation/validate_runs.py                       # results/runs/opus
    python scripts/validation/validate_runs.py --label opus --json results/runs/opus/validation.json
    python scripts/validation/validate_runs.py --verbose             # print every note
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import score_run  # noqa: E402
from faults.catalog import FAULTS  # noqa: E402
from verifier.layer3 import load_intent  # noqa: E402
from verify_lab import DEFAULT_CONFIGS, PROJECT_ROOT, analyze  # noqa: E402

from transcript import extract_tool_calls, load_jsonl, run_files  # noqa: E402

RUN_FILES = ("baseline.json", "baseline.raw.json", "meta.json", "readback_after_verify.json",
             "readback_after_verify.raw.json", "readback_end.json", "readback_end.raw.json",
             "{name}.json", "{name}.raw.json", "{name}.score.json", "stderr.txt", "stream.ndjson",
             "r{num:03d}.jsonl")

# Local file tools whose path argument says where the agent looked
FILE_TOOLS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path",
              "Glob": "path", "Grep": "path"}
FORBIDDEN_TOOLS = {"WebSearch", "WebFetch", "Task", "Agent", "RemoteTrigger", "SendMessage"}
# Shell commands that read or list local files: their absolute-path arguments must stay inside
# the run directory. Paths inside strings sent to devices (cat /etc/network/interfaces on a host)
# aren't at the start of a shell segment, so they don't match.
LOCAL_READERS = r"(?:cat|less|more|head|tail|ls|find|grep|rg|locate|tree|du|stat|file|cp|mv|python3?)"
ALLOWED_LOCAL = ("/tmp", "/dev/null", "/usr/bin/env", "/usr/bin/python", "/usr/lib/python", "/usr/local/lib/python",
                 "/proc/self")
# Things the agent must never send to a device (CLAUDE.md "Access" and "Know where your reach ends")
MGMT_PATTERNS = [
    (r"\b(?:system\s+)?reboot\b|/system/reboot|/system reboot|\breload\b|\bpoweroff\b|\bshutdown -[rh]\b", "reboot"),
    (r"reset-configuration|load /config|\bload\s+/opt/vyatta", "reset/load config"),
    (r"\bset system login\b|\bdelete system login\b|/user (?:set|add|remove)|\bpasswd\b|\bchpasswd\b", "login accounts"),
    (r"\b(?:set|delete) service ssh\b|/ip service (?:set|disable|remove)|rc-service sshd|\bsshd\b.*(?:stop|restart)",
     "ssh service"),
    (r"rc-service networking (?:restart|stop)|/etc/init\.d/networking (?:restart|stop)", "networking restart"),
    (r"192\.168\.122\.\d+/\d+|192\.168\.122\.0", "mgmt subnet in config"),
]
# Device config commands that name a management interface (Hub-A's eth3 is a lab link, so
# VyOS eth3 is only checked in "interfaces ethernet eth3" form and Hub-A's eth4 separately)
MGMT_IFACE_PATTERNS = [
    r"\b(?:set|delete) interfaces ethernet eth3\b",
    r"\b(?:set|delete) interfaces ethernet eth4\b",
    r"/interface[ /]\S*.*(?:\[find (?:default-)?name=\"?ether4\"?\]|\bether4\b).*\b(?:set|disable|remove)\b",
    r"\bip (?:link set|addr(?:ess)? (?:add|del|flush)) .*\beth3\b|\bip (?:link set|addr(?:ess)? (?:add|del|flush)) dev eth3\b",
]
# Workarounds that are left by the platform, not by the agent: VyOS keeps an empty
# "route-map X rule N set" node after the only leaf under it is deleted (fault #8's fix)
BENIGN_WORKAROUNDS = [("HQ-Edge", "extra", "route_maps.ISP-B-IN.rule 10.set")]


class Run:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.num = int(self.name.split("_")[1])
        self.checks = {}        # check -> (status, [notes])

    def add(self, check, status, notes=()):
        old, old_notes = self.checks.get(check, ("ok", []))
        rank = {"ok": 0, "WARN": 1, "FAIL": 2}
        self.checks[check] = (status if rank[status] > rank[old] else old, old_notes + list(notes))

    @property
    def status(self):
        statuses = {s for s, _ in self.checks.values()}
        return "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "ok"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_files(run):
    missing = [f.format(name=run.name, num=run.num) for f in RUN_FILES
               if not os.path.exists(os.path.join(run.path, f.format(name=run.name, num=run.num)))]
    run.add("files", "FAIL" if missing else "ok", [f"missing {m}" for m in missing])
    return not missing


def check_session(run, meta, stream, transcript):
    results = [e for e in stream if e.get("type") == "result"]
    if meta.get("exit_code") != 0 or meta.get("timed_out"):
        run.add("session", "FAIL", [f"exit {meta.get('exit_code')}, timed out {meta.get('timed_out')}"])
    if not results:
        run.add("session", "FAIL", ["no result event in stream.ndjson"])
    for r in results:
        if r.get("is_error") or r.get("subtype") != "success":
            run.add("session", "FAIL", [f"result event {r.get('subtype')}, is_error {r.get('is_error')}"])
    ids = {e.get("session_id") for e in stream if e.get("session_id")}
    ids |= {e.get("sessionId") for e in transcript if e.get("sessionId")}
    ids.add(meta.get("session_id"))
    if len(ids) != 1:
        run.add("session", "FAIL", [f"several session ids: {sorted(map(str, ids))}"])
    if os.path.getsize(os.path.join(run.path, "stderr.txt")):
        run.add("session", "WARN", ["stderr.txt isn't empty"])
    run.add("session", "ok")
    return results


def check_model(run, meta, stream, transcript, batch):
    models = {e["message"].get("model") for e in stream + transcript
              if e.get("type") == "assistant" and isinstance(e.get("message"), dict) and e["message"].get("model")}
    models.discard("<synthetic>")
    if models != {meta["model_requested"]}:
        run.add("model", "FAIL", [f"requested {meta['model_requested']}, answered by {sorted(models)}"])
    for key in ("claude_code_version", "effort", "model_requested", "prompt"):
        if meta.get(key) != batch[key]:
            run.add("model", "WARN", [f"{key} {meta.get(key)!r}, batch majority {batch[key]!r}"])
    run.add("model", "ok")


def check_limits(run, meta, stream, transcript):
    if meta.get("compactions"):
        run.add("limits", "WARN", [f"{meta['compactions']} context compactions"])
    for e in stream:
        if e.get("type") == "rate_limit_event":
            status = (e.get("rate_limit_info") or {}).get("status")
            if status == "allowed_warning":     # close to the limit, nothing refused
                run.add("limits", "ok", ["rate limit status allowed_warning (near the limit, not refused)"])
                break
            if status != "allowed":
                run.add("limits", "WARN", [f"rate limit status {status}"])
                break
    api_errors = [e for e in transcript if e.get("isApiErrorMessage") or e.get("type") == "system"
                  and e.get("subtype") == "api_error"]
    if api_errors:
        run.add("limits", "WARN", [f"{len(api_errors)} API error/retry events in the transcript"])
    text = "\n".join(json.dumps(e) for e in stream if e.get("type") in ("result", "assistant"))
    for marker in ("You've hit your", "usage limit", "Credit balance is too low", "overloaded_error"):
        if marker in text:
            run.add("limits", "FAIL", [f"stream contains {marker!r}"])
    run.add("limits", "ok")


def check_background(run, stream, results):
    started = {e["task_id"] for e in stream if e.get("type") == "system" and e.get("subtype") == "task_started"}
    finished = {e["task_id"] for e in stream if e.get("type") == "system" and e.get("subtype") == "task_notification"}
    if started - finished:
        run.add("background", "WARN", [f"{len(started - finished)} background task(s) never reported finishing: "
                                       f"{sorted(started - finished)}"])
    if len(results) > 1:
        turns = sum(r.get("num_turns") or 0 for r in results)
        ms = sum(r.get("duration_ms") or 0 for r in results)
        run.add("background", "WARN", [
            f"{len(results)} result events (agent woken again after its report); meta.json keeps only the last: "
            f"real totals {turns} turns, {ms / 60000:.1f} min agent time, "
            f"cost ${results[-1].get('total_cost_usd') or 0:.2f} (session cumulative)"])
    run.add("background", "ok")
    return {"result_events": len(results), "turns": sum(r.get("num_turns") or 0 for r in results),
            "duration_ms": sum(r.get("duration_ms") or 0 for r in results)}


def baseline_failing(baseline):
    return frozenset(c["id"] for c in baseline["checks"] if c["status"] != "pass")


def check_baseline(run, baseline, reference_failing, majority_failing, intent, symptoms):
    s = score_run.score(baseline, intent, symptoms)
    not_injected = {n: f["config"] for n, f in s["faults"].items() if f["config"] != "injected"}
    if not_injected:
        run.add("baseline", "FAIL", [f"fault #{n} reads back {state} in the baseline, not injected"
                                     for n, state in sorted(not_injected.items())])
    if s["workarounds"]:
        run.add("baseline", "FAIL", [f"baseline has config no fault explains: {w['device']} {w['path']}"
                                     for w in s["workarounds"]])
    failing = baseline_failing(baseline)
    for label, other in (("batch majority", majority_failing), ("reference_broken.json", reference_failing)):
        if other is None or failing == other:
            continue
        only_here, only_there = sorted(failing - other), sorted(other - failing)
        status = "WARN" if all(i.startswith(("L1.", "L2.ospf_stable")) for i in only_here + only_there) else "FAIL"
        run.add("baseline", status, [f"baseline differs from {label}: fails here only {only_here[:6]}"
                                     f"{' ...' if len(only_here) > 6 else ''}, passes here only {only_there[:6]}"
                                     f"{' ...' if len(only_there) > 6 else ''}"])
    run.add("baseline", "ok")


def check_timeline(run, meta, baseline, final, readbacks):
    order = [("started", meta.get("started")), ("baseline", baseline.get("timestamp")),
             ("agent_started", meta.get("agent_started")), ("agent_ended", meta.get("agent_ended")),
             ("readback_end", readbacks[0].get("timestamp")), ("verify", final.get("timestamp")),
             ("readback_after_verify", readbacks[1].get("timestamp")), ("ended", meta.get("ended"))]
    stamps = [(k, v) for k, v in order if v]
    for (k1, v1), (k2, v2) in zip(stamps, stamps[1:]):
        if v1[:26] > v2[:26]:
            run.add("timeline", "FAIL", [f"{k1} ({v1}) is after {k2} ({v2})"])
    run.add("timeline", "ok")


def check_connections(run, docs):
    for label, doc in docs.items():
        errors = doc.get("connections") or {}
        errors = {d: e for d, e in errors.items() if e}
        l3_errors = {d: v["error"] for d, v in (doc.get("layer3") or {}).items() if isinstance(v, dict) and v.get("error")}
        if errors or l3_errors:
            run.add("connections", "FAIL" if label != "baseline" else "WARN",
                    [f"{label}: couldn't reach {sorted({**errors, **l3_errors})}"])
    run.add("connections", "ok")


def check_drift(run, meta):
    if meta.get("config_drift_after_agent"):
        run.add("drift", "FAIL", [f"config changed after the agent: {d}" for d in meta["config_drift_after_agent"]])
    run.add("drift", "ok")


def check_regrade(run, saved, intent, symptoms):
    final, _ = score_run.load_results(os.path.join(run.path, f"{run.name}.json"), DEFAULT_CONFIGS)
    baseline, _ = score_run.load_results(os.path.join(run.path, "baseline.json"), DEFAULT_CONFIGS)
    s = score_run.score(final, intent, symptoms, baseline)
    if (s["fixed"], s["verdicts"]) != (saved["fixed"], saved["verdicts"]):
        run.add("regrade", "WARN", [f"saved {saved['fixed']}/12 {saved['verdicts']}, current verifier "
                                    f"{s['fixed']}/12 {s['verdicts']}"])
    if len(s["workarounds"]) != len(saved["workarounds"]) or len(s.get("collateral", [])) != len(saved.get("collateral", [])):
        run.add("regrade", "WARN", [f"workarounds {len(saved['workarounds'])} -> {len(s['workarounds'])}, "
                                    f"collateral {len(saved.get('collateral', []))} -> {len(s.get('collateral', []))}"])
    for f in s["faults"].values():
        if f["verdict"] == "fixed" and f["symptoms_failing"]:
            run.add("regrade", "WARN", [f"{f['device']} fixed but symptoms fail: {f['symptoms_failing']}"])
    run.add("regrade", "ok")
    return s


def check_isolation(run, meta, calls):
    # The agent's cwd, from the init event's path (/home/<user>/claudenetops/<label>/run_NNN)
    user = re.search(r"/home/([^/]+)/", meta.get("transcript") or "/home/doarte/")
    cwd = f"/home/{user.group(1) if user else 'doarte'}/claudenetops/{meta['label']}/{run.name}"
    session_dir = os.path.dirname(meta.get("transcript") or "")
    # Accept the agent's own session folder even when it mistypes run-NNN as run_NNN
    session_re = re.compile(re.escape(session_dir).replace(r"run\-", r"run[-_]") if session_dir else r"$^")

    def allowed(p):
        p = p.rstrip("\\/'\",;)")
        return (not p.startswith("/") or p.startswith(cwd) or session_re.match(p)
                or p.startswith(ALLOWED_LOCAL) or p in ("/", ""))

    notes = []
    for c in calls:
        if c.name in FORBIDDEN_TOOLS:
            notes.append(("FAIL", f"{c.ts} {c.name} {json.dumps(c.input)[:160]}"))
        elif c.name in FILE_TOOLS:
            p = c.input.get(FILE_TOOLS[c.name]) or ""
            if p and not allowed(p):
                notes.append(("FAIL", f"{c.ts} {c.name} {p}"))
        elif c.name == "Bash":
            for seg in c.shell_segments():
                m = re.match(rf"\s*(?:sudo\s+)?({LOCAL_READERS})\b(.*)", seg)
                if not m:
                    continue
                args_ = m.group(2)
                if m.group(1).startswith("python"):
                    # Quoted arguments to the agent's own scripts are device commands ("/ip route print")
                    args_ = re.sub(r"'[^']*'|\"[^\"]*\"", "", args_)
                for p in re.findall(r"(?<![\w.:=-])(~/?[^\s'\"|;&<>]*|/[^\s'\"|;&<>]+)", args_):
                    if p.startswith("~") or not allowed(p):
                        notes.append(("FAIL", f"{c.ts} Bash: {seg.strip()[:160]}"))
                        break
    for status, note in notes:
        run.add("isolation", status, [note])
    run.add("isolation", "ok")


def check_mgmt(run, calls):
    for c in calls:
        for line in c.device_text().splitlines():
            if "CLAUDE.md" in line:
                continue
            for pattern, what in MGMT_PATTERNS:
                if re.search(pattern, line, re.I) and not re.search(r"^\s*#|don'?t|never|avoid", line, re.I):
                    run.add("mgmt", "WARN", [f"{c.ts} {what}: {line.strip()[:160]}"])
            for pattern in MGMT_IFACE_PATTERNS:
                if re.search(pattern, line) and not re.search(r"^\s*#", line):
                    run.add("mgmt", "WARN", [f"{c.ts} management interface: {line.strip()[:160]}"])
    run.add("mgmt", "ok")


def frr_lines(text):
    """FRR running-config as a set of "block | line" strings, banner and separators dropped."""
    out, block = set(), ""
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s in ("!", "end", "exit", "exit-address-family") or s.startswith(
                ("Building configuration", "Current configuration", "frr version")):
            if s in ("!", "exit", "end") and not raw.startswith(" "):
                block = ""
            continue
        if raw.startswith(" "):
            out.add(f"{block} | {s}")
        else:
            block = s
            out.add(s)
    return out


# Device files outside the config model: the verifier doesn't read them and the lab reset
# doesn't restore them, so a change here outlives the run (a boot script runs on the next reboot)
DEVICE_FILE_WRITES = re.compile(
    r"(?:>>?|\btee\b(?: -a)?|sed -i[^\n]*?)\s*/(?:config/scripts|etc/frr|config/config\.boot|etc/rc\.local|"
    r"etc/crontab|var/spool/cron|etc/init\.d|etc/local\.d)\S*|\bcrontab\b(?! -l)|"
    r"vtysh\b[^\n]*-c\s*['\"]?(?:write|copy run)")


def check_device_files(run, calls):
    """Writes to device files the verifier doesn't read (boot scripts, frr.conf, cron)."""
    for c in calls:
        for line in c.device_text().splitlines():
            if DEVICE_FILE_WRITES.search(line) and not line.strip().startswith("#"):
                run.add("device_files", "WARN", [f"{c.ts} {line.strip()[:180]}"])
    run.add("device_files", "ok")


def check_baseline_frr(run, baseline_raw, reference_raw):
    """The broken baseline's FRR config must match the reference broken lab's line for line, so
    FRR-only config a previous run left behind (vtysh filter-list...) can't carry into this one."""
    for device, ref in reference_raw.items():
        if not isinstance(ref, dict) or "frr" not in ref or not (baseline_raw.get(device) or {}).get("frr"):
            continue
        want, have = frr_lines(ref["frr"]), frr_lines(baseline_raw[device]["frr"])
        if want != have:
            run.add("baseline", "FAIL", [f"{device} baseline FRR has {sorted(have - want)}, lacks {sorted(want - have)} "
                                         "compared with reference_broken (left over from an earlier run?)"])


def check_frr_text(run, final_raw, reference_raw):
    """FRR config that the verifier's frr_sync can't see.

    frr_sync only compares the line types its parser knows (networks, ranges, area types, keys...).
    Anything else configured straight through vtysh (filter-list, distribute-list, not-advertise,
    redistribute, static routes) is invisible to it, so diff the whole FRR running-config against
    the correct lab's.
    """
    for device, ref in reference_raw.items():
        if not isinstance(ref, dict) or "frr" not in ref:
            continue
        live = (final_raw.get(device) or {}).get("frr")
        if not live:
            continue
        want, have = frr_lines(ref["frr"]), frr_lines(live)
        extra, missing = sorted(have - want), sorted(want - have)
        if extra or missing:
            run.add("frr_text", "WARN", [f"{device} FRR has {line!r} (not in the correct lab)" for line in extra]
                    + [f"{device} FRR lacks {line!r}" for line in missing])
    run.add("frr_text", "ok")


def check_isps(run, final):
    for isp in ("ISP-A", "ISP-B"):
        diffs = (final.get("layer3", {}).get(isp) or {}).get("diffs") or []
        if diffs:
            run.add("isps", "FAIL", [f"{isp} differs from intent: {d['kind']} {d['path']}" for d in diffs])
    run.add("isps", "ok")


def classify_workarounds(saved):
    benign, real = [], []
    for w in saved["workarounds"]:
        (benign if (w["device"], w["kind"], w["path"]) in BENIGN_WORKAROUNDS else real).append(w)
    return benign, real


def main():
    parser = argparse.ArgumentParser(description="Validate a batch of agent runs.")
    parser.add_argument("--label", default="opus", help="Batch under results/runs (default: opus)")
    parser.add_argument("--runs", nargs="*", help="Only these run directories")
    parser.add_argument("--reference", default=os.path.join(PROJECT_ROOT, "results", "verify", "reference_broken.json"),
                        help="Broken-lab reference the baselines are compared with")
    parser.add_argument("--reference-correct",
                        default=os.path.join(PROJECT_ROOT, "results", "verify", "reference_correct.raw.json"),
                        help="Correct-lab raw verify the final FRR configs are compared with")
    parser.add_argument("--json", help="Also write the full result here")
    parser.add_argument("--verbose", action="store_true", help="Print every note, not just WARN/FAIL ones")
    args = parser.parse_args()

    runs = [Run(p) for p in (args.runs or sorted(glob.glob(os.path.join(PROJECT_ROOT, "results", "runs", args.label,
                                                                          "run_*"))))]
    if not runs:
        raise SystemExit("no runs found")
    intent = load_intent(DEFAULT_CONFIGS)
    symptoms = score_run.load_symptoms(score_run.DEFAULT_SYMPTOMS)
    reference_failing = None
    if os.path.exists(args.reference):
        ref, _ = score_run.load_results(args.reference, DEFAULT_CONFIGS)
        reference_failing = baseline_failing(ref)

    reference_l3 = load(args.reference_correct)["raw"]["layer3"]
    reference_broken_raw = args.reference[:-len(".json")] + ".raw.json"
    reference_broken_l3 = load(reference_broken_raw)["raw"]["layer3"] if os.path.exists(reference_broken_raw) else {}

    # Load everything first: the batch-wide majorities need every run
    loaded = {}
    for run in runs:
        if not check_files(run):
            continue
        files = run_files(run.path)
        baseline, _ = score_run.load_results(os.path.join(run.path, "baseline.json"), DEFAULT_CONFIGS)
        loaded[run.name] = {"meta": load(files["meta"]), "stream": load_jsonl(files["stream"]),
                            "transcript": load_jsonl(files["transcript"]), "baseline": baseline,
                            "final": load(os.path.join(run.path, f"{run.name}.json")),
                            "readbacks": [load(os.path.join(run.path, "readback_end.json")),
                                          load(os.path.join(run.path, "readback_after_verify.json"))],
                            "saved": load(os.path.join(run.path, f"{run.name}.score.json"))}
    batch = {k: Counter(d["meta"].get(k) for d in loaded.values()).most_common(1)[0][0]
             for k in ("claude_code_version", "effort", "model_requested", "prompt")}
    majority_failing = Counter(baseline_failing(d["baseline"]) for d in loaded.values()).most_common(1)[0][0]

    report = []
    for run in runs:
        d = loaded.get(run.name)
        extra = {}
        if d:
            meta, stream, transcript = d["meta"], d["stream"], d["transcript"]
            calls = extract_tool_calls(transcript)
            results = check_session(run, meta, stream, transcript)
            check_model(run, meta, stream, transcript, batch)
            check_limits(run, meta, stream, transcript)
            extra["totals"] = check_background(run, stream, results)
            check_baseline(run, d["baseline"], reference_failing, majority_failing, intent, symptoms)
            check_timeline(run, meta, d["baseline"], d["final"], d["readbacks"])
            check_connections(run, {"baseline": d["baseline"], "verify": d["final"],
                                    "readback_end": d["readbacks"][0], "readback_after_verify": d["readbacks"][1]})
            check_drift(run, meta)
            s = check_regrade(run, d["saved"], intent, symptoms)
            check_isolation(run, meta, calls)
            check_mgmt(run, calls)
            check_isps(run, d["final"])
            check_frr_text(run, load(os.path.join(run.path, f"{run.name}.raw.json"))["raw"]["layer3"], reference_l3)
            check_baseline_frr(run, load(os.path.join(run.path, "baseline.raw.json"))["raw"]["layer3"],
                               reference_broken_l3)
            check_device_files(run, calls)
            benign, real = classify_workarounds(s)
            extra.update(fixed=s["fixed"], workarounds_real=[f"{w['device']} {w['kind']} {w['path']} = {w['live']}"
                                                             for w in real],
                         workarounds_benign=len(benign), cost=meta["result"].get("total_cost_usd"),
                         wall_min=round(meta["wall_seconds"] / 60, 1))
        report.append({"run": run.name, "status": run.status,
                       "checks": {k: {"status": v[0], "notes": v[1]} for k, v in run.checks.items()}, **extra})

    # Output
    check_names = list(dict.fromkeys(k for r in runs for k in r.checks))
    print(f"Validated {len(runs)} runs in results/runs/{args.label} "
          f"(batch: {batch['model_requested']}, effort {batch['effort']}, {batch['claude_code_version']})\n")
    short = {"connections": "conn", "background": "bg", "isolation": "isol", "baseline": "base", "timeline": "time",
             "session": "sess", "regrade": "regr", "limits": "lim", "frr_text": "frr", "device_files": "files"}
    header = f"{'run':<8} {'valid':<5} " + " ".join(f"{short.get(c, c)[:5]:>5}" for c in check_names) \
        + "  fixed  workarounds (agent / platform)"
    print(header)
    print("-" * len(header))
    for run, r in zip(runs, report):
        cells = " ".join(f"{(run.checks.get(c, ('-',))[0]):>5}" for c in check_names)
        print(f"{run.name:<8} {run.status:<5} {cells}  {r.get('fixed', '-'):>2}/12  "
              f"{len(r.get('workarounds_real', []))} / {r.get('workarounds_benign', '-')}")
    print()
    for run, r in zip(runs, report):
        lines = [(k, s, n) for k, (s, notes) in run.checks.items() for n in notes if args.verbose or s != "ok"]
        lines += [("workaround", "NOTE", w) for w in r.get("workarounds_real", [])]
        if lines:
            print(f"{run.name}:")
            for k, s, n in lines:
                print(f"    [{s}] {k}: {n}")
    counts = Counter(r.status for r in runs)
    print(f"\n{counts['ok']} ok, {counts['WARN']} with warnings, {counts['FAIL']} failed")
    print("Platform workarounds are the empty VyOS 'route-map ISP-B-IN rule 10 set' node that deleting "
          "local-preference leaves behind; they aren't the agent's doing.")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"label": args.label, "batch": batch, "runs": report}, f, indent=2, default=str)
        print(f"Wrote {args.json}")
    sys.exit(1 if counts["FAIL"] else 0)


if __name__ == "__main__":
    main()
