"""Layer 2: control-plane state (OSPF adjacencies, routing tables, BGP).

Collection polls each OSPF router until two snapshots taken `interval` seconds apart agree
(convergence gate), and the last two snapshots are kept so the analysis can spot flapping
that a single snapshot would miss.
"""

import ipaddress
import time

from . import design
from .checks import check, passfail
from .devices import DEVICE_BY_NAME, connect, run_commands
from .state import (
    frr_bgp_summary, frr_bgp_table, frr_neighbors, frr_ospf_route_types, frr_routes,
    ros_neighbors, ros_routes,
)

VYOS_OSPF_COMMANDS = {
    "neighbors": 'vtysh -c "show ip ospf neighbor detail json"',
    "routes": 'vtysh -c "show ip route json"',
    "ospf_routes": 'vtysh -c "show ip ospf route json"',
}
BGP_COMMANDS = {
    "bgp_summary": 'vtysh -c "show bgp ipv4 unicast summary json"',
    "bgp_table": 'vtysh -c "show bgp ipv4 unicast json"',
}
ROUTEROS_COMMANDS = {
    "neighbors": "/routing ospf neighbor print terse without-paging",
    "routes": "/ip route print terse without-paging",
}

LAYER2_DEVICES = design.OSPF_ROUTERS + list(design.ISP_OWN_PREFIXES)

PARSERS = {
    ("vyos", "neighbors"): frr_neighbors,
    ("vyos", "routes"): frr_routes,
    ("vyos", "ospf_routes"): frr_ospf_route_types,
    ("vyos", "bgp_summary"): frr_bgp_summary,
    ("vyos", "bgp_table"): frr_bgp_table,
    ("routeros", "neighbors"): ros_neighbors,
    ("routeros", "routes"): ros_routes,
}


def commands_for(name):
    platform = DEVICE_BY_NAME[name]["platform"]
    if platform == "routeros":
        return dict(ROUTEROS_COMMANDS)
    if name in design.ISP_OWN_PREFIXES:
        return {"bgp_table": BGP_COMMANDS["bgp_table"]}
    cmds = dict(VYOS_OSPF_COMMANDS)
    if name == design.BGP_ROUTER:
        cmds.update(BGP_COMMANDS)
    return cmds


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def parse_snapshot(name, outputs):
    """Parse one snapshot's raw outputs. Unparseable outputs become {"_error": ...}."""
    platform = DEVICE_BY_NAME[name]["platform"]
    parsed = {}
    for key, text in outputs.items():
        try:
            parsed[key] = PARSERS[(platform, key)](text)
        except Exception as e:
            parsed[key] = {"_error": f"{type(e).__name__}: {e}"}
    return parsed


def stability_key(name, outputs):
    """What must not change between two snapshots for a router to count as converged."""
    parsed = parse_snapshot(name, outputs)
    key = []
    neighbors = parsed.get("neighbors")
    if isinstance(neighbors, list):
        key += sorted((n["rid"], n["interface"], n["state"], n["state_changes"]) for n in neighbors)
    else:
        key.append(repr(neighbors))
    summary = parsed.get("bgp_summary")
    if isinstance(summary, dict) and "_error" not in summary:
        key += sorted((peer, s["state"], s["pfx_rcd"]) for peer, s in summary.items())
    return key


def collect(interval=30, timeout=180, devices=None):
    """Return {device: {"converged": bool, "snapshots": [{"t": s, "outputs": {...}}]}}."""
    from .devices import run_parallel

    def one(device):
        name = device["name"]
        cmds = commands_for(name)
        poll = name in design.OSPF_ROUTERS
        conn = connect(device)
        try:
            start, snaps, converged = time.monotonic(), [], not poll
            while True:
                snaps.append({"t": round(time.monotonic() - start, 1), "outputs": run_commands(conn, cmds)})
                if not poll:
                    break
                if len(snaps) >= 2 and stability_key(name, snaps[-2]["outputs"]) == stability_key(name, snaps[-1]["outputs"]):
                    converged = True
                    break
                if time.monotonic() - start + interval > timeout:
                    break
                time.sleep(interval)
            return {"converged": converged, "snapshots": snaps[-2:]}
        finally:
            conn.disconnect()

    names = devices or LAYER2_DEVICES
    return run_parallel(one, [DEVICE_BY_NAME[n] for n in names])


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _get(parsed, name, key):
    """Return parsed data, or None if the device or output is unavailable."""
    data = parsed.get(name, {}).get(key)
    if data is None or (isinstance(data, dict) and "_error" in data):
        return None
    return data


def _error_detail(raw, parsed, name, key):
    if "error" in raw.get(name, {"error": "not collected"}):
        return f"{name}: {raw.get(name, {}).get('error', 'not collected')}"
    data = parsed.get(name, {}).get(key)
    if isinstance(data, dict) and "_error" in data:
        return f"{name} {key}: {data['_error']}"
    return f"{name}: no {key} output"


def analyze(raw):
    """Turn collected Layer 2 data into checks."""
    parsed, previous = {}, {}
    for name, data in raw.items():
        snaps = data.get("snapshots") or []
        if snaps:
            parsed[name] = parse_snapshot(name, snaps[-1]["outputs"])
        if len(snaps) >= 2:
            previous[name] = parse_snapshot(name, snaps[-2]["outputs"])

    checks = []
    checks += _adjacency_checks(raw, parsed)
    checks += _stability_checks(raw, parsed, previous)
    checks += _default_route_checks(raw, parsed)
    checks += _route_type_checks(raw, parsed)
    checks += _coverage_checks(raw, parsed)
    checks += _summary_checks(raw, parsed)
    checks += _stub_checks(raw, parsed)
    checks += _bgp_checks(raw, parsed)
    checks += _isp_checks(raw, parsed)
    return checks


def _neighbor_state(neighbors, peer, kind):
    rid = design.ROUTER_IDS[peer]
    states = [n["state"] for n in neighbors if n["rid"] == rid and n["virtual"] == (kind == "vlink")]
    if not states:
        return "absent"
    return "Full" if "Full" in states else states[0]


def _adjacency_checks(raw, parsed):
    checks = []
    for a, b, kind in design.ADJACENCIES:
        cid = f"L2.ospf_adjacency.{a}--{b}" + (".vlink" if kind == "vlink" else "")
        observed, missing = {}, []
        for local, peer in ((a, b), (b, a)):
            neighbors = _get(parsed, local, "neighbors")
            if neighbors is None:
                missing.append(_error_detail(raw, parsed, local, "neighbors"))
            else:
                observed[local] = _neighbor_state(neighbors, peer, kind)
        if missing:
            checks.append(check(2, cid, "error", "Full on both ends", observed, "; ".join(missing), device=a))
            continue
        ok = all(state == "Full" for state in observed.values())
        checks.append(check(2, cid, passfail(ok), "Full on both ends", observed, device=a))

    expected_peers = {r: set() for r in design.OSPF_ROUTERS}
    for a, b, kind in design.ADJACENCIES:
        expected_peers[a].add((design.ROUTER_IDS[b], kind == "vlink"))
        expected_peers[b].add((design.ROUTER_IDS[a], kind == "vlink"))
    for name in design.OSPF_ROUTERS:
        cid = f"L2.ospf_neighbors.{name}.no_unexpected"
        neighbors = _get(parsed, name, "neighbors")
        if neighbors is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "neighbors"), device=name))
            continue
        unexpected = [f"{n['rid']} ({n['interface']}, {n['state']})" for n in neighbors
                      if (n["rid"], n["virtual"]) not in expected_peers[name]]
        checks.append(check(2, cid, passfail(not unexpected), [], unexpected, device=name))
    return checks


def _stability_checks(raw, parsed, previous):
    checks = []
    for name in design.OSPF_ROUTERS:
        cid = f"L2.ospf_stable.{name}"
        now, before = _get(parsed, name, "neighbors"), _get(previous, name, "neighbors")
        if now is None or before is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "neighbors"), device=name))
            continue
        problems = []
        old = {(n["rid"], n["virtual"]): n for n in before}
        for n in now:
            o = old.get((n["rid"], n["virtual"]))
            if o is None:
                problems.append(f"{n['rid']} appeared")
                continue
            if o["state"] != n["state"]:
                problems.append(f"{n['rid']} {o['state']} -> {n['state']}")
            if None not in (o["state_changes"], n["state_changes"]) and n["state_changes"] != o["state_changes"]:
                problems.append(f"{n['rid']} state changes {o['state_changes']} -> {n['state_changes']}")
            elif None not in (o["uptime_s"], n["uptime_s"]) and n["uptime_s"] < o["uptime_s"]:
                problems.append(f"{n['rid']} uptime reset")
        for key in set(old) - {(n["rid"], n["virtual"]) for n in now}:
            problems.append(f"{key[0]} disappeared")
        if not raw[name].get("converged"):
            problems.insert(0, "did not converge before the timeout")
        checks.append(check(2, cid, passfail(not problems), "stable across snapshots", problems, device=name))
    return checks


def _default_route_checks(raw, parsed):
    checks = []
    for name, (protocol, next_hop) in design.DEFAULT_ROUTES.items():
        cid = f"L2.default_route.{name}"
        routes = _get(parsed, name, "routes")
        if routes is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "routes"), device=name))
            continue
        defaults = [r for r in routes if r["prefix"] == "0.0.0.0/0"]
        ok = any(r["protocol"] == protocol and (next_hop is None or next_hop in r["nexthops"]) for r in defaults)
        observed = [f"{r['protocol']} via {','.join(r['nexthops']) or '-'}" for r in defaults] or "none"
        checks.append(check(2, cid, passfail(ok), f"{protocol} via {next_hop or 'any'}", observed, device=name))
    return checks


def _route_type_checks(raw, parsed):
    checks = []
    for name, expected in design.OSPF_ROUTE_TYPES.items():
        types = _get(parsed, name, "ospf_routes")
        for prefix, want in expected.items():
            cid = f"L2.ospf_route_type.{name}.{prefix}"
            if types is None:
                checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "ospf_routes"), device=name))
                continue
            have = types.get(prefix, "absent")
            checks.append(check(2, cid, passfail(have == want), want, have, device=name))
    return checks


def _covering_route(routes, subnet, allow_default):
    net = ipaddress.ip_network(subnet)
    best = None
    for r in routes:
        try:
            rnet = ipaddress.ip_network(r["prefix"])
        except ValueError:
            continue
        if rnet.prefixlen == 0 and not allow_default:
            continue
        if net.subnet_of(rnet) and (best is None or rnet.prefixlen > best.prefixlen):
            best = rnet
    return best


def _coverage_checks(raw, parsed):
    checks = []
    for name in design.OSPF_ROUTERS:
        cid = f"L2.route_coverage.{name}"
        routes = _get(parsed, name, "routes")
        if routes is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "routes"), device=name))
            continue
        allow_default = name in design.STUB_ALLOWED_OSPF_ROUTES
        uncovered = [s for s in design.LAB_SUBNETS if _covering_route(routes, s, allow_default) is None]
        checks.append(check(2, cid, passfail(not uncovered), "every lab subnet covered", uncovered, device=name))
    return checks


def _summary_checks(raw, parsed):
    checks = []
    for name, rules in design.SUMMARIES.items():
        cid = f"L2.summaries.{name}"
        routes = _get(parsed, name, "routes")
        if routes is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "routes"), device=name))
            continue
        prefixes = {r["prefix"] for r in routes}
        problems = [f"missing {p}" for p in rules["present"] if p not in prefixes]
        problems += [f"leaked {p}" for p in rules["absent"] if p in prefixes]
        checks.append(check(2, cid, passfail(not problems), rules, problems, device=name))
    return checks


def _stub_checks(raw, parsed):
    checks = []
    for name, allowed in design.STUB_ALLOWED_OSPF_ROUTES.items():
        cid = f"L2.stub_area.{name}"
        routes = _get(parsed, name, "routes")
        if routes is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "routes"), device=name))
            continue
        extra = sorted(r["prefix"] for r in routes if r["protocol"] == "ospf" and r["prefix"] not in allowed)
        checks.append(check(2, cid, passfail(not extra), sorted(allowed), extra, "OSPF routes outside the allowed set", device=name))
    return checks


def _bgp_checks(raw, parsed):
    name = design.BGP_ROUTER
    summary, table = _get(parsed, name, "bgp_summary"), _get(parsed, name, "bgp_table")
    checks = []
    for peer, info in design.BGP_PEERS.items():
        isp = info["name"]
        if summary is None:
            for cid in (f"L2.bgp_session.{isp}", f"L2.bgp_received.{isp}", f"L2.bgp_advertised.{isp}"):
                checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "bgp_summary"), device=name))
        else:
            s = summary.get(peer, {})
            ok = s.get("state") == "Established" and s.get("remote_as") == info["remote_as"]
            checks.append(check(2, f"L2.bgp_session.{isp}", passfail(ok),
                                f"Established, AS {info['remote_as']}", f"{s.get('state')}, AS {s.get('remote_as')}", device=name))
            if table is None:
                checks.append(check(2, f"L2.bgp_received.{isp}", "error", detail=_error_detail(raw, parsed, name, "bgp_table"), device=name))
            else:
                received = {p for p, paths in table.items() if any(x["peer"] == peer or x["next_hop"] == peer for x in paths)}
                ok = received == info["received"] and s.get("pfx_rcd") == len(info["received"])
                checks.append(check(2, f"L2.bgp_received.{isp}", passfail(ok), sorted(info["received"]),
                                    {"prefixes": sorted(received), "pfx_rcd": s.get("pfx_rcd")}, device=name))
            checks.append(check(2, f"L2.bgp_advertised.{isp}", passfail(s.get("pfx_snt") == 0), 0, s.get("pfx_snt"), device=name))

    for prefix, (next_hop, local_pref) in design.BGP_BEST.items():
        cid = f"L2.bgp_best.{prefix}"
        if table is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "bgp_table"), device=name))
            continue
        best = [p for p in table.get(prefix, []) if p["best"]]
        ok = bool(best) and best[0]["next_hop"] == next_hop and (local_pref is None or best[0]["local_pref"] == local_pref)
        observed = f"via {best[0]['next_hop']}, local-pref {best[0]['local_pref']}" if best else "no best path"
        checks.append(check(2, cid, passfail(ok), f"via {next_hop}" + (f", local-pref {local_pref}" if local_pref else ""),
                            observed, device=name))

    cid = "L2.bgp_backup_default"
    if table is None:
        checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, name, "bgp_table"), device=name))
    else:
        backup = [p for p in table.get("0.0.0.0/0", []) if p["next_hop"] == design.BGP_BACKUP_DEFAULT and p["valid"]]
        ok = bool(backup) and not backup[0]["best"]
        observed = ("best" if backup[0]["best"] else "present, not best") if backup else "absent"
        checks.append(check(2, cid, passfail(ok), f"valid backup via {design.BGP_BACKUP_DEFAULT}", observed, device=name))
    return checks


def _isp_checks(raw, parsed):
    checks = []
    for isp, own in design.ISP_OWN_PREFIXES.items():
        cid = f"L2.isp_no_transit.{isp}"
        table = _get(parsed, isp, "bgp_table")
        if table is None:
            checks.append(check(2, cid, "error", detail=_error_detail(raw, parsed, isp, "bgp_table"), device=isp))
            continue
        problems = [f"{p} via AS path '{x['as_path']}'" for p, paths in table.items() for x in paths
                    if design.ENTERPRISE_AS in x["as_path"].split()]
        problems += [f"unexpected {p}" for p in table if p not in own]
        checks.append(check(2, cid, passfail(not problems), sorted(own), sorted(set(problems)), device=isp))
    return checks
