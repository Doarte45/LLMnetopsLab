"""Vendor-neutral config model and the diff used by Layer 3.

Every platform parser fills the same structure, so equivalent syntax (a VyOS network
statement vs. an interface area, a MikroTik template covering two LANs) compares equal.

Conventions that keep the diff free of noise:
- None, False, empty strings and empty containers count as "not configured", so a default
  written out explicitly matches one that is simply absent.
- Lists of scalars are sets; lists of dicts (firewall rules) are ordered.
- Keys starting with "_" are parser bookkeeping and are never diffed.
"""

import ipaddress

DEFAULT_MTU = "1500"


def new_model():
    return {
        "hostname": None,
        "interfaces": {},
        "ospf": None,
        "bgp": None,
        "route_maps": {},
        "static_routes": [],
        "dhcp_clients": [],
        "nat": {},
        "firewall": [],
        # Config in a lab-relevant section that the model doesn't break down further.
        # Intent has none, so anything here is extra.
        "other": [],
    }


def new_interface():
    return {"addresses": [], "mtu": None, "disabled": False, "attrs": {}}


def new_ospf():
    return {
        "router_id": None,
        "areas": {},
        "interfaces": {},
        "virtual_links": {},
        "default_originate": None,
        "redistribute": [],
        "extra_networks": [],
        "attrs": {},
    }


def new_ospf_area():
    return {"type": "normal", "no_summary": False, "ranges": {}, "auth": None, "attrs": {}}


def new_ospf_interface():
    return {"area": None, "passive": False, "auth": None, "attrs": {}, "_explicit_area": False}


def iface(model, name):
    return model["interfaces"].setdefault(name, new_interface())


def ospf(model):
    if model["ospf"] is None:
        model["ospf"] = new_ospf()
    return model["ospf"]


def ospf_area(model, aid):
    return ospf(model)["areas"].setdefault(aid, new_ospf_area())


def ospf_iface(model, name):
    return ospf(model)["interfaces"].setdefault(name, new_ospf_interface())


def area_id(value):
    """Normalize an OSPF area ID ("10", "0.0.0.10") to its decimal string."""
    value = str(value).strip()
    if "." in value:
        return str(int(ipaddress.IPv4Address(value)))
    return str(int(value))


def norm_prefix(value):
    """Normalize a prefix to network/len (host bits cleared). Returns the input if it isn't one."""
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return value


def norm_address(value):
    """Normalize an interface address, keeping its host bits."""
    try:
        return str(ipaddress.ip_interface(value))
    except ValueError:
        return value


def norm_mtu(value):
    value = str(value)
    return None if value == DEFAULT_MTU else value


def leaf(target, tokens):
    """Store a generic config path: last token is the value, a lone token is a flag."""
    if not tokens:
        return
    if len(tokens) == 1:
        target[tokens[0]] = True
    else:
        target[" ".join(tokens[:-1])] = tokens[-1]


def assign_ospf_areas(model, networks):
    """Turn (area, prefix) network statements into per-interface areas.

    Interfaces with an explicit area keep it. Statements that cover no interface address
    are recorded as extra_networks (dead or leftover config).
    """
    o = model["ospf"]
    if o is None:
        return
    used = set()
    for name, intf in model["interfaces"].items():
        for addr in intf["addresses"]:
            try:
                ip = ipaddress.ip_interface(addr.split()[0]).ip
            except ValueError:
                continue
            for aid, prefix in networks:
                try:
                    covered = ip in ipaddress.ip_network(prefix, strict=False)
                except ValueError:
                    continue
                if covered:
                    used.add((aid, prefix))
                    entry = ospf_iface(model, name)
                    if entry["area"] is None:
                        entry["area"] = aid
    o["extra_networks"] = sorted(f"{prefix} area {aid}" for aid, prefix in networks if (aid, prefix) not in used)


def flatten(obj, prefix=""):
    """Flatten a model into {path: value}, following the conventions in the module docstring."""
    out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            key = str(key)
            if key.startswith("_"):
                continue
            out.update(flatten(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(obj, list):
        if all(not isinstance(item, (dict, list)) for item in obj):
            for item in obj:
                out[f"{prefix}[{item}]"] = True
        else:
            for n, item in enumerate(obj):
                out.update(flatten(item, f"{prefix}[{n}]"))
    elif obj is None or obj is False or obj == "":
        pass
    else:
        out[prefix] = obj
    return out


def diff_models(intent, live):
    """Compare two models in both directions.

    missing: in the intent but not on the device (fault not fixed)
    extra:   on the device but not in the intent (workaround or leftover)
    changed: present on both with different values
    """
    a, b = flatten(intent), flatten(live)
    diffs = []
    for path in sorted(set(a) | set(b)):
        if path not in b:
            diffs.append({"path": path, "kind": "missing", "intent": a[path], "live": None})
        elif path not in a:
            diffs.append({"path": path, "kind": "extra", "intent": None, "live": b[path]})
        elif a[path] != b[path]:
            diffs.append({"path": path, "kind": "changed", "intent": a[path], "live": b[path]})
    return diffs
