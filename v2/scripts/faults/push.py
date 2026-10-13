"""Send fault commands to the live devices.

VyOS:     configure, set/delete, commit, save (faults are saved like the rest of the broken
          baseline, so L3.persisted doesn't fail just because a fault was injected).
RouterOS: commands take effect and persist immediately.
Alpine:   shell commands, checked by exit status.
Every platform's output is checked for errors, because none of them raise on a bad command
through netmiko. A command that runs but does nothing (a [find] that matches no item) isn't an
error here; the read-back in fault_lab.py catches that.
"""

import re

from verifier.devices import DEVICE_BY_NAME, connect

VYOS_ERRORS = re.compile(r"failed|not valid|invalid|error|nothing to delete", re.I)
ROUTEROS_ERRORS = re.compile(r"syntax error|expected |bad command|failure|no such item|input does not match|invalid", re.I)
FRR_ERRORS = re.compile(r"^%", re.M)


class PushError(Exception):
    pass


def _vyos(conn, commands):
    out = conn.send_config_set(commands, exit_config_mode=False)
    if VYOS_ERRORS.search(out):
        conn.send_command_timing("exit discard")
        raise PushError(f"config rejected, nothing committed:\n{out.strip()}")
    out += conn.commit()
    # netmiko's save_config() raises ValueError on this VyOS build although the save works
    out += conn.send_command_timing("save")
    conn.send_command_timing("exit")
    return out


def _routeros(conn, commands):
    out = ""
    for cmd in commands:
        result = conn.send_command(cmd)
        out += f"{cmd}\n{result}\n"
        if ROUTEROS_ERRORS.search(result):
            raise PushError(f"{cmd!r} failed:\n{result.strip()}")
    return out


def _alpine(conn, commands):
    out = ""
    for cmd in commands:
        result = conn.send_command(f"{cmd}; echo __RC=$?")
        out += f"{cmd}\n{result}\n"
        match = re.search(r"__RC=(\d+)", result)
        if not match or match.group(1) != "0":
            raise PushError(f"{cmd!r} failed:\n{result.strip()}")
    return out


PUSHERS = {"vyos": _vyos, "routeros": _routeros, "alpine": _alpine}


def push(device, commands):
    """Apply config commands to a device. Returns the session output, raises PushError."""
    conn = connect(DEVICE_BY_NAME[device])
    try:
        return PUSHERS[DEVICE_BY_NAME[device]["platform"]](conn, commands)
    finally:
        conn.disconnect()


def run_op(device, command):
    """Run one operational command (a kick). VyOS kicks go through vtysh, so FRR errors start with %."""
    conn = connect(DEVICE_BY_NAME[device])
    try:
        out = conn.send_command(command)
    finally:
        conn.disconnect()
    if FRR_ERRORS.search(out) or ROUTEROS_ERRORS.search(out):
        raise PushError(f"{command!r} on {device} failed:\n{out.strip()}")
    return out
