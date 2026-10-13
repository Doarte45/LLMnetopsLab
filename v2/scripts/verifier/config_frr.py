"""Check that a VyOS router's committed config actually reached FRR.

During lab validation, VyOS reported successful commits that vyos-configd never rendered
into FRR. So Layer 3 also reads "vtysh -c 'show running-config'" and compares the routing
parts of it against the same view derived from the VyOS config model.
"""

from .model import area_id, norm_prefix


def _empty_view():
    return {
        "ospf.router_id": [], "ospf.networks": [], "ospf.interface_areas": [], "ospf.ranges": [],
        "ospf.virtual_links": [], "ospf.area_types": [], "ospf.area_auth": [],
        "ospf.default_originate": [], "ospf.md5_keys": [], "ospf.passive": [],
        "bgp.asn": [], "bgp.router_id": [], "bgp.neighbors": [], "bgp.route_maps": [],
        "bgp.networks": [], "bgp.default_originate": [], "route_maps": [],
    }


def parse_frr_config(text):
    view = _empty_view()
    block, block_arg = None, None
    for raw in text.splitlines():
        if not raw.strip() or raw.strip() in ("!", "exit", "exit-address-family", "end"):
            if raw.strip() in ("!", "exit", "end") and not raw.startswith(" "):
                block = None
            continue
        t = raw.split()
        if not raw.startswith(" "):
            block, block_arg = t[0], t[1:]
            if t[:2] == ["router", "bgp"] and len(t) > 2:
                view["bgp.asn"].append(t[2])
            elif t[0] == "route-map" and len(t) > 3:
                view["route_maps"].append(f"{t[1]} {t[2]} {t[3]}")
            continue
        if block == "router" and block_arg[:1] == ["ospf"]:
            _ospf_line(view, t)
        elif block == "router" and block_arg[:1] == ["bgp"]:
            _bgp_line(view, t)
        elif block == "interface" and block_arg:
            name = block_arg[0]
            if t[:3] == ["ip", "ospf", "message-digest-key"] and len(t) > 5:
                view["ospf.md5_keys"].append(f"{name} {t[3]} {t[5]}")
            elif t[:3] == ["ip", "ospf", "passive"]:
                view["ospf.passive"].append(name)
            elif t[:3] == ["ip", "ospf", "area"] and len(t) > 3:
                view["ospf.interface_areas"].append(f"{name} {area_id(t[3])}")
        elif block == "route-map" and len(block_arg) > 2:
            view["route_maps"].append(f"{block_arg[0]} {block_arg[2]} {' '.join(t)}")
    return {k: sorted(set(v)) for k, v in view.items()}


def _ospf_line(view, t):
    if t[:2] == ["ospf", "router-id"] and len(t) > 2:
        view["ospf.router_id"].append(t[2])
    elif t[0] == "network" and len(t) > 3:
        view["ospf.networks"].append(f"{norm_prefix(t[1])} {area_id(t[3])}")
    elif t[0] == "area" and len(t) > 2:
        aid = area_id(t[1])
        if t[2] == "range" and len(t) > 3:
            view["ospf.ranges"].append(f"{aid} {norm_prefix(t[3])}")
        elif t[2] == "virtual-link" and len(t) > 3:
            view["ospf.virtual_links"].append(f"{aid} {t[3]}")
            if "message-digest-key" in t:
                i = t.index("message-digest-key")
                if i + 3 < len(t):
                    view["ospf.md5_keys"].append(f"vlink-{t[3]} {t[i + 1]} {t[i + 3]}")
        elif t[2] in ("stub", "nssa"):
            view["ospf.area_types"].append(f"{aid} {' '.join(t[2:])}")
        elif t[2:4] == ["authentication", "message-digest"]:
            view["ospf.area_auth"].append(f"{aid} md5")
        elif t[2:] == ["authentication"]:
            view["ospf.area_auth"].append(f"{aid} plaintext")
    elif t[:2] == ["default-information", "originate"]:
        view["ospf.default_originate"].append("always" if "always" in t else "conditional")
    elif t[0] == "passive-interface" and len(t) > 1:
        view["ospf.passive"].append(t[1])


def _bgp_line(view, t):
    if t[:2] == ["bgp", "router-id"] and len(t) > 2:
        view["bgp.router_id"].append(t[2])
    elif t[0] == "neighbor" and len(t) > 3 and t[2] == "remote-as":
        view["bgp.neighbors"].append(f"{t[1]} {t[3]}")
    elif t[0] == "neighbor" and len(t) > 4 and t[2] == "route-map":
        view["bgp.route_maps"].append(f"{t[1]} {t[3]} {t[4]}")
    elif t[0] == "neighbor" and len(t) > 2 and t[2] == "default-originate":
        view["bgp.default_originate"].append(t[1])
    elif t[0] == "network" and len(t) > 1:
        view["bgp.networks"].append(norm_prefix(t[1]))


def view_from_model(model):
    """The FRR view a VyOS config model should render to."""
    view = _empty_view()
    o = model.get("ospf")
    if o:
        if o["router_id"]:
            view["ospf.router_id"].append(o["router_id"])
        for aid, prefix in o.get("_networks", []):
            view["ospf.networks"].append(f"{prefix} {aid}")
        for name, entry in o["interfaces"].items():
            if entry.get("_explicit_area"):
                view["ospf.interface_areas"].append(f"{name} {entry['area']}")
            if entry["passive"]:
                view["ospf.passive"].append(name)
            auth = entry["auth"]
            if auth and auth.get("type") == "md5":
                view["ospf.md5_keys"].append(f"{name} {auth.get('key_id')} {auth.get('key')}")
        for aid, area in o["areas"].items():
            for prefix in area["ranges"]:
                view["ospf.ranges"].append(f"{aid} {prefix}")
            if area["type"] in ("stub", "nssa"):
                view["ospf.area_types"].append(f"{aid} {area['type']}" + (" no-summary" if area["no_summary"] else ""))
            if area["auth"]:
                view["ospf.area_auth"].append(f"{aid} {area['auth']}")
        for rid, vlink in o["virtual_links"].items():
            view["ospf.virtual_links"].append(f"{vlink['transit_area']} {rid}")
            auth = vlink["auth"]
            if auth and auth.get("type") == "md5":
                view["ospf.md5_keys"].append(f"vlink-{rid} {auth.get('key_id')} {auth.get('key')}")
        if o["default_originate"]:
            view["ospf.default_originate"].append("always" if o["default_originate"]["always"] else "conditional")
    b = model.get("bgp")
    if b:
        if b["asn"]:
            view["bgp.asn"].append(b["asn"])
        if b["router_id"]:
            view["bgp.router_id"].append(b["router_id"])
        for ip, n in b["neighbors"].items():
            if n["remote_as"]:
                view["bgp.neighbors"].append(f"{ip} {n['remote_as']}")
            if n["import"]:
                view["bgp.route_maps"].append(f"{ip} {n['import']} in")
            if n["export"]:
                view["bgp.route_maps"].append(f"{ip} {n['export']} out")
            if n["default_originate"]:
                view["bgp.default_originate"].append(ip)
        view["bgp.networks"].extend(b["networks"])
    for name, rules in model.get("route_maps", {}).items():
        for rule_key, attrs in rules.items():
            seq = rule_key.split()[-1]
            if attrs.get("action"):
                view["route_maps"].append(f"{name} {attrs['action']} {seq}")
            for key, value in attrs.items():
                if key.startswith(("set ", "match ")):
                    view["route_maps"].append(f"{name} {seq} {key} {value}")
    return {k: sorted(set(v)) for k, v in view.items()}


def diff_views(expected, actual):
    """Return [{"path", "kind", "item"}] for items missing from or extra in FRR."""
    diffs = []
    for key in sorted(set(expected) | set(actual)):
        want, have = set(expected.get(key, [])), set(actual.get(key, []))
        diffs += [{"path": key, "kind": "missing_in_frr", "item": i} for i in sorted(want - have)]
        diffs += [{"path": key, "kind": "extra_in_frr", "item": i} for i in sorted(have - want)]
    return diffs
