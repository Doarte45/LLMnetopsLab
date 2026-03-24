#!/usr/bin/env python3
"""Push VyOS configs (.set files) to routers via SSH.

Usage:
    python scripts/push_configs.py broken
    python scripts/push_configs.py corrected
"""

import sys
import time
import os
from netmiko import ConnectHandler

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


def parse_set_file(filepath):
    """Read a .set file and return a list of set commands."""
    commands = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line and line.startswith("set "):
                commands.append(line)
    return commands


def build_delete_commands(set_commands):
    """Build delete commands to clear sections before applying set commands.

    Extracts unique top-level config paths from set commands so we get a
    clean replacement rather than layering on top of existing config.
    E.g. 'set interfaces ethernet eth0 address ...' -> 'delete interfaces ethernet eth0'
         'set protocols ospf ...' -> 'delete protocols ospf'
         'set protocols static ...' -> 'delete protocols static'
         'set system host-name ...' -> 'delete system host-name'
    """
    delete_paths = set()
    for cmd in set_commands:
        # Remove the leading 'set '
        parts = cmd[4:].split()
        if parts[0] == "interfaces" and len(parts) >= 3:
            # Delete at the interface level: e.g. 'interfaces ethernet eth0'
            delete_paths.add(f"interfaces {parts[1]} {parts[2]}")
        elif parts[0] == "protocols" and len(parts) >= 2:
            # Delete at protocol level: e.g. 'protocols ospf'
            delete_paths.add(f"protocols {parts[1]}")
        elif parts[0] == "system" and len(parts) >= 2:
            # Delete specific system setting: e.g. 'system host-name'
            delete_paths.add(f"system {parts[1]}")
        else:
            delete_paths.add(parts[0])

    return [f"delete {path}" for path in sorted(delete_paths)]


def push_config(name, host, config_path):
    """Push a .set config file to a router, with retries."""
    set_commands = parse_set_file(config_path)
    if not set_commands:
        return False, "No set commands found in config file"

    delete_commands = build_delete_commands(set_commands)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            conn = ConnectHandler(
                **ROUTER_CREDS,
                host=host,
                conn_timeout=TIMEOUT,
                read_timeout_override=TIMEOUT,
            )

            # Enter configure mode
            conn.config_mode()

            # Delete existing sections for a clean replacement
            print(f"  Deleting old config sections...", flush=True)
            for cmd in delete_commands:
                conn.send_command_timing(cmd)

            # Apply new set commands
            print(f"  Applying {len(set_commands)} set commands...", flush=True)
            for cmd in set_commands:
                conn.send_command_timing(cmd)

            # Commit and save
            print("  Committing...", flush=True)
            commit_output = conn.send_command_timing("commit", delay_factor=4)
            if "error" in commit_output.lower() or "failed" in commit_output.lower():
                conn.send_command_timing("discard")
                conn.exit_config_mode()
                conn.disconnect()
                return False, f"Commit failed: {commit_output}"

            print("  Saving...", flush=True)
            conn.send_command_timing("save")

            conn.exit_config_mode()

            # Clear command history so it can't be used to find the solution
            print("  Clearing command history...", flush=True)
            conn.send_command_timing("rm -f /home/vyos/.bash_history")
            conn.send_command_timing("history -c")

            conn.disconnect()
            return True, f"Applied {len(set_commands)} commands"

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


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("broken", "corrected"):
        print("Usage: python scripts/push_configs.py <broken|corrected>")
        sys.exit(1)

    config_type = sys.argv[1]
    config_dir = os.path.join("configs", "VyOS", config_type)

    if not os.path.isdir(config_dir):
        print(f"Config directory not found: {config_dir}")
        sys.exit(1)

    # Map router names to their config files
    config_files = {}
    for router in ROUTERS:
        filename = f"{router['name']}-{config_type}.set"
        filepath = os.path.join(config_dir, filename)
        if not os.path.isfile(filepath):
            print(f"Config file not found: {filepath}")
            sys.exit(1)
        config_files[router["name"]] = filepath

    print(f"Pushing '{config_type}' configs to {len(ROUTERS)} routers\n", flush=True)

    results = {}
    for router in ROUTERS:
        name = router["name"]
        host = router["host"]
        config_path = config_files[name]
        print(f"--- {name} ({host}) [{os.path.basename(config_path)}] ---", flush=True)

        success, message = push_config(name, host, config_path)
        results[name] = success

        if success:
            print(f"{name}: OK - {message}\n", flush=True)
        else:
            print(f"{name}: FAILED after {MAX_RETRIES} attempts - {message}\n", flush=True)

    # Summary
    print("=" * 40)
    passed = sum(1 for v in results.values() if v)
    print(f"Results: {passed}/{len(results)} routers configured successfully")
    for name, success in results.items():
        status = "OK" if success else "FAILED"
        print(f"  {name}: {status}")


if __name__ == "__main__":
    main()
