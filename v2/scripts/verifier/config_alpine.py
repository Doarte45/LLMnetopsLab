"""Parse Alpine host networking (/etc/network/interfaces, ip addr, ip route) into a host model.

Hosts use their own model shape: the interfaces file (what persists) and the runtime state
(what is applied). Intent derives the runtime from the file, so comparing a live host to the
intent checks both, and comparing a live host's file to its own runtime checks persistence.
"""

import ipaddress
import re


def parse_interfaces_file(text):
    """Return {iface: stanza} for every iface except lo."""
    stanzas, auto, current = {}, set(), None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if tokens[0] in ("auto", "allow-hotplug"):
            auto.update(tokens[1:])
            current = None
        elif tokens[0] == "iface" and len(tokens) > 1:
            current = stanzas.setdefault(tokens[1], {"method": tokens[3] if len(tokens) > 3 else None, "attrs": {}})
        elif current is not None:
            current["attrs"][tokens[0]] = " ".join(tokens[1:])
    out = {}
    for name, stanza in stanzas.items():
        if name == "lo":
            continue
        attrs = dict(stanza["attrs"])
        address = attrs.pop("address", None)
        netmask = attrs.pop("netmask", None)
        if address and netmask and "/" not in address:
            address = str(ipaddress.ip_interface(f"{address}/{netmask}"))
        out[name] = {
            "auto": name in auto,
            "method": stanza["method"],
            "address": address,
            "gateway": attrs.pop("gateway", None),
            "attrs": attrs,
        }
    return out


def runtime_from_file(stanzas):
    """The addresses and routes the interfaces file should produce once applied."""
    addresses, routes = {}, []
    for name, s in stanzas.items():
        if s["method"] != "static" or not s["address"]:
            continue
        addresses[name] = [s["address"]]
        routes.append(f"{ipaddress.ip_interface(s['address']).network} dev {name}")
        if s["gateway"]:
            routes.append(f"default via {s['gateway']} dev {name}")
    return {"addresses": addresses, "routes": sorted(routes)}


def parse_ip_addr(text):
    """Parse "ip -4 -o addr show" into {iface: [cidr]}, skipping lo."""
    addresses = {}
    for line in text.splitlines():
        match = re.match(r"\s*\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if match and match.group(1) != "lo":
            addresses.setdefault(match.group(1), []).append(match.group(2))
    return addresses


def parse_ip_route(text):
    """Parse "ip -4 route show" into normalized "dst [via gw] dev iface" strings."""
    routes = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or "dev" not in tokens:
            continue
        dst = tokens[0]
        dev = tokens[tokens.index("dev") + 1]
        via = tokens[tokens.index("via") + 1] if "via" in tokens else None
        routes.append(f"{dst} via {via} dev {dev}" if via else f"{dst} dev {dev}")
    return sorted(routes)


def build_model(hostname, stanzas, runtime):
    return {"hostname": hostname, "interfaces_file": stanzas, "runtime": runtime}


def intent_from_scripts(texts):
    """Build the intent model from the repo scripts (base script, then the CCAccess script).

    The base script writes /etc/network/interfaces from a heredoc; the access script appends
    the eth3 stanza from another heredoc.
    """
    file_text, hostname = "", None
    for text in texts:
        file_text += _heredoc(text) + "\n"
        match = re.search(r"setup-hostname\s+(\S+)", text)
        if match:
            hostname = match.group(1)
    stanzas = parse_interfaces_file(file_text)
    return build_model(hostname, stanzas, runtime_from_file(stanzas))


def _heredoc(text):
    match = re.search(r"<<\s*'?(\w+)'?\s*\n(.*?)\n\1\s*$", text, re.S | re.M)
    return match.group(2) if match else ""
