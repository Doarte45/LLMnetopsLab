"""Parse RouterOS 7 scripts (repo .rsc files or "/export terse") into the model."""

import ipaddress
import re
import shlex

from .model import (
    area_id, iface, leaf, new_model, new_ospf_area, norm_address, norm_mtu, norm_prefix,
    ospf, ospf_iface,
)

VERBS = {"add", "set", "remove"}
# Sections where anything unmodeled is reported as extra config. Everything else
# (/system, /user, /ip service, /ip ssh, /tool ...) is management or platform detail.
RELEVANT = ("/ip address", "/ip route", "/ip dhcp-client", "/ip dhcp-server", "/ip pool",
            "/ip firewall", "/ip arp", "/routing", "/interface")
IGNORED = ("/interface list",)
# Ethernet settings that are VM detail rather than lab config
IGNORED_ETHERNET_ATTRS = {"disable-running-check", "l2mtu", "mac-address", "orig-mac-address"}
TEMPLATE_KEYS = {"area", "networks", "interfaces", "passive", "auth", "auth-id", "auth-key", "instance"}
INSTANCE_IGNORED = {"name", "version", "vrf", "routing-table"}

AREA_TYPES = {"default": "normal", "stub": "stub", "nssa": "nssa"}


def parse_export(text):
    model = new_model()
    templates, area_names = [], {}
    for line in _logical_lines(text):
        parsed = _split(line)
        if parsed is None:
            continue
        path, verb, selector, attrs = parsed
        _apply(model, path, verb, selector, attrs, templates, area_names)
    _finalize_ospf(model, templates, area_names)
    return model


def _logical_lines(text):
    """Join backslash continuations and drop comments."""
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1].strip() + " "
            continue
        line = (buf + line.strip()).strip()
        buf = ""
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _split(line):
    """Split "/path verb [ find sel ] k=v flag" into (path, verb, selector, attrs)."""
    if not line.startswith("/"):
        return None
    selector = None
    match = re.search(r"\[(.*?)\]", line)
    if match:
        selector = match.group(1).strip()
        line = line[:match.start()] + line[match.end():]
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    path_parts, verb, rest = [], None, []
    for i, tok in enumerate(tokens):
        if tok in VERBS:
            verb, rest = tok, tokens[i + 1:]
            break
        path_parts.append(tok)
    if verb is None:
        return None
    attrs = {}
    for tok in rest:
        if "=" in tok:
            key, value = tok.split("=", 1)
            attrs[key] = value
        else:
            attrs[tok] = True
    attrs.pop("comment", None)
    if attrs.get("disabled") == "no":
        del attrs["disabled"]
    for key, value in attrs.items():
        if isinstance(value, str) and "," in value:
            attrs[key] = ",".join(sorted(value.split(",")))
    return " ".join(path_parts), verb, selector, attrs


def _selector_name(selector):
    if not selector:
        return None
    match = re.search(r"(?:default-name|name)=(\S+)", selector)
    return match.group(1).strip('"') if match else None


def _attr_line(path, verb, attrs):
    return " ".join([path, verb] + [k if v is True else f"{k}={v}" for k, v in sorted(attrs.items())])


def _apply(model, path, verb, selector, attrs, templates, area_names):
    if path == "/system identity":
        if "name" in attrs:
            model["hostname"] = attrs["name"]
        return
    if not path.startswith(RELEVANT) or path.startswith(IGNORED):
        return

    if path == "/ip address" and verb == "add":
        intf = iface(model, attrs.get("interface", "?"))
        address = norm_address(attrs.get("address", "?"))
        intf["addresses"].append(address + (" disabled" if attrs.get("disabled") == "yes" else ""))
        for key, value in attrs.items():
            if key not in ("address", "interface", "network", "disabled"):
                intf["attrs"][f"address {address} {key}"] = value
    elif path == "/interface ethernet" and verb == "set":
        name = _selector_name(selector) or attrs.get("name", "?")
        intf = iface(model, name)
        for key, value in attrs.items():
            if key in IGNORED_ETHERNET_ATTRS:
                continue
            if key == "mtu":
                intf["mtu"] = norm_mtu(value)
            elif key == "disabled":
                intf["disabled"] = value == "yes"
            else:
                intf["attrs"][key] = value
    elif path == "/routing ospf instance" and verb == "add":
        o = ospf(model)
        o["router_id"] = attrs.get("router-id")
        originate = attrs.get("originate-default", "never")
        if originate != "never":
            o["default_originate"] = {"enabled": True, "always": originate == "always", "attrs": {}}
        if attrs.get("redistribute"):
            o["redistribute"].extend(attrs["redistribute"].split(","))
        for key, value in attrs.items():
            if key not in INSTANCE_IGNORED | {"router-id", "originate-default", "redistribute"}:
                o["attrs"][f"instance {key}"] = value
    elif path == "/routing ospf area" and verb == "add":
        aid = area_id(attrs.get("area-id", "0.0.0.0"))
        area_names[attrs.get("name", aid)] = aid
        area = ospf(model)["areas"].setdefault(aid, new_ospf_area())
        area["type"] = AREA_TYPES.get(attrs.get("type", "default"), attrs.get("type"))
        area["no_summary"] = attrs.get("no-summaries") in (True, "yes")
        for key, value in attrs.items():
            if key not in ("name", "area-id", "instance", "type", "no-summaries"):
                area["attrs"][key] = value
    elif path == "/routing ospf area range" and verb == "add":
        aid = area_names.get(attrs.get("area"), attrs.get("area"))
        area = ospf(model)["areas"].setdefault(aid, new_ospf_area())
        rng = area["ranges"].setdefault(norm_prefix(attrs.get("prefix", "?")), {"configured": True})
        for key, value in attrs.items():
            if key not in ("area", "prefix", "instance"):
                rng[key] = value
    elif path == "/routing ospf interface-template" and verb == "add":
        templates.append(attrs)
    elif path == "/ip route" and verb == "add":
        model["static_routes"].append(_attr_line("", "", attrs).strip())
    elif path == "/ip dhcp-client":
        if verb == "remove":
            model["dhcp_clients"] = []
        elif verb == "add":
            model["dhcp_clients"].append(_attr_line("", "", attrs).strip())
    elif path.startswith("/ip firewall ") and path.split()[-1] in ("filter", "nat", "mangle", "raw") and verb == "add":
        model["firewall"].append({"table": path.split()[-1], **attrs})
    else:
        if verb != "remove":
            model["other"].append(_attr_line(path, verb, attrs) + (f" [{selector}]" if selector else ""))


def _finalize_ospf(model, templates, area_names):
    """Apply interface-templates to the router's interfaces (first matching template wins)."""
    if model["ospf"] is None:
        return
    extra = []
    for t in templates:
        if t.get("disabled") == "yes":
            continue
        aid = area_names.get(t.get("area"), t.get("area"))
        nets = [norm_prefix(n) for n in str(t.get("networks", "")).split(",") if n]
        names = [n for n in str(t.get("interfaces", "")).split(",") if n]
        matched = False
        for name, intf in model["interfaces"].items():
            hit = name in names
            for addr in intf["addresses"]:
                try:
                    ip = ipaddress.ip_interface(addr.split()[0]).ip
                except ValueError:
                    continue
                if any(ip in ipaddress.ip_network(n, strict=False) for n in nets if "/" in n):
                    hit = True
            if not hit:
                continue
            matched = True
            entry = ospf_iface(model, name)
            if entry["area"] is not None:
                continue
            entry["area"] = aid
            entry["passive"] = t.get("passive") in (True, "yes")
            if t.get("auth"):
                entry["auth"] = {"type": t["auth"], "key_id": t.get("auth-id"), "key": t.get("auth-key")}
            for key, value in t.items():
                if key not in TEMPLATE_KEYS and key != "disabled":
                    leaf(entry["attrs"], [key, value] if value is not True else [key])
        if not matched:
            extra.append(" ".join(nets + names) + f" area {aid}")
    model["ospf"]["extra_networks"] = sorted(extra)
