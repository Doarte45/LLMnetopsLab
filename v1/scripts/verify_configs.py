#!/usr/bin/env python3
"""Verify OSPF troubleshooting lab by comparing live router configs to answer keys."""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

from netmiko import ConnectHandler

# ---------------------------------------------------------------------------
# Router definitions (reused from connect_routers.py)
# ---------------------------------------------------------------------------

ROUTERS = [
    {"name": "HQ", "host": "192.168.122.6"},
    {"name": "Branch1", "host": "192.168.122.5"},
    {"name": "Branch2", "host": "192.168.122.7"},
]

ROUTER_CREDS = {
    "device_type": "vyos",
    "username": "vyos",
    "password": "test",
    "port": 22,
}

TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5

# Path to corrected .set files (relative to project root)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
CORRECTED_DIR = os.path.join(PROJECT_ROOT, "configs", "VyOS", "corrected")

# ---------------------------------------------------------------------------
# Config sections we care about (filter out management / system defaults)
# ---------------------------------------------------------------------------

RELEVANT_PREFIXES = (
    "set interfaces ethernet eth0 ",
    "set interfaces ethernet eth1 ",
    "set interfaces ethernet eth2 ",
    "set interfaces loopback lo ",
    "set protocols ospf ",
    "set protocols static ",
    "set system host-name ",
)

# ---------------------------------------------------------------------------
# Known errors — the 6 intentional bugs students must fix
# ---------------------------------------------------------------------------

KNOWN_ERRORS = [
    {
        "id": 1,
        "router": "HQ",
        "description": "Wrong IP on eth0 (10.10.10.1/22 → 10.10.0.1/22)",
        "broken_present": ["set interfaces ethernet eth0 address 10.10.10.1/22"],
        "corrected_present": ["set interfaces ethernet eth0 address 10.10.0.1/22"],
    },
    {
        "id": 2,
        "router": "HQ",
        "description": "Wrong OSPF network mask (10.10.0.0/21 → 10.10.0.0/22)",
        "broken_present": ["set protocols ospf area 0 network 10.10.0.0/21"],
        "corrected_present": ["set protocols ospf area 0 network 10.10.0.0/22"],
    },
    {
        "id": 3,
        "router": "HQ",
        "description": "Missing default-information originate",
        "broken_present": [],
        "corrected_present": ["set protocols ospf default-information originate"],
    },
    {
        "id": 4,
        "router": "Branch2",
        "description": "eth2 still disabled",
        "broken_present": ["set interfaces ethernet eth2 disable"],
        "corrected_present": [],
    },
    {
        "id": 5,
        "router": "Branch2",
        "description": "Wrong passive interface (eth2 → eth0)",
        "broken_present": ["set protocols ospf interface eth2 passive"],
        "corrected_present": ["set protocols ospf interface eth0 passive"],
    },
    {
        "id": 6,
        "router": "Branch2",
        "description": "Wrong OSPF LAN network mask (10.10.4.0/22 → 10.10.6.0/23)",
        "broken_present": ["set protocols ospf area 0 network 10.10.4.0/22"],
        "corrected_present": ["set protocols ospf area 0 network 10.10.6.0/23"],
    },
]


def normalize_line(line):
    """Strip whitespace, remove single quotes, and return lowercase for comparison."""
    return line.strip().replace("'", "").lower()


def filter_relevant(lines):
    """Keep only lines matching relevant config sections."""
    result = set()
    for raw in lines:
        line = raw.strip().replace("'", "")
        if not line:
            continue
        if any(line.startswith(p) for p in RELEVANT_PREFIXES):
            result.add(line)
    return result


def load_corrected_config(router_name):
    """Load the corrected .set file for a router and return filtered lines."""
    path = os.path.join(CORRECTED_DIR, f"{router_name}-corrected.set")
    with open(path) as f:
        return filter_relevant(f.readlines())


def fetch_running_config(name, host):
    """SSH into a router and return its running config as a set of filtered lines."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            conn = ConnectHandler(
                **ROUTER_CREDS,
                host=host,
                conn_timeout=TIMEOUT,
                read_timeout_override=TIMEOUT,
            )
            output = conn.send_command("show configuration commands")
            conn.disconnect()
            return True, filter_relevant(output.splitlines())
        except Exception as e:
            if attempt < MAX_RETRIES:
                print(
                    f"  Attempt {attempt}/{MAX_RETRIES} failed: {e}\n"
                    f"  Retrying in {RETRY_DELAY}s...",
                    flush=True,
                )
                time.sleep(RETRY_DELAY)
            else:
                return False, str(e)


def check_error(error, running_lines):
    """Check whether a known error is fixed on the live router.

    Returns True if fixed, False if still broken.
    """
    running_lower = {normalize_line(l) for l in running_lines}

    # Broken lines should be ABSENT from a fixed config
    for line in error["broken_present"]:
        if normalize_line(line) in running_lower:
            return False

    # Corrected lines should be PRESENT in a fixed config
    for line in error["corrected_present"]:
        if normalize_line(line) not in running_lower:
            return False

    return True


DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "results", "verify")


def next_run_number(output_dir):
    """Scan output_dir for run_NNN.json and return the next number."""
    if not os.path.isdir(output_dir):
        return 1
    existing = []
    for f in os.listdir(output_dir):
        m = re.match(r"run_(\d{3})\.json$", f)
        if m:
            existing.append(int(m.group(1)))
    return max(existing) + 1 if existing else 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Verify OSPF lab by comparing live router configs to answer keys."
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help="Directory for JSON output files (default: results/verify/)",
    )
    parser.add_argument(
        "--run-number", type=int, default=None,
        help="Run number (e.g. 1 → run_001.json). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--no-file", action="store_true",
        help="Skip writing JSON output file (stdout only).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Structured results for JSON output
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "errors": [],
        "routers": {},
        "score": {},
    }

    width = 60
    print("=" * width)
    print("  OSPF Troubleshooting Lab — Verification Report")
    print("=" * width)
    print()

    fixed_count = 0
    total_errors = len(KNOWN_ERRORS)
    extra_summary = {}

    for router in ROUTERS:
        name = router["name"]
        host = router["host"]
        print(f"--- {name} ({host}) ---", flush=True)

        # Fetch live config
        success, result = fetch_running_config(name, host)
        if not success:
            print(f"  CONNECTION FAILED: {result}")
            print(f"  Skipping verification for {name}.\n")
            extra_summary[name] = "N/A (connection failed)"
            results["routers"][name] = {
                "connection": "failed",
                "error_message": result,
                "extra_commands": None,
                "missing_commands": None,
                "config_matches_answer_key": None,
            }
            # Record errors for this router as UNKNOWN
            for error in KNOWN_ERRORS:
                if error["router"] == name:
                    results["errors"].append({
                        "id": error["id"],
                        "router": name,
                        "description": error["description"],
                        "status": "UNKNOWN",
                    })
            continue

        running_lines = result
        corrected_lines = load_corrected_config(name)

        # Check known errors for this router
        router_errors = [e for e in KNOWN_ERRORS if e["router"] == name]
        if router_errors:
            for error in router_errors:
                is_fixed = check_error(error, running_lines)
                status = "FIXED" if is_fixed else "BROKEN"
                results["errors"].append({
                    "id": error["id"],
                    "router": name,
                    "description": error["description"],
                    "status": status,
                })
                if is_fixed:
                    fixed_count += 1
                    tag = "[FIXED]  "
                else:
                    tag = "[BROKEN] "
                print(f"  {tag}#{error['id']} {error['description']}")
        else:
            print("  No known errors for this router.")

        # Collect known error lines for this router so we can exclude them
        # from the generic extra/missing lists (they're already reported above)
        known_lines = set()
        for error in router_errors:
            for line in error["broken_present"]:
                known_lines.add(normalize_line(line))
            for line in error["corrected_present"]:
                known_lines.add(normalize_line(line))

        # Set comparison: extra and missing lines
        running_lower = {normalize_line(l) for l in running_lines}
        corrected_lower = {normalize_line(l) for l in corrected_lines}

        # Filter out lines already covered by known error tracking
        extra_ci = {
            l for l in running_lines
            if normalize_line(l) not in corrected_lower
            and normalize_line(l) not in known_lines
        }
        missing_ci = {
            l for l in corrected_lines
            if normalize_line(l) not in running_lower
            and normalize_line(l) not in known_lines
        }

        if extra_ci:
            print(f"\n  Extra commands (not in answer key):")
            for line in sorted(extra_ci):
                print(f"    + {line}")

        if missing_ci:
            print(f"\n  Missing commands (should be present):")
            for line in sorted(missing_ci):
                print(f"    - {line}")

        # Only show "matches answer key" when there are no issues at all
        all_fixed = all(check_error(e, running_lines) for e in router_errors)
        matches_key = not extra_ci and not missing_ci and all_fixed
        if matches_key:
            if router_errors:
                print("\n  Config matches answer key: YES")
            else:
                print("  Config matches answer key: YES")

        results["routers"][name] = {
            "connection": "ok",
            "extra_commands": sorted(extra_ci),
            "missing_commands": sorted(missing_ci),
            "config_matches_answer_key": matches_key,
        }

        extra_summary[name] = len(extra_ci)
        print()

    # Overall score
    pct = (fixed_count * 100 // total_errors) if total_errors else 100
    results["score"] = {
        "fixed": fixed_count,
        "total": total_errors,
        "percentage": pct,
    }

    print("=" * width)
    print(f"  OVERALL SCORE: {fixed_count}/{total_errors} errors fixed ({pct}%)")
    if fixed_count == total_errors:
        print("  Status: COMPLETE")
    elif fixed_count == 0:
        print("  Status: NOT STARTED")
    else:
        print("  Status: PARTIALLY COMPLETE")
    print("=" * width)

    print("\n  Extra configuration summary:")
    for name in [r["name"] for r in ROUTERS]:
        val = extra_summary.get(name, "?")
        if isinstance(val, int):
            label = "extra command" if val == 1 else "extra commands"
            print(f"    {name + ':':10s} {val} {label}")
        else:
            print(f"    {name + ':':10s} {val}")
    print()

    # Write JSON output file
    if not args.no_file:
        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        run_num = args.run_number if args.run_number is not None else next_run_number(output_dir)
        filename = f"run_{run_num:03d}.json"
        filepath = os.path.join(output_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print(f"  Verify output saved to {os.path.relpath(filepath, PROJECT_ROOT)}")
        except OSError as e:
            print(f"  WARNING: Could not write output file: {e}")


if __name__ == "__main__":
    main()
