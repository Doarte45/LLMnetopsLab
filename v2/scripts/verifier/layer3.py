"""Layer 3: config intent.

Each device's live config is parsed into the vendor-neutral model and diffed in both
directions against the intent model built from v2/configs (base config + CCAccess script):
missing items mean a fault wasn't fixed, extra items mean a workaround or leftover.

Persistence is checked per platform:
- VyOS: running config vs. /config/config.boot, both parsed into the model
- RouterOS: changes persist on their own, nothing to check
- Alpine: /etc/network/interfaces vs. the live addresses and routes
VyOS routers also get a check that the committed config reached FRR (config_frr.py).
"""

import os

from .checks import check, passfail
from .config_alpine import (
    build_model, intent_from_scripts, parse_interfaces_file, parse_ip_addr, parse_ip_route, runtime_from_file,
)
from .config_frr import diff_views, parse_frr_config, view_from_model
from .config_routeros import parse_export
from .config_vyos import parse_set_commands
from .devices import DEVICE_BY_NAME, DEVICES, connect, run_commands, run_parallel
from .model import diff_models

# Saved config rendered as set-commands with VyOS's own config parser
VYOS_SAVED = ("python3 -c 'from vyos.configtree import ConfigTree; "
              "print(ConfigTree(open(\"/config/config.boot\").read()).to_commands())'")

COMMANDS = {
    "vyos": {
        "running": "show configuration commands",
        "saved": VYOS_SAVED,
        "frr": "vtysh -c 'show running-config'",
    },
    "routeros": {"export": "/export terse"},
    "alpine": {
        "hostname": "hostname",
        "interfaces": "cat /etc/network/interfaces",
        "addr": "ip -4 -o addr show",
        "route": "ip -4 route show",
    },
}

CONFIG_FILES = {
    "vyos": ("VyOS/{name}.set", "VyOS/CCAccess/{name}-access.set"),
    "routeros": ("Mikrotik/{name}.rsc", "Mikrotik/CCAccess/{name}-access.rsc"),
    "alpine": ("Alpine/{name}.sh", "Alpine/CCAccess/{name}-access.sh"),
}


def config_texts(configs_dir, name, platform):
    """Return [base config, CCAccess script] for a device as text."""
    texts = []
    for pattern in CONFIG_FILES[platform]:
        with open(os.path.join(configs_dir, pattern.format(name=name)), encoding="utf-8") as f:
            texts.append(f.read())
    return texts


def model_from_texts(platform, texts):
    """Parse a device's config scripts (base config, then CCAccess script) into the model."""
    if platform == "vyos":
        return parse_set_commands("\n".join(texts))
    if platform == "routeros":
        return parse_export("\n".join(texts))
    return intent_from_scripts(texts)


def load_intent(configs_dir):
    """Build the intent model for every device from the repo configs."""
    return {d["name"]: model_from_texts(d["platform"], config_texts(configs_dir, d["name"], d["platform"]))
            for d in DEVICES}


def _strip_secrets(text):
    """Drop login password lines so saved raw output carries no hashes."""
    return "\n".join(line for line in text.splitlines() if "-password" not in line)


def collect(devices=None):
    """Return {device: {key: raw output}} for the named devices (default: all)."""
    def one(device):
        conn = connect(device)
        try:
            outputs = run_commands(conn, COMMANDS[device["platform"]])
            return {key: _strip_secrets(text) for key, text in outputs.items()}
        finally:
            conn.disconnect()

    return run_parallel(one, [DEVICE_BY_NAME[n] for n in devices] if devices else DEVICES)


def live_model(platform, outputs):
    if platform == "vyos":
        return parse_set_commands(outputs["running"])
    if platform == "routeros":
        return parse_export(outputs["export"])
    stanzas = parse_interfaces_file(outputs["interfaces"])
    runtime = {"addresses": parse_ip_addr(outputs["addr"]), "routes": parse_ip_route(outputs["route"])}
    return build_model(outputs["hostname"].strip(), stanzas, runtime)


def analyze(raw, intent):
    """Return (checks, details). details[device] holds the diffs behind each check."""
    checks, details = [], {}
    for device in DEVICES:
        name, platform = device["name"], device["platform"]
        outputs = raw.get(name, {"error": "not collected"})
        ids = [f"L3.config.{name}", f"L3.persisted.{name}"] + ([f"L3.frr_sync.{name}"] if platform == "vyos" else [])
        if "error" in outputs:
            checks += [check(3, cid, "error", detail=outputs["error"], device=name) for cid in ids]
            details[name] = {"error": outputs["error"]}
            continue

        model = live_model(platform, outputs)
        diffs = diff_models(intent[name], model)
        details[name] = {"diffs": diffs}
        summary = {kind: sum(1 for d in diffs if d["kind"] == kind) for kind in ("missing", "extra", "changed")}
        checks.append(check(3, ids[0], passfail(not diffs), "matches intent", summary, device=name))

        if platform == "vyos":
            saved_diffs = diff_models(model, parse_set_commands(outputs["saved"])) if "set " in outputs["saved"] else None
            frr_diffs = diff_views(view_from_model(model), parse_frr_config(outputs["frr"]))
            details[name]["unsaved"] = saved_diffs
            details[name]["frr"] = frr_diffs
            if saved_diffs is None:
                checks.append(check(3, ids[1], "error", detail="could not read the saved config: "
                                    + outputs["saved"].strip()[:200], device=name))
            else:
                checks.append(check(3, ids[1], passfail(not saved_diffs), "running == saved",
                                    f"{len(saved_diffs)} differences", device=name))
            checks.append(check(3, ids[2], passfail(not frr_diffs), "VyOS config rendered in FRR",
                                f"{len(frr_diffs)} differences", device=name))
        elif platform == "alpine":
            unapplied = diff_models(runtime_from_file(model["interfaces_file"]), model["runtime"])
            details[name]["unsaved"] = unapplied
            checks.append(check(3, ids[1], passfail(not unapplied), "interfaces file == live state",
                                f"{len(unapplied)} differences", device=name))
        else:
            checks.append(check(3, ids[1], "pass", detail="RouterOS persists every change", device=name))
    return checks, details
