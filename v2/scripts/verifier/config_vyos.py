"""Parse VyOS set-commands (repo .set files or "show configuration commands") into the model."""

import shlex

from .model import (
    area_id, assign_ospf_areas, iface, leaf, new_model, norm_address, norm_mtu, norm_prefix,
    ospf, ospf_area, ospf_iface,
)

# Per-interface leaves that are hardware detail, not lab config
IGNORED_INTERFACE_LEAVES = {"hw-id", "offload", "description", "ring-buffer", "speed", "duplex"}
# Sections where anything unmodeled is reported as extra config
OTHER_SECTIONS = {"firewall", "vrf", "qos", "load-balancing", "high-availability"}
OTHER_SERVICES = {"dhcp-server", "dhcp-relay"}


def parse_set_commands(text):
    model = new_model()
    networks = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("set "):
            continue
        try:
            tokens = shlex.split(line)[1:]
        except ValueError:
            model["other"].append(line)
            continue
        if len(tokens) < 2:
            continue
        _apply(model, tokens, line, networks)
    assign_ospf_areas(model, networks)
    if model["ospf"] is not None:
        model["ospf"]["_networks"] = networks
    return model


def _apply(model, t, line, networks):
    section = t[0]
    if section == "system":
        if t[1] == "host-name" and len(t) > 2:
            model["hostname"] = t[2]
    elif section == "service":
        if t[1] in OTHER_SERVICES:
            model["other"].append(" ".join(t))
    elif section == "interfaces":
        _interfaces(model, t)
    elif section == "protocols":
        if t[1] == "ospf":
            _ospf(model, t[2:], networks)
        elif t[1] == "bgp":
            _bgp(model, t[2:])
        elif t[1] == "static":
            model["static_routes"].append(" ".join(t[2:]))
        else:
            model["other"].append(" ".join(t))
    elif section == "policy":
        if t[1] == "route-map" and len(t) > 5 and t[3] == "rule":
            rule = model["route_maps"].setdefault(t[2], {}).setdefault(f"rule {t[4]}", {})
            leaf(rule, t[5:])
        else:
            model["other"].append(" ".join(t))
    elif section == "nat":
        if len(t) > 4 and t[2] == "rule":
            leaf(model["nat"].setdefault(f"{t[1]} rule {t[3]}", {}), t[4:])
        else:
            model["other"].append(" ".join(t))
    elif section in OTHER_SECTIONS:
        model["other"].append(" ".join(t))


def _interfaces(model, t):
    kind = t[1]
    if kind not in ("ethernet", "loopback"):
        model["other"].append(" ".join(t))
        return
    if len(t) < 4 or t[3] in IGNORED_INTERFACE_LEAVES:
        return
    name, rest = t[2], t[3:]
    intf = iface(model, name)
    if rest[0] == "address" and len(rest) > 1:
        intf["addresses"].append(norm_address(rest[1]))
    elif rest[0] == "mtu" and len(rest) > 1:
        intf["mtu"] = norm_mtu(rest[1])
    elif rest == ["disable"]:
        intf["disabled"] = True
    else:
        leaf(intf["attrs"], rest)


def _md5(tokens):
    """Parse "... md5 key-id K md5-key X" into an auth dict."""
    auth = {"type": "md5"}
    if "key-id" in tokens:
        i = tokens.index("key-id")
        auth["key_id"] = tokens[i + 1] if i + 1 < len(tokens) else None
    if "md5-key" in tokens:
        i = tokens.index("md5-key")
        auth["key"] = tokens[i + 1] if i + 1 < len(tokens) else None
    return auth


def _ospf(model, r, networks):
    o = ospf(model)
    if not r:
        return
    if r[0] == "parameters" and len(r) > 2 and r[1] == "router-id":
        o["router_id"] = r[2]
    elif r[0] == "area" and len(r) > 2:
        aid, rest = area_id(r[1]), r[2:]
        area = ospf_area(model, aid)
        if rest[0] == "network" and len(rest) > 1:
            networks.append((aid, norm_prefix(rest[1])))
        elif rest[0] == "range" and len(rest) > 1:
            rng = area["ranges"].setdefault(norm_prefix(rest[1]), {"configured": True})
            leaf(rng, rest[2:])
        elif rest[0] == "authentication" and len(rest) > 1:
            area["auth"] = rest[1]
        elif rest[0] == "area-type" and len(rest) > 1:
            area["type"] = rest[1]
            sub = rest[2:]
            if sub == ["no-summary"]:
                area["no_summary"] = True
            else:
                leaf(area["attrs"], sub)
        elif rest[0] == "virtual-link" and len(rest) > 1:
            vlink = ospf(model)["virtual_links"].setdefault(rest[1], {"transit_area": aid, "auth": None, "attrs": {}})
            sub = rest[2:]
            if sub[:2] == ["authentication", "md5"]:
                vlink["auth"] = _md5(sub)
            else:
                leaf(vlink["attrs"], sub)
        else:
            leaf(area["attrs"], rest)
    elif r[0] == "interface" and len(r) > 1:
        entry, rest = ospf_iface(model, r[1]), r[2:]
        if not rest:
            # Bare "interface X": the empty node VyOS keeps after its last leaf is deleted.
            # It configures nothing, so it adds nothing to the model.
            pass
        elif rest[0] == "passive":
            entry["passive"] = rest[1:] != ["disable"]
        elif rest[0] == "area" and len(rest) > 1:
            entry["area"] = area_id(rest[1])
            entry["_explicit_area"] = True
        elif rest[:2] == ["authentication", "md5"]:
            entry["auth"] = _md5(rest)
        elif rest[:2] == ["authentication", "plaintext-password"] and len(rest) > 2:
            entry["auth"] = {"type": "plaintext", "key": rest[2]}
        else:
            leaf(entry["attrs"], rest)
    elif r[0] == "default-information" and len(r) > 1 and r[1] == "originate":
        if o["default_originate"] is None:
            o["default_originate"] = {"enabled": True, "always": False, "attrs": {}}
        sub = r[2:]
        if sub == ["always"]:
            o["default_originate"]["always"] = True
        else:
            leaf(o["default_originate"]["attrs"], sub)
    elif r[0] == "redistribute":
        o["redistribute"].append(" ".join(r[1:]))
    else:
        leaf(o["attrs"], r)


def _bgp(model, r):
    if model["bgp"] is None:
        model["bgp"] = {"asn": None, "router_id": None, "neighbors": {}, "networks": [], "attrs": {}}
    b = model["bgp"]
    if not r:
        return
    if r[0] == "system-as" and len(r) > 1:
        b["asn"] = r[1]
    elif r[0] == "parameters" and r[1:2] == ["router-id"] and len(r) > 2:
        b["router_id"] = r[2]
    elif r[0] == "neighbor" and len(r) > 2:
        n = b["neighbors"].setdefault(
            r[1], {"remote_as": None, "import": None, "export": None, "default_originate": False, "attrs": {}})
        rest = r[2:]
        if rest[0] == "remote-as" and len(rest) > 1:
            n["remote_as"] = rest[1]
        elif rest[:1] == ["address-family"] and rest[2:4] in (["route-map", "import"], ["route-map", "export"]) and len(rest) > 4:
            n["import" if rest[3] == "import" else "export"] = rest[4]
        elif rest[:1] == ["address-family"] and rest[2:] == ["default-originate"]:
            n["default_originate"] = True
        else:
            leaf(n["attrs"], rest)
    elif r[:3] == ["address-family", "ipv4-unicast", "network"] and len(r) > 3:
        b["networks"].append(norm_prefix(r[3]))
        leaf(b["attrs"], r[4:])
    else:
        leaf(b["attrs"], r)
