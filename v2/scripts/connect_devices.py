#!/usr/bin/env python3
"""Verify SSH access to every v2 GNS3 lab device over the CCAccess network using netmiko."""

import time
from concurrent.futures import ThreadPoolExecutor

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException

# Management addresses from gns3/topology-notes.md (CCAccess section)
DEVICES = [
    {"name": "HQ-Edge", "host": "192.168.122.11", "platform": "vyos"},
    {"name": "Hub-A", "host": "192.168.122.12", "platform": "vyos"},
    {"name": "Hub-B", "host": "192.168.122.13", "platform": "vyos"},
    {"name": "ISP-A", "host": "192.168.122.14", "platform": "vyos"},
    {"name": "ISP-B", "host": "192.168.122.15", "platform": "vyos"},
    {"name": "Branch-1", "host": "192.168.122.21", "platform": "mikrotik"},
    {"name": "Branch-2", "host": "192.168.122.22", "platform": "mikrotik"},
    {"name": "Branch-3", "host": "192.168.122.23", "platform": "mikrotik"},
    {"name": "Branch-4", "host": "192.168.122.24", "platform": "mikrotik"},
    {"name": "B1-PC1", "host": "192.168.122.31", "platform": "alpine"},
    {"name": "B1-PC2", "host": "192.168.122.32", "platform": "alpine"},
    {"name": "B2-PC1", "host": "192.168.122.33", "platform": "alpine"},
    {"name": "B2-PC2", "host": "192.168.122.34", "platform": "alpine"},
    {"name": "B3-PC1", "host": "192.168.122.35", "platform": "alpine"},
    {"name": "B3-PC2", "host": "192.168.122.36", "platform": "alpine"},
    {"name": "B4-PC1", "host": "192.168.122.37", "platform": "alpine"},
    {"name": "B4-PC2", "host": "192.168.122.38", "platform": "alpine"},
    {"name": "B4-PC3", "host": "192.168.122.39", "platform": "alpine"},
    {"name": "B4-PC4", "host": "192.168.122.40", "platform": "alpine"},
]

PLATFORMS = {
    "vyos": {
        "creds": {"device_type": "vyos", "username": "vyos", "password": "test"},
        "identity_cmd": "hostname",
    },
    "mikrotik": {
        "creds": {"device_type": "mikrotik_routeros", "username": "admin", "password": "test"},
        # "/system identity print" wraps the name one character per line over netmiko
        "identity_cmd": ":put [/system identity get name]",
    },
    "alpine": {
        "creds": {"device_type": "linux", "username": "root", "password": "alpine"},
        "identity_cmd": "hostname",
    },
}

TIMEOUT = 15
MAX_RETRIES = 2
RETRY_DELAY = 5


def connect_device(device):
    """Log in, run the platform's identity command, and return (status, detail).

    status is "OK", "WRONG ID" (SSH works but the hostname doesn't match), or "FAILED".
    """
    platform = PLATFORMS[device["platform"]]
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            conn = ConnectHandler(
                **platform["creds"],
                host=device["host"],
                port=22,
                conn_timeout=TIMEOUT,
                read_timeout_override=TIMEOUT,
            )
            identity = conn.send_command(platform["identity_cmd"]).strip()
            conn.disconnect()
            if identity != device["name"]:
                return "WRONG ID", f"SSH ok, but identity is {identity!r} (expected {device['name']!r})"
            return "OK", f"identity {identity!r}"
        except NetmikoAuthenticationException as e:
            return "FAILED", f"authentication failed: {e}".splitlines()[0]
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            else:
                return "FAILED", f"{type(e).__name__}: {e}".splitlines()[0]


def main():
    with ThreadPoolExecutor(max_workers=len(DEVICES)) as pool:
        results = list(pool.map(connect_device, DEVICES))

    print(f"{'Device':<10} {'Host':<16} {'Platform':<9} Result")
    print("-" * 80)
    for device, (status, detail) in zip(DEVICES, results):
        print(f"{device['name']:<10} {device['host']:<16} {device['platform']:<9} {status} - {detail}")

    reachable = sum(1 for status, _ in results if status != "FAILED")
    verified = sum(1 for status, _ in results if status == "OK")
    print("=" * 80)
    print(f"Results: {reachable}/{len(DEVICES)} devices reachable over SSH, "
          f"{verified}/{len(DEVICES)} with the expected identity")


if __name__ == "__main__":
    main()
