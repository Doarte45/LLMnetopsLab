#!/usr/bin/env python3
"""Connect to VyOS routers in GNS3 lab via SSH using netmiko."""

import time
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


def connect_router(name, host):
    """Try to connect to a router, with retries on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            conn = ConnectHandler(
                **ROUTER_CREDS,
                host=host,
                conn_timeout=TIMEOUT,
                read_timeout_override=TIMEOUT,
            )
            output = conn.send_command("show version")
            conn.disconnect()
            return True, output
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
    results = {}
    for router in ROUTERS:
        name = router["name"]
        host = router["host"]
        print(f"--- {name} ({host}) ---", flush=True)

        success, output = connect_router(name, host)
        results[name] = success

        if success:
            print(output, flush=True)
            print(f"{name}: OK\n", flush=True)
        else:
            print(f"{name}: FAILED after {MAX_RETRIES} attempts - {output}\n", flush=True)

    # Summary
    print("=" * 40)
    passed = sum(1 for v in results.values() if v)
    print(f"Results: {passed}/{len(results)} routers connected successfully")
    for name, success in results.items():
        status = "OK" if success else "FAILED"
        print(f"  {name}: {status}")


if __name__ == "__main__":
    main()
