"""Lab inventory and SSH helpers shared by all three layers."""

import time
from concurrent.futures import ThreadPoolExecutor

# Management addresses from gns3/topology-notes.md (CCAccess section)
DEVICES = [
    {"name": "HQ-Edge", "host": "192.168.122.11", "platform": "vyos"},
    {"name": "Hub-A", "host": "192.168.122.12", "platform": "vyos"},
    {"name": "Hub-B", "host": "192.168.122.13", "platform": "vyos"},
    {"name": "ISP-A", "host": "192.168.122.14", "platform": "vyos"},
    {"name": "ISP-B", "host": "192.168.122.15", "platform": "vyos"},
    {"name": "Branch-1", "host": "192.168.122.21", "platform": "routeros"},
    {"name": "Branch-2", "host": "192.168.122.22", "platform": "routeros"},
    {"name": "Branch-3", "host": "192.168.122.23", "platform": "routeros"},
    {"name": "Branch-4", "host": "192.168.122.24", "platform": "routeros"},
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

DEVICE_BY_NAME = {d["name"]: d for d in DEVICES}

CREDS = {
    "vyos": {"device_type": "vyos", "username": "vyos", "password": "test"},
    # netmiko appends terminal options to the MikroTik username, so pass plain "admin"
    "routeros": {"device_type": "mikrotik_routeros", "username": "admin", "password": "test"},
    "alpine": {"device_type": "linux", "username": "root", "password": "alpine"},
}

TIMEOUT = 30
READ_TIMEOUT = 90
MAX_RETRIES = 3
RETRY_DELAY = 5


def devices_on(*platforms):
    return [d for d in DEVICES if d["platform"] in platforms]


def connect(device):
    """Open a netmiko session, retrying transient failures."""
    from netmiko import ConnectHandler
    from netmiko.exceptions import NetmikoAuthenticationException

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return ConnectHandler(
                **CREDS[device["platform"]],
                host=device["host"],
                port=22,
                conn_timeout=TIMEOUT,
                read_timeout_override=READ_TIMEOUT,
            )
        except NetmikoAuthenticationException:
            raise
        except Exception:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_DELAY)


def run_commands(conn, commands):
    """Run {key: command} on an open session and return {key: output}."""
    return {key: conn.send_command(cmd, read_timeout=READ_TIMEOUT) for key, cmd in commands.items()}


def run_parallel(func, devices):
    """Call func(device) for every device in parallel and return {name: result}.

    func should return a dict; an exception becomes {"error": "..."}.
    """
    def wrapped(device):
        try:
            return func(device)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}".splitlines()[0]}

    with ThreadPoolExecutor(max_workers=len(devices) or 1) as pool:
        results = list(pool.map(wrapped, devices))
    return {d["name"]: r for d, r in zip(devices, results)}
