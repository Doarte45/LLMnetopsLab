#!/usr/bin/env python3
"""How the agents went about each run: fix order, timing, rabbit holes and workaround attempts.

The scorer only sees the end state. This reads the transcripts to see the path there,
including workarounds the agent tried and later removed, which the end state can't show.

Per run:
    collect     minutes from agent start to the first config change
    fix order   when each fault's fix was first sent (matched on the fixing command)
    attempts    workaround-style commands sent at any point (see ATTEMPTS)
    area 20     time spent on the Hub-B summarization quirk: in the correct lab FRR still sends
                the area 20 /24s into area 10 and 10.1.0.0/16 into area 20 (verifier/design.py
                "Baseline quirk"), which the design says shouldn't happen
    left over   workarounds still in the config at the end (from the run's score file)
    report      what the agent's report says about anything it left unresolved

Matching is on command text, so it's approximate: a command counts as sent when it appears in a
Bash call, or in a written script that a later Bash call runs.

Run from v2/:
    python scripts/validation/analyze_runs.py                    # results/runs/opus, summary tables
    python scripts/validation/analyze_runs.py --run run_013      # one run's timeline in detail
    python scripts/validation/analyze_runs.py --json results/runs/opus/analysis.json
"""

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from faults.catalog import FAULTS  # noqa: E402
from verify_lab import PROJECT_ROOT  # noqa: E402

from transcript import assistant_texts, extract_tool_calls, final_report, load_jsonl, run_files  # noqa: E402

# The command that fixes each fault (first time it's sent = when the fault was fixed)
FIX_SIGNATURES = {
    1: r"(?:set|delete) protocols ospf area 10 virtual-link '?10\.255\.0\.2",
    2: r"/ip firewall filter remove",
    3: r"mtu=1500",
    4: r"set protocols ospf interface eth0 authentication md5 key-id 1 md5-key '?LabArea0Key",
    5: r"/routing ospf area set .*type=stub",
    6: r"set protocols ospf area 10 range '?10\.1\.0\.0/16",
    7: r"/ip firewall filter (?:set|add) .*connection-state=established,related",
    8: r"(?:set|delete) policy route-map ISP-[AB]-IN rule 10 set local-preference",
    9: r"delete protocols ospf default-information\b",
    10: r"set nat source rule 110 outbound-interface (?:name )?'?eth2",
    11: r"interface-template set .*\bpassive",
    12: r"10\.1\.1\.11/24",
}
# Workaround-style commands. Only lines that change config count, not show commands. BGP resets
# aren't listed: every run that sent one did it right after fault #8's local-preference fix, to apply it.
ATTEMPTS = {
    "vtysh config (bypasses VyOS)": r"vtysh\b.*(?:configure|conf t|-c ['\"]?(?:router ospf|area \S+ (?:filter|range)|no area|ip prefix-list))",
    "prefix-list / filter-list": r"(?:set|delete) policy prefix-list|area \S+ filter-list|\bfilter-list (?:in|out)\b",
    "range not-advertise": r"range \S+ not-advertise|not-advertise",
    "area 20 range/network flap": r"(?:set|delete) protocols ospf area 20 (?:range|network)",
    "OSPF/FRR process restart": r"restart (?:frr|ospf)|systemctl restart|clear ip ospf process|reset ip ospf|"
                                r"/routing ospf instance (?:disable|enable)",
    "static route": r"(?:set|delete) protocols static|/ip route add\b|\bip route (?:add|replace) (?!default)",
    "mtu-ignore / MTU elsewhere": r"mtu-ignore|set interfaces ethernet eth\d mtu",
    "firewall rule added": r"/ip firewall filter add",
    "redistribute": r"set protocols ospf redistribute",
    "area type change on Hub-B": r"set protocols ospf area 20 area-type (?!stub no-summary)",
}
# Branch-4's intended filter rules (configs/Mikrotik/Branch-4.rsc). Re-adding these is the #7 fix (delete the
# all-states rule, add it back with connection-state) or rebuilding the rule set, not a workaround.
INTENDED_FW_RULE = re.compile(r"(?=.*action=accept)(?=.*connection-state=established,related)|"
                              r"(?=.*action=drop)(?=.*src-address=10\.2\.2\.0/24)(?=.*dst-address=10\.0\.0\.0/8)|"
                              r"(?=.*action=drop)(?=.*src-address=10\.0\.0\.0/8)(?=.*dst-address=10\.2\.2\.0/24)")
CONFIG_LINE = re.compile(r"\b(?:set|delete|add|remove|disable|enable|configure|conf t|restart|clear|reset|no)\b|"
                         r"\bip (?:addr|route|link)\b|sed -i")
SHOW_LINE = re.compile(r"\b(?:show|print|export|get|cat|grep)\b")
# What a message about the area 20 quirk says: the area 20 /24s showing up in area 10, or
# 10.1.0.0/16 in totally stubby area 20, and the range/filter attempts at it. "leak" alone isn't
# enough: fault #6 "leaks" area 10 subnets and the guest firewall "leaks" (the ping check bug).
AREA20_TOPIC = re.compile(
    r"10\.2\.(?:[12]|x)(?:\.\w+)?/24|individual (?:\S+ ){0,3}(?:area 20|/24s? from area 20|10\.2)|"
    r"(?:/24s?|LSAs?|routes|subnets) from area 20|area 20 (?:\S+ ){0,2}(?:/24|LSAs?|routes|subnets)|"
    r"type.?3 LSAs? (?:\S+ ){0,6}(?:area 10|area 20)|component|not-advertise|filter-list|"
    r"range (?:\S+ ){0,6}(?:suppress|isn't|is not|doesn't|does not|not working)|suppress|"
    r"10\.1\.0\.0/16 (?:\S+ ){0,10}(?:leak|despite|shouldn't|should not|no-summary)|"
    r"(?:leak|despite|shouldn't|should not|no-summary) (?:\S+ ){0,10}10\.1\.0\.0/16", re.I)
# A message about the firewall only counts when it also names something area 20 specific
FIREWALL_TOPIC = re.compile(r"guest|firewall|isolation|packet loss|ping|conntrack|connection.track", re.I)
AREA20_SPECIFIC = re.compile(r"10\.2\.(?:[12]|x)(?:\.\w+)?/24|type.?3|range|area 20|10\.1\.0\.0/16|LSA", re.I)


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def executed_calls(calls):
    """[(time it ran, call, text)] for everything that could have sent commands: Bash calls, and
    scripts written with Write/Edit when a later Bash call runs them (the text as it was then)."""
    out, files = [], {}
    for c in calls:
        if c.name in ("Write", "Edit") and c.input.get("file_path"):
            path = c.input["file_path"]
            if c.name == "Write":
                files[path] = c.input.get("content", "")
            elif path in files:
                files[path] = files[path].replace(c.input.get("old_string", ""), c.input.get("new_string", ""),
                                                  -1 if c.input.get("replace_all") else 1)
        elif c.name == "Bash":
            text = c.input.get("command", "")
            for path, content in files.items():
                if os.path.basename(path) in text and re.search(r"python3?\b[^\n]*" + re.escape(os.path.basename(path)),
                                                                text):
                    text += "\n" + content
            out.append((parse_ts(c.ts), c, text))
    return out


def config_lines(text):
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", '"""', "'''")):
            continue
        if CONFIG_LINE.search(s) and not (SHOW_LINE.search(s) and not re.search(r"\b(?:set|delete|add|remove)\b", s)):
            yield s


def analyze(run_dir):
    name = os.path.basename(run_dir)
    files = run_files(run_dir)
    meta = json.load(open(files["meta"], encoding="utf-8"))
    stream, transcript = load_jsonl(files["stream"]), load_jsonl(files["transcript"])
    score = json.load(open(os.path.join(run_dir, f"{name}.score.json"), encoding="utf-8"))
    calls = extract_tool_calls(transcript)
    start = parse_ts(meta["agent_started"])

    def mins(t):
        return round((t - start).total_seconds() / 60, 1) if t else None

    fixes, attempts, first_change = {}, defaultdict(list), None
    for t, c, text in executed_calls(calls):
        for line in config_lines(text):
            for num, sig in FIX_SIGNATURES.items():
                if num not in fixes and re.search(sig, line):
                    fixes[num] = mins(t)
            for label, pat in ATTEMPTS.items():
                if label == "firewall rule added" and INTENDED_FW_RULE.search(line):
                    continue
                if re.search(pat, line, re.I):
                    attempts[label].append((mins(t), line[:140]))
        if fixes and first_change is None:
            first_change = min(fixes.values())
    attempts = {k: v for k, v in attempts.items() if k != "firewall rule added" or any("ospf" not in l for _, l in v)}

    # Time on the area 20 summarization quirk: first to last assistant message about it, up to the
    # final report. The report describes faults #5 and #6 in the same terms, and a background task
    # can wake the agent after it, so counting either would stretch the span to the end of the run.
    report = final_report(stream)
    texts = assistant_texts(transcript)
    report_at = next((parse_ts(ts) for ts, text in texts if len(text.strip()) > 200 and text.strip() in report), None)
    topic = [mins(parse_ts(ts)) for ts, text in texts
             if (report_at is None or parse_ts(ts) < report_at) and AREA20_TOPIC.search(text)
             and not (FIREWALL_TOPIC.search(text) and not AREA20_SPECIFIC.search(text))]
    unresolved = ""
    m = re.search(r"#+\s*(?:\d\.\s*)?(?:Unresolved|Uncertain)[^\n]*\n(.*?)(?=\n#+\s|\Z)", report, re.S | re.I)
    if m:
        unresolved = m.group(1).strip()

    results = [e for e in stream if e.get("type") == "result"]
    benign = ("HQ-Edge", "route_maps.ISP-B-IN.rule 10.set")
    left_over = sorted({f"{w['device']} {w['path'].split('[')[0]}"
                        + (f"[{w['path'].split('[')[1].split(' rule')[0]}" if "prefix-list" in w["path"] else "")
                        for w in score["workarounds"] if (w["device"], w["path"]) != benign})
    tools = Counter(c.name for c in calls)
    return {
        "run": name,
        "minutes": round(meta["wall_seconds"] / 60, 1),
        "cost": round(meta["result"].get("total_cost_usd") or 0, 2),
        "turns": sum(r.get("num_turns") or 0 for r in results),
        "tool_calls": dict(tools),
        "scripts_written": len({c.input.get("file_path") for c in calls if c.name == "Write"}
                               | set(re.findall(r"cat\s*<<\s*'?\w+'?\s*>\s*(\S+\.py)",
                                                "\n".join(c.input.get("command", "") for c in calls if c.name == "Bash")))),
        "first_fix_min": first_change,
        "fix_minutes": dict(sorted(fixes.items())),
        "all_fixed_by_min": max(fixes.values()) if len(fixes) == len(FAULTS) else None,
        "missing_fix_signatures": sorted(set(FIX_SIGNATURES) - set(fixes)),
        "attempts": attempts,
        "area20_minutes": (round(topic[-1] - topic[0], 1) if len(topic) > 1 else 0) if topic else 0,
        "area20_first_min": topic[0] if topic else None,
        "left_over": left_over,
        "fixed": score["fixed"],
        "unresolved": unresolved,
        "report_mentions_quirk": bool(re.search(r"FRR|platform (?:quirk|limitation|behavio)", unresolved)),
    }


def print_summary(rows):
    print(f"{'run':<8} {'min':>5} {'cost':>6} {'turns':>5} {'1st fix':>7} {'all 12':>6} {'area20':>6}  attempts / left over")
    print("-" * 110)
    for r in rows:
        tags = []
        short = {"vtysh config (bypasses VyOS)": "vtysh", "prefix-list / filter-list": "filter-list",
                 "range not-advertise": "not-adv", "area 20 range/network flap": "a20-flap",
                 "OSPF/FRR process restart": "restart", "static route": "STATIC",
                 "mtu-ignore / MTU elsewhere": "MTU", "firewall rule added": "fw-add", "redistribute": "REDIST",
                 "area type change on Hub-B": "area-type"}
        tags = [f"{short[k]}x{len(v)}" for k, v in r["attempts"].items()]
        left = f"  LEFT: {', '.join(r['left_over'])}" if r["left_over"] else ""
        print(f"{r['run']:<8} {r['minutes']:>5} {r['cost']:>6} {r['turns']:>5} {r['first_fix_min'] or '-':>7} "
              f"{r['all_fixed_by_min'] or '-':>6} {r['area20_minutes']:>6}  {' '.join(tags)}{left}")

    n = len(rows)
    print(f"\nFix order (median minute after agent start each fault's fix was first sent, runs where matched):")
    for f in FAULTS:
        ts = sorted(r["fix_minutes"][f.num] for r in rows if f.num in r["fix_minutes"])
        med = ts[len(ts) // 2] if ts else None
        print(f"    #{f.num:<2} {f.device:<9} {med if med is not None else '-':>5} min  ({len(ts)}/{n})  {f.title[:70]}")
    print("\nWorkaround-style attempts (runs that tried it at least once):")
    tried = Counter(k for r in rows for k in r["attempts"])
    for k, c in tried.most_common():
        print(f"    {c:>2}/{n}  {k}")
    print(f"\nArea 20 summarization quirk: {sum(1 for r in rows if r['area20_minutes'] > 1)}/{n} runs spent time on it "
          f"(median {sorted(r['area20_minutes'] for r in rows)[n // 2]} min); "
          f"{sum(1 for r in rows if r['report_mentions_quirk'])}/{n} reports blame FRR/the platform for something left unresolved")
    print(f"Left over at the end (beyond the platform's empty route-map node): "
          f"{sum(1 for r in rows if r['left_over'])}/{n} runs")


def print_run(r):
    print(f"{r['run']}: {r['fixed']}/12 fixed, {r['minutes']} min, ${r['cost']}, {r['turns']} turns, "
          f"{r['scripts_written']} scripts, tools {r['tool_calls']}")
    print(f"  first fix at {r['first_fix_min']} min, all 12 fixes sent by {r['all_fixed_by_min']} min")
    for num, t in sorted(r["fix_minutes"].items(), key=lambda kv: kv[1]):
        print(f"    {t:>5} min  #{num}")
    if r["missing_fix_signatures"]:
        print(f"  fix command not recognised for #{r['missing_fix_signatures']} (fixed another way)")
    for k, v in r["attempts"].items():
        print(f"  {k}: {len(v)}")
        for t, line in v[:6]:
            print(f"      {t:>5} min  {line}")
    print(f"  area 20 quirk: {r['area20_minutes']} min, from minute {r['area20_first_min']}")
    if r["left_over"]:
        print(f"  left over: {r['left_over']}")
    if r["unresolved"]:
        print("  report, unresolved/uncertain section:")
        for line in r["unresolved"].splitlines()[:25]:
            print(f"      {line[:150]}")


def main():
    parser = argparse.ArgumentParser(description="How the agents went about each run.")
    parser.add_argument("--label", default="opus", help="Batch under results/runs (default: opus)")
    parser.add_argument("--run", action="append", help="Detail for this run (run_013); repeatable")
    parser.add_argument("--json", help="Also write every run's analysis here")
    args = parser.parse_args()

    dirs = sorted(glob.glob(os.path.join(PROJECT_ROOT, "results", "runs", args.label, "run_*")))
    rows = [analyze(d) for d in dirs if not args.run or os.path.basename(d) in args.run]
    if args.run:
        for r in rows:
            print_run(r)
            print()
    else:
        print_summary(rows)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, default=str)
        print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
