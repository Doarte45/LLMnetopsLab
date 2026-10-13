"""Parse control-plane output for Layer 2: FRR JSON on VyOS, print terse on RouterOS.

Every parser returns plain dicts so the analysis doesn't care which platform produced them.
FRR's JSON field names have shifted between releases, so each field tries the known
spellings in turn.
"""

import json
import re
import shlex


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in output")
    return json.loads(text[start:end + 1])


def _first(d, *keys):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FRR (VyOS)
# ---------------------------------------------------------------------------

def frr_neighbors(text):
    """Parse "show ip ospf neighbor detail json" into a list of neighbors."""
    data = extract_json(text)
    table = data.get("neighbors", data)
    out = []
    for rid, entries in table.items():
        if not isinstance(entries, list):
            entries = [entries]
        for e in entries:
            if not isinstance(e, dict):
                continue
            state = str(_first(e, "nbrState", "state", "converged") or "")
            iface = str(_first(e, "ifaceName", "interfaceName") or "").split(":")[0]
            uptime_ms = _first(e, "upTimeInMsec", "lastPrgrsvChangeMsec")
            out.append({
                "rid": rid,
                "state": state.split("/")[0],
                "interface": iface,
                "virtual": iface.upper().startswith("VLINK"),
                "state_changes": _int(_first(e, "stateChangeCounter", "stateChanges")),
                "uptime_s": _int(uptime_ms) / 1000 if _int(uptime_ms) is not None else None,
            })
    return out


def frr_routes(text):
    """Parse "show ip route json" into the selected routes."""
    data = extract_json(text)
    out = []
    for prefix, entries in data.items():
        for e in entries if isinstance(entries, list) else [entries]:
            if not e.get("selected"):
                continue
            hops = [n["ip"] for n in e.get("nexthops", []) if n.get("ip")]
            out.append({"prefix": prefix, "protocol": e.get("protocol"), "nexthops": hops})
    return out


def frr_ospf_route_types(text):
    """Parse "show ip ospf route json" into {prefix: route type} ("N", "N IA", "N E2" ...)."""
    data = extract_json(text)
    out = {}
    for prefix, entry in data.items():
        if "/" not in prefix or not isinstance(entry, dict):
            continue
        out[prefix] = str(entry.get("routeType", "")).strip()
    return out


def frr_bgp_summary(text):
    """Parse "show bgp ipv4 unicast summary json" into {peer: session info}."""
    data = extract_json(text)
    data = data.get("ipv4Unicast", data)
    out = {}
    for peer, p in data.get("peers", {}).items():
        out[peer] = {
            "state": p.get("state"),
            "remote_as": _int(_first(p, "remoteAs")),
            "pfx_rcd": _int(_first(p, "pfxRcd", "prefixReceivedCount")),
            "pfx_snt": _int(_first(p, "pfxSnt", "prefixSentCount")),
            "uptime_s": (_int(p.get("peerUptimeMsec")) or 0) / 1000,
        }
    return out


def frr_bgp_table(text):
    """Parse "show bgp ipv4 unicast json" into {prefix: [paths]}."""
    data = extract_json(text)
    out = {}
    for prefix, paths in data.get("routes", {}).items():
        for p in paths if isinstance(paths, list) else [paths]:
            best = p.get("bestpath")
            if isinstance(best, dict):
                best = best.get("overall", True)
            hops = [n.get("ip") for n in p.get("nexthops", []) if n.get("ip")]
            as_path = p.get("path")
            if isinstance(as_path, dict) or as_path is None:
                as_path = (p.get("aspath") or as_path or {}).get("string", "")
            out.setdefault(prefix, []).append({
                "next_hop": hops[0] if hops else p.get("peerId"),
                "peer": p.get("peerId"),
                "best": bool(best),
                "valid": bool(p.get("valid", True)),
                "local_pref": _int(_first(p, "locPrf", "localPref")),
                "as_path": str(as_path),
            })
    return out


# ---------------------------------------------------------------------------
# RouterOS
# ---------------------------------------------------------------------------

ROS_PROTOCOL_FLAGS = {"c": "connected", "s": "static", "o": "ospf", "b": "bgp", "d": "dhcp", "r": "rip", "v": "vpn"}
ROS_PROTOCOL_BY_DISTANCE = {"0": "connected", "1": "static", "110": "ospf", "20": "bgp", "200": "bgp"}


def ros_terse(text):
    """Parse "print terse" output into a list of {"_flags": "...", key: value}."""
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if "=" not in line or line.startswith(("#", "Flags:", "Columns:")):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()
        flags, attrs = "", {}
        for tok in tokens:
            if "=" in tok:
                key, value = tok.split("=", 1)
                attrs[key] = value
            elif not attrs and not tok.isdigit():
                flags += tok
        attrs["_flags"] = flags
        items.append(attrs)
    return items


def ros_duration(value):
    """Parse RouterOS durations ("1w2d3h4m5s", "4m17s320ms", "00:04:17") into seconds."""
    if not value:
        return None
    if ":" in value:
        parts = [int(p) for p in value.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
    total = 0
    for number, unit in re.findall(r"(\d+)(ms|w|d|h|m|s)", value):
        total += int(number) * units[unit]
    return total


def ros_neighbors(text):
    """Parse "/routing ospf neighbor print terse"."""
    out = []
    for n in ros_terse(text):
        if "router-id" not in n:
            continue
        out.append({
            "rid": n["router-id"],
            "state": n.get("state", ""),
            "interface": n.get("interface", n.get("address", "")),
            "virtual": False,
            "state_changes": _int(n.get("state-changes")),
            "uptime_s": ros_duration(n.get("adjacency")),
        })
    return out


def ros_routes(text):
    """Parse "/ip route print terse" into active main-table routes."""
    out = []
    for r in ros_terse(text):
        if "dst-address" not in r or r.get("routing-table", "main") != "main":
            continue
        flags = r["_flags"]
        if flags and "A" not in flags:
            continue
        protocol = next((p for f, p in ROS_PROTOCOL_FLAGS.items() if f in flags), None)
        protocol = protocol or ROS_PROTOCOL_BY_DISTANCE.get(r.get("distance"), "unknown")
        hops = []
        for gw in r.get("gateway", "").split(","):
            gw = gw.split("%")[0]
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", gw):
                hops.append(gw)
        out.append({"prefix": r["dst-address"], "protocol": protocol, "nexthops": hops})
    return out
