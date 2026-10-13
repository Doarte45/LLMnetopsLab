"""Offline tests for the three-layer verifier.

fixtures/baseline.raw.json is real output collected from the correct lab on 2026-10-06.
Each test injects a fault into a copy of it (by editing the device output the way the fault
would change it) and checks that the expected checks fail and nothing else does.

Run from v2/scripts:  python -m pytest tests -q
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pytest  # noqa: E402

from verifier import layer1, layer2, layer3  # noqa: E402

CONFIGS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "configs")

with open(os.path.join(HERE, "fixtures", "baseline.raw.json"), encoding="utf-8") as f:
    BASELINE = json.load(f)["raw"]
INTENT = layer3.load_intent(CONFIGS)


@pytest.fixture
def raw():
    return copy.deepcopy(BASELINE)


def failing(raw):
    """Run every layer's analysis and return the IDs of checks that didn't pass."""
    checks = layer1.analyze(raw["layer1"])[0] + layer2.analyze(raw["layer2"]) + layer3.analyze(raw["layer3"], INTENT)[0]
    return {c["id"] for c in checks if c["status"] != "pass"}


def l3_diffs(raw, device):
    return layer3.analyze(raw["layer3"], INTENT)[1][device]


def edit_config(raw, device, old, new, keys=("running", "saved")):
    """Replace text in a device's config outputs (running and saved by default)."""
    for key in keys:
        text = raw["layer3"][device][key]
        assert old in text, f"{old!r} not in {device} {key}"
        raw["layer3"][device][key] = text.replace(old, new)


def edit_json(raw, device, key, func, snapshots=(-1, -2)):
    """Apply func to the parsed JSON of a Layer 2 output in the given snapshots."""
    for i in snapshots:
        outputs = raw["layer2"][device]["snapshots"][i]["outputs"]
        data = json.loads(outputs[key])
        func(data)
        outputs[key] = json.dumps(data)


def edit_text(raw, device, key, func, snapshots=(-1, -2)):
    for i in snapshots:
        outputs = raw["layer2"][device]["snapshots"][i]["outputs"]
        outputs[key] = func(outputs[key])


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_baseline_passes_every_check(raw):
    assert failing(raw) == set()


def test_intent_has_no_unparsed_config():
    for name, model in INTENT.items():
        assert not model.get("other"), name
        assert not (model.get("ospf") or {}).get("extra_networks"), name


def test_baseline_check_counts(raw):
    l1, _ = layer1.analyze(raw["layer1"])
    assert len(l1) == 110
    assert sum(c["category"] == "ping_denied" for c in l1) == 32
    assert sum(c["category"] == "ping_internet" for c in l1) == 20


# ---------------------------------------------------------------------------
# Layer 3: candidate faults (config side)
# ---------------------------------------------------------------------------

def test_fault1_virtual_link_points_at_interface_ip(raw):
    edit_config(raw, "Hub-B", "virtual-link 10.255.0.2", "virtual-link 172.16.10.9")
    paths = {(d["kind"], d["path"].split(".auth")[0]) for d in l3_diffs(raw, "Hub-B")["diffs"]}
    assert ("missing", "ospf.virtual_links.10.255.0.2.transit_area") in paths
    assert ("extra", "ospf.virtual_links.172.16.10.9.transit_area") in paths
    assert "L3.config.Hub-B" in failing(raw)


def test_fault2_branch3_filter_drops_ospf(raw):
    raw["layer3"]["Branch-3"]["export"] += "\n/ip firewall filter add action=drop chain=forward protocol=ospf\n"
    diffs = l3_diffs(raw, "Branch-3")["diffs"]
    assert {d["path"] for d in diffs} >= {"firewall[0].protocol", "firewall[0].action"}
    assert all(d["kind"] == "extra" for d in diffs)


def test_fault3_mtu_mismatch(raw):
    edit_config(raw, "Branch-2", "set [ find default-name=ether1 ] disable-running-check=no",
                "set [ find default-name=ether1 ] disable-running-check=no mtu=1400", keys=("export",))
    diffs = l3_diffs(raw, "Branch-2")["diffs"]
    assert [(d["kind"], d["path"], d["live"]) for d in diffs] == [("extra", "interfaces.ether1.mtu", "1400")]


def test_default_mtu_written_out_is_not_a_diff(raw):
    raw["layer3"]["Branch-2"]["export"] += "\n/interface ethernet set [ find default-name=ether1 ] mtu=1500\n"
    assert l3_diffs(raw, "Branch-2")["diffs"] == []


def test_fault4_md5_key_mismatch(raw):
    edit_config(raw, "Hub-A", "interface eth0 authentication md5 key-id 1 md5-key 'LabArea0Key'",
                "interface eth0 authentication md5 key-id 1 md5-key 'LabArea0key'")
    diffs = l3_diffs(raw, "Hub-A")["diffs"]
    assert [(d["kind"], d["path"], d["live"]) for d in diffs] == [("changed", "ospf.interfaces.eth0.auth.key", "LabArea0key")]


def test_fault5_branch4_area_not_stub(raw):
    edit_config(raw, "Branch-4", " type=stub", "", keys=("export",))
    diffs = l3_diffs(raw, "Branch-4")["diffs"]
    assert [(d["path"], d["live"]) for d in diffs] == [("ospf.areas.20.type", "normal")]


def test_fault6_wrong_range(raw):
    edit_config(raw, "Hub-A", "range 10.1.0.0/16", "range 10.1.0.0/24")
    kinds = {(d["kind"], d["path"]) for d in l3_diffs(raw, "Hub-A")["diffs"]}
    assert ("missing", "ospf.areas.10.ranges.10.1.0.0/16.configured") in kinds
    assert ("extra", "ospf.areas.10.ranges.10.1.0.0/24.configured") in kinds


def test_fault7_return_traffic_rule_widened(raw):
    edit_config(raw, "Branch-4", " connection-state=established,related", "", keys=("export",))
    diffs = l3_diffs(raw, "Branch-4")["diffs"]
    assert [(d["kind"], d["path"]) for d in diffs] == [("missing", "firewall[0].connection-state")]


def test_fault8_local_pref_on_wrong_isp(raw):
    edit_config(raw, "HQ-Edge", "set policy route-map ISP-A-IN rule 10 set local-preference '200'",
                "set policy route-map ISP-B-IN rule 10 set local-preference '200'")
    kinds = {(d["kind"], d["path"]) for d in l3_diffs(raw, "HQ-Edge")["diffs"]}
    assert kinds == {("missing", "route_maps.ISP-A-IN.rule 10.set local-preference"),
                     ("extra", "route_maps.ISP-B-IN.rule 10.set local-preference")}


def test_fault9_default_originate_always(raw):
    edit_config(raw, "HQ-Edge", "set protocols ospf default-information originate",
                "set protocols ospf default-information originate always")
    diffs = l3_diffs(raw, "HQ-Edge")["diffs"]
    assert [(d["kind"], d["path"]) for d in diffs] == [("extra", "ospf.default_originate.always")]


def test_fault10_nat_wrong_interface(raw):
    edit_config(raw, "HQ-Edge", "rule 110 outbound-interface name 'eth2'", "rule 110 outbound-interface name 'eth1'")
    diffs = l3_diffs(raw, "HQ-Edge")["diffs"]
    assert [(d["path"], d["live"]) for d in diffs] == [("nat.source rule 110.outbound-interface name", "eth1")]


def test_fault11_lan_not_passive(raw):
    edit_config(raw, "Branch-2", "networks=10.1.2.0/24 passive", "networks=10.1.2.0/24", keys=("export",))
    assert [(d["kind"], d["path"]) for d in l3_diffs(raw, "Branch-2")["diffs"]] == [
        ("missing", "ospf.interfaces.ether2.passive")]


def test_fault12_host_wrong_mask(raw):
    host = raw["layer3"]["B1-PC2"]
    host["interfaces"] = host["interfaces"].replace("10.1.1.11/24", "10.1.1.11/16")
    host["addr"] = host["addr"].replace("10.1.1.11/24", "10.1.1.11/16")
    host["route"] = host["route"].replace("10.1.1.0/24 dev eth0", "10.1.0.0/16 dev eth0")
    ids = failing(raw)
    assert "L3.config.B1-PC2" in ids and "L3.persisted.B1-PC2" not in ids


# ---------------------------------------------------------------------------
# Layer 3: equivalence, workarounds, persistence, FRR sync
# ---------------------------------------------------------------------------

def test_equivalent_routeros_template_syntax(raw):
    edit_config(raw, "Branch-1", "networks=172.16.10.0/30", "interfaces=ether1", keys=("export",))
    assert l3_diffs(raw, "Branch-1")["diffs"] == []


def test_equivalent_vyos_interface_area(raw):
    edit_config(raw, "HQ-Edge", "set protocols ospf area 0 network '172.16.1.0/30'",
                "set protocols ospf interface eth0 area '0'")
    assert l3_diffs(raw, "HQ-Edge")["diffs"] == []


def test_empty_vyos_ospf_interface_node_is_not_a_diff(raw):
    # Deleting the only leaf under "protocols ospf interface X" leaves a bare node (v1 Branch2)
    for device in ("HQ-Edge", "Hub-A"):
        for key in ("running", "saved"):
            raw["layer3"][device][key] += "\nset protocols ospf interface eth1"
        assert l3_diffs(raw, device)["diffs"] == []
    assert not {i for i in failing(raw) if i.startswith("L3.")}


def test_workaround_static_route(raw):
    raw["layer3"]["Branch-1"]["export"] += "\n/ip route add dst-address=10.2.0.0/16 gateway=172.16.10.1\n"
    assert [d["kind"] for d in l3_diffs(raw, "Branch-1")["diffs"]] == ["extra"]


def test_dhcp_client_back_on_branch(raw):
    raw["layer3"]["Branch-3"]["export"] += "\n/ip dhcp-client add interface=ether1\n"
    assert [d["path"] for d in l3_diffs(raw, "Branch-3")["diffs"]] == ["dhcp_clients[interface=ether1]"]


def test_unsaved_vyos_change(raw):
    edit_config(raw, "HQ-Edge", "default-information originate", "default-information originate always",
                keys=("running", "frr"))
    ids = failing(raw)
    assert {"L3.config.HQ-Edge", "L3.persisted.HQ-Edge"} <= ids
    assert "L3.frr_sync.HQ-Edge" not in ids


def test_commit_that_never_reached_frr(raw):
    edit_config(raw, "Hub-A", "range 10.1.0.0/16", "range 10.1.0.0/24")
    details = l3_diffs(raw, "Hub-A")
    assert {d["item"] for d in details["frr"]} == {"10 10.1.0.0/16", "10 10.1.0.0/24"}
    assert "L3.frr_sync.Hub-A" in failing(raw)


def test_alpine_change_not_in_interfaces_file(raw):
    host = raw["layer3"]["B2-PC1"]
    host["route"] = host["route"].replace("default via 10.1.2.1", "default via 10.1.2.254")
    assert {"L3.config.B2-PC1", "L3.persisted.B2-PC1"} <= failing(raw)


def test_unreachable_device_is_error_not_fail(raw):
    raw["layer3"]["Hub-A"] = {"error": "NetmikoTimeoutException: timed out"}
    checks, _ = layer3.analyze(raw["layer3"], INTENT)
    hub_a = [c for c in checks if c["device"] == "Hub-A"]
    assert len(hub_a) == 3 and all(c["status"] == "error" for c in hub_a)


# ---------------------------------------------------------------------------
# Layer 2
# ---------------------------------------------------------------------------

def _drop_vlink(data):
    for rid in list(data["neighbors"]):
        data["neighbors"][rid] = [n for n in data["neighbors"][rid] if not n["ifaceName"].startswith("VLINK")]
        if not data["neighbors"][rid]:
            del data["neighbors"][rid]


def test_virtual_link_down(raw):
    edit_json(raw, "Hub-A", "neighbors", _drop_vlink)
    edit_json(raw, "Hub-B", "neighbors", _drop_vlink)
    ids = {i for i in failing(raw) if i.startswith("L2.")}
    assert ids == {"L2.ospf_adjacency.Hub-A--Hub-B.vlink"}


def test_adjacency_stuck_in_exstart(raw):
    def stuck(text):
        return text.replace('state="Full"', 'state="ExStart"').replace("state=Full", "state=ExStart")
    edit_text(raw, "Branch-2", "neighbors", stuck)
    assert "L2.ospf_adjacency.Hub-A--Branch-2" in failing(raw)


def test_flapping_adjacency(raw):
    def bump(data):
        for entries in data["neighbors"].values():
            for n in entries:
                n["stateChangeCounter"] += 4
    edit_json(raw, "HQ-Edge", "neighbors", bump, snapshots=(-1,))
    ids = {i for i in failing(raw) if i.startswith("L2.")}
    assert ids == {"L2.ospf_stable.HQ-Edge"}


def test_area10_lans_leak_into_area0(raw):
    def leak(data):
        data["10.1.1.0/24"] = [{"protocol": "ospf", "selected": True, "nexthops": [{"ip": "172.16.1.2"}]}]
    edit_json(raw, "HQ-Edge", "routes", leak)
    assert {i for i in failing(raw) if i.startswith("L2.")} == {"L2.summaries.HQ-Edge"}


def test_isp_b_default_preferred(raw):
    def swap(data):
        for path in data["routes"]["0.0.0.0/0"]:
            path["bestpath"] = not path.get("bestpath", False)
            if not path["bestpath"]:
                del path["bestpath"]
    edit_json(raw, "HQ-Edge", "bgp_table", swap)
    ids = {i for i in failing(raw) if i.startswith("L2.")}
    assert ids == {"L2.bgp_best.0.0.0.0/0", "L2.bgp_backup_default"}


def test_transit_leak_to_isp_b(raw):
    def leak(data):
        data["routes"]["8.8.8.8/32"] = [{"valid": True, "bestpath": True, "path": "65000 65100",
                                         "nexthops": [{"ip": "203.0.113.6"}], "peerId": "203.0.113.6"}]
    edit_json(raw, "ISP-B", "bgp_table", leak, snapshots=(-1,))
    assert {i for i in failing(raw) if i.startswith("L2.")} == {"L2.isp_no_transit.ISP-B"}


def test_stub_area_receives_inter_area_routes(raw):
    line = " 9 ADo dst-address=10.1.1.0/24 routing-table=main gateway=172.16.20.5 distance=110"
    edit_text(raw, "Branch-4", "routes", lambda t: t + "\n" + line + "\n")
    assert {i for i in failing(raw) if i.startswith("L2.")} == {"L2.stub_area.Branch-4"}


def test_missing_default_route_on_branch(raw):
    def drop(text):
        return "\n".join(l for l in text.splitlines() if "dst-address=0.0.0.0/0" not in l)
    edit_text(raw, "Branch-1", "routes", drop)
    assert {i for i in failing(raw) if i.startswith("L2.")} == {"L2.default_route.Branch-1"}


def test_unreachable_router_gives_errors(raw):
    raw["layer2"]["Hub-A"] = {"error": "NetmikoTimeoutException: timed out"}
    checks = layer2.analyze(raw["layer2"])
    statuses = {c["id"]: c["status"] for c in checks if "Hub-A" in c["id"]}
    assert statuses and set(statuses.values()) == {"error"}


# ---------------------------------------------------------------------------
# Layer 1
# ---------------------------------------------------------------------------

def test_guest_policy_hole(raw):
    raw["layer1"]["B4-PC3"]["first"]["10.1.1.10"] = True
    assert {i for i in failing(raw) if i.startswith("L1.")} == {"L1.ping.B4-PC3->B1-PC1"}


def test_flaky_vs_hard_failure(raw):
    raw["layer1"]["B1-PC1"]["first"]["1.1.1.1"] = False
    raw["layer1"]["B1-PC1"]["first"]["8.8.8.8"] = False
    raw["layer1"]["B1-PC1"]["retry"] = {"1.1.1.1": True, "8.8.8.8": False}
    checks, matrix = layer1.analyze(raw["layer1"])
    assert matrix["B1-PC1"]["1.1.1.1"] == "flaky"
    assert matrix["B1-PC1"]["8.8.8.8"] == "fail"


def test_ping_output_parser_ignores_echoed_command():
    script = layer1.ping_script(["10.1.1.10", "8.8.8.8"])
    output = script[:60] + "\n" + script[60:] + "\nPING 10.1.1.10 ok\nPING 8.8.8.8 fail\n"
    assert layer1.parse_ping_output(output) == {"10.1.1.10": True, "8.8.8.8": False}
