"""Layer 1: host reachability, pinged from the hosts themselves.

Each host pings the other nine hosts and both ISP loopbacks in parallel (one SSH session per
host). Allowed pairs that fail are pinged again once: passing on the retry is "flaky", failing
twice is "fail". Expected-deny pairs (guest to non-guest, both directions) pass when the ping
fails and aren't retried.
"""

import re

from . import design
from .checks import check
from .devices import DEVICE_BY_NAME, connect, run_parallel

# BusyBox ping: -c count, -W seconds to wait for a reply, -w overall deadline
PING = "ping -c 3 -W 1 -w 4"


def targets_for(host):
    return {d: ip for d, ip in design.HOSTS.items() if d != host} | {ip: ip for ip in design.INTERNET_TARGETS}


def ping_script(ips):
    """One shell line that pings every IP in parallel and prints "PING <ip> ok|fail".

    The outer subshell keeps the shell's job-control messages out of the output.
    """
    return ("( for t in " + " ".join(ips) + "; do ( if " + PING + " $t >/dev/null 2>&1; "
            "then echo PING $t ok; else echo PING $t fail; fi ) & done; wait )")


def parse_ping_output(text):
    return {ip: result == "ok" for ip, result in re.findall(r"^PING (\S+) (ok|fail)\s*$", text, re.M)}


def collect(retry=True):
    """Return {host: {"first": {target: bool}, "retry": {target: bool}}}."""

    def one(device):
        host = device["name"]
        targets = targets_for(host)
        conn = connect(device)
        try:
            first = parse_ping_output(conn.send_command(ping_script(targets.values()), read_timeout=60))
            by_ip = {ip: name for name, ip in targets.items()}
            redo = [ip for ip, ok in first.items()
                    if not ok and design.ping_expectation(host, by_ip[ip]) == "allow"]
            second = {}
            if retry and redo:
                second = parse_ping_output(conn.send_command(ping_script(redo), read_timeout=60))
            return {"first": first, "retry": second}
        finally:
            conn.disconnect()

    return run_parallel(one, [DEVICE_BY_NAME[h] for h in design.HOSTS])


def analyze(raw):
    """Return (checks, matrix). matrix[src][dst] is the check status."""
    checks, matrix = [], {}
    for src in design.HOSTS:
        data = raw.get(src, {"error": "not collected"})
        matrix[src] = {}
        for dst, ip in targets_for(src).items():
            expectation = design.ping_expectation(src, dst)
            category = "internet" if dst in design.INTERNET_TARGETS else ("allowed" if expectation == "allow" else "denied")
            cid = f"L1.ping.{src}->{dst}"
            if "error" in data or ip not in data.get("first", {}):
                status, observed = "error", data.get("error", "no result")
            elif expectation == "deny":
                status = "fail" if data["first"][ip] else "pass"
                observed = "reachable" if data["first"][ip] else "blocked"
            elif data["first"][ip]:
                status, observed = "pass", "reachable"
            elif data.get("retry", {}).get(ip):
                status, observed = "flaky", "failed once, passed on retry"
            else:
                status, observed = "fail", "unreachable"
            matrix[src][dst] = status
            checks.append(check(1, cid, status, "reachable" if expectation == "allow" else "blocked",
                                observed, device=src, category=f"ping_{category}"))
    return checks, matrix
