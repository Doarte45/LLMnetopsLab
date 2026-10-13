"""The 12 v2 faults (planning/faults-and-verifier.md, "Candidate fault set").

Each fault is defined once, three ways that must agree:
- edits:  text replacements on the device's base config. Applied together they produce the
          broken config files in configs/<Platform>/broken/ (tests/test_faults.py checks this).
- inject/revert: the live commands that add and remove the fault on a running lab.
- l3_diff: the Layer 3 differences the fault leaves on its device, as (kind, path). The harness
          checks the live device against the diff computed from the edits, so a command that
          doesn't do what the broken config says is caught on read-back.

expect and may are check-ID globs (fnmatch, case-sensitive) for the verifier run with the
fault injected. Every check matching an expect glob must fail; checks matching a may glob can
go either way; any other failing check is unexpected. These start as predictions and should be
tightened from the measured runs (fault_lab.py regrade re-grades saved runs offline).

kick: op-mode commands run after inject and revert to make the lab notice a change without
changing config (e.g. an OSPF MTU mismatch is only checked when the adjacency re-forms).
settle / revert_settle: seconds to wait before verifying. OSPF faults use more than the 40 s
dead interval, so the verifier never sees an adjacency that is down but not yet timed out.
"""

import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from verifier.devices import DEVICE_BY_NAME
from verifier.layer3 import CONFIG_FILES, config_texts, model_from_texts
from verifier.model import diff_models

CONFIGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "configs")
BROKEN_FILES = {
    "vyos": "VyOS/broken/{name}-broken.set",
    "routeros": "Mikrotik/broken/{name}-broken.rsc",
    "alpine": "Alpine/broken/{name}-broken.sh",
}

OSPF_SETTLE = 50
OSPF_REVERT_SETTLE = 60


@dataclass
class Fault:
    num: int
    device: str
    title: str
    edits: list
    inject: list
    revert: list
    l3_diff: list
    expect: list
    may: list = field(default_factory=list)
    kick: list = field(default_factory=list)
    settle: int = 5
    revert_settle: int = 5

    @property
    def platform(self):
        return DEVICE_BY_NAME[self.device]["platform"]


# ---------------------------------------------------------------------------
# Check groups shared by several faults
# ---------------------------------------------------------------------------

# Area 20 cut off: B4-PC1/2 lose every non-area-20 host (B4-PC3/4 are guest, so their pairs
# with non-guest hosts are expected-deny and stay blocked) and all four B4 hosts lose the internet.
B4_CUT_OFF = [
    "L1.ping.B[123]-PC?->B4-PC[12]",
    "L1.ping.B4-PC[12]->B[123]-PC?",
    "L1.ping.B4-PC?->8.8.8.8",
    "L1.ping.B4-PC?->1.1.1.1",
]
# Branch-2's LAN cut off (guest pairs stay blocked either way)
B2_CUT_OFF = [
    "L1.ping.B2-PC?->B[13]-PC?",
    "L1.ping.B2-PC?->B4-PC[12]",
    "L1.ping.B[13]-PC?->B2-PC?",
    "L1.ping.B4-PC[12]->B2-PC?",
    "L1.ping.B2-PC?->8.8.8.8",
    "L1.ping.B2-PC?->1.1.1.1",
]
# The 32 expected-deny pairs now get through
GUEST_HOLES = [
    "L1.ping.B4-PC[34]->B[123]-PC?",
    "L1.ping.B4-PC[34]->B4-PC[12]",
    "L1.ping.B[123]-PC?->B4-PC[34]",
    "L1.ping.B4-PC[12]->B4-PC[34]",
]
NO_INTERNET = ["L1.ping.*->8.8.8.8", "L1.ping.*->1.1.1.1"]
# Routing-table knock-on effects of a lost adjacency; how far they reach is to be measured
ROUTING_FALLOUT = ["L2.summaries.*", "L2.route_coverage.*", "L2.ospf_route_type.*"]

# Virtual link down (measured with #2, results/faults/dev/run_003): Hub-B still summarizes area 20
# into area 10, so Branch-3, which reaches Hub-B directly, keeps B3 <-> B4 working. Traffic from
# Branch-1/2 goes through Hub-A, and Hub-A as an ABR ignores summaries learned from a
# non-backbone area, so it has no route to area 20. Area 0 loses 10.2.0.0/16.
VLINK_DOWN_EXPECT = [
    "L2.ospf_adjacency.Hub-A--Hub-B.vlink",
    "L2.summaries.HQ-Edge", "L2.summaries.Hub-A", "L2.route_coverage.HQ-Edge", "L2.route_coverage.Hub-A",
    "L2.ospf_route_type.HQ-Edge.10.2.0.0/16", "L2.ospf_route_type.Hub-A.10.2.0.0/16",
    "L1.ping.B[12]-PC?->B4-PC[12]",
    "L1.ping.B4-PC[12]->B[12]-PC?",
    "L1.ping.B4-PC?->8.8.8.8",
    "L1.ping.B4-PC?->1.1.1.1",
]
VLINK_DOWN_MAY = ROUTING_FALLOUT + ["L2.default_route.Hub-B", "L2.default_route.Branch-4", "L2.stub_area.Branch-4"]
# With #1 the virtual link points at a router ID that doesn't exist, so Hub-B's VLINK interface
# never comes up and Hub-B stops acting as an ABR: no area 20 summary into area 10 and no default
# into area 20. That cuts area 20 off completely, Branch-3 included (measured, run_004). With #2
# Hub-A is still reachable through area 10, the VLINK interface is up (only the neighbor never
# forms), and Hub-B keeps summarizing.
VLINK_ENDPOINT_MISSING_EXPECT = VLINK_DOWN_EXPECT + B4_CUT_OFF + [
    "L2.summaries.Branch-[123]", "L2.route_coverage.Branch-[1234]", "L2.default_route.Branch-4",
]


FAULTS = [
    Fault(
        1, "Hub-B", "Virtual link points at Hub-A's interface IP (172.16.10.9) instead of its router ID",
        edits=[("virtual-link 10.255.0.2", "virtual-link 172.16.10.9")],
        inject=[
            "delete protocols ospf area 10 virtual-link 10.255.0.2",
            "set protocols ospf area 10 virtual-link 172.16.10.9 authentication md5 key-id 1 md5-key 'LabArea0Key'",
        ],
        revert=[
            "delete protocols ospf area 10 virtual-link 172.16.10.9",
            "set protocols ospf area 10 virtual-link 10.255.0.2 authentication md5 key-id 1 md5-key 'LabArea0Key'",
        ],
        l3_diff=[
            ("missing", "ospf.virtual_links.10.255.0.2.auth.key"),
            ("missing", "ospf.virtual_links.10.255.0.2.auth.key_id"),
            ("missing", "ospf.virtual_links.10.255.0.2.auth.type"),
            ("missing", "ospf.virtual_links.10.255.0.2.transit_area"),
            ("extra", "ospf.virtual_links.172.16.10.9.auth.key"),
            ("extra", "ospf.virtual_links.172.16.10.9.auth.key_id"),
            ("extra", "ospf.virtual_links.172.16.10.9.auth.type"),
            ("extra", "ospf.virtual_links.172.16.10.9.transit_area"),
        ],
        expect=VLINK_ENDPOINT_MISSING_EXPECT + ["L3.config.Hub-B"],
        may=VLINK_DOWN_MAY,
        settle=OSPF_SETTLE, revert_settle=OSPF_REVERT_SETTLE,
    ),
    Fault(
        2, "Branch-3", "Forward-chain filter drops OSPF, killing the virtual link that transits Branch-3",
        edits=[("networks=10.1.3.0/24 passive\n",
                "networks=10.1.3.0/24 passive\n\n/ip firewall filter add chain=forward protocol=ospf action=drop\n")],
        inject=["/ip firewall filter add chain=forward protocol=ospf action=drop"],
        # Unquoted, "protocol=ospf" inside [find] matches nothing on RouterOS 7.23 (the remove
        # then silently does nothing); the quoted value matches
        revert=['/ip firewall filter remove [find chain=forward protocol="ospf"]'],
        l3_diff=[
            ("extra", "firewall[0].action"),
            ("extra", "firewall[0].chain"),
            ("extra", "firewall[0].protocol"),
            ("extra", "firewall[0].table"),
        ],
        expect=VLINK_DOWN_EXPECT + ["L3.config.Branch-3"],
        may=VLINK_DOWN_MAY,
        settle=OSPF_SETTLE, revert_settle=OSPF_REVERT_SETTLE,
    ),
    Fault(
        3, "Branch-2", "MTU 1400 on ether1 (Hub-A eth2 is 1500): adjacency stuck in ExStart/Exchange",
        edits=[("/ip address add address=172.16.10.6/30 interface=ether1\n",
                "/interface ethernet set [find default-name=ether1] mtu=1400\n"
                "/ip address add address=172.16.10.6/30 interface=ether1\n")],
        inject=["/interface ethernet set [find default-name=ether1] mtu=1400"],
        revert=["/interface ethernet set [find default-name=ether1] mtu=1500"],
        l3_diff=[("extra", "interfaces.ether1.mtu")],
        expect=["L2.ospf_adjacency.Hub-A--Branch-2", "L2.default_route.Branch-2", "L2.route_coverage.Branch-2"]
        + B2_CUT_OFF + ["L3.config.Branch-2"],
        may=ROUTING_FALLOUT + ["L2.ospf_stable.Hub-A", "L2.ospf_stable.Branch-2"],
        # The MTU is only compared during the database exchange, so an adjacency that is
        # already Full survives the change. Resetting it from Hub-A's side forces a new exchange.
        kick=[("Hub-A", 'vtysh -c "clear ip ospf interface eth2"')],
        settle=OSPF_SETTLE, revert_settle=OSPF_REVERT_SETTLE,
    ),
    Fault(
        4, "Hub-A", "Wrong MD5 key on the area 0 link to HQ-Edge ('LabArea0key' vs 'LabArea0Key')",
        edits=[("set protocols ospf interface eth0 authentication md5 key-id 1 md5-key 'LabArea0Key'",
                "set protocols ospf interface eth0 authentication md5 key-id 1 md5-key 'LabArea0key'")],
        inject=["set protocols ospf interface eth0 authentication md5 key-id 1 md5-key 'LabArea0key'"],
        revert=["set protocols ospf interface eth0 authentication md5 key-id 1 md5-key 'LabArea0Key'"],
        l3_diff=[("changed", "ospf.interfaces.eth0.auth.key")],
        expect=[
            "L2.ospf_adjacency.HQ-Edge--Hub-A",
            "L2.default_route.Hub-A", "L2.default_route.Hub-B", "L2.default_route.Branch-[123]",
            "L2.route_coverage.HQ-Edge", "L2.summaries.HQ-Edge",
            "L2.ospf_route_type.HQ-Edge.*", "L2.ospf_route_type.Hub-[AB].0.0.0.0/0",
        ] + NO_INTERNET + ["L3.config.Hub-A"],
        may=["L2.ospf_stable.HQ-Edge", "L2.ospf_stable.Hub-A"],
        settle=OSPF_SETTLE, revert_settle=OSPF_REVERT_SETTLE,
    ),
    Fault(
        5, "Branch-4", "Area 20 not stub on Branch-4 (type=default vs stub): stub-flag mismatch with Hub-B",
        edits=[("instance=default-v2 type=stub", "instance=default-v2")],
        inject=['/routing ospf area set [find name="area20"] type=default'],
        revert=['/routing ospf area set [find name="area20"] type=stub'],
        l3_diff=[("changed", "ospf.areas.20.type")],
        expect=["L2.ospf_adjacency.Hub-B--Branch-4", "L2.default_route.Branch-4", "L2.route_coverage.Branch-4"]
        + B4_CUT_OFF + ["L3.config.Branch-4"],
        may=ROUTING_FALLOUT + ["L2.ospf_stable.Hub-B", "L2.ospf_stable.Branch-4"],
        settle=OSPF_SETTLE, revert_settle=OSPF_REVERT_SETTLE,
    ),
    Fault(
        6, "Hub-A", "Area 10 range 10.1.0.0/24 instead of /16: area 10 LANs leak into area 0",
        edits=[("set protocols ospf area 10 range 10.1.0.0/16", "set protocols ospf area 10 range 10.1.0.0/24")],
        inject=["delete protocols ospf area 10 range 10.1.0.0/16", "set protocols ospf area 10 range 10.1.0.0/24"],
        revert=["delete protocols ospf area 10 range 10.1.0.0/24", "set protocols ospf area 10 range 10.1.0.0/16"],
        l3_diff=[
            ("missing", "ospf.areas.10.ranges.10.1.0.0/16.configured"),
            ("extra", "ospf.areas.10.ranges.10.1.0.0/24.configured"),
        ],
        expect=["L2.summaries.HQ-Edge", "L3.config.Hub-A"],
        settle=20, revert_settle=20,
    ),
    Fault(
        7, "Branch-4", "Return-traffic rule widened to all states: accepts everything above the guest drops",
        edits=[("connection-state=established,related action=accept", "action=accept")],
        # Select the rule by what it is (the only forward accept rule in the design), not by its
        # comment: an agent rewrote the comment once, which silently turned the next inject into a no-op
        inject=["/ip firewall filter set [find chain=forward action=accept] !connection-state"],
        revert=["/ip firewall filter set [find chain=forward action=accept] connection-state=established,related"],
        l3_diff=[("missing", "firewall[0].connection-state")],
        expect=GUEST_HOLES + ["L3.config.Branch-4"],
    ),
    Fault(
        8, "HQ-Edge", "Local preference 200 on ISP-B-IN instead of ISP-A-IN: backup default becomes best",
        edits=[("set policy route-map ISP-A-IN rule 10 set local-preference 200\n"
                "set policy route-map ISP-B-IN rule 10 action permit\n",
                "set policy route-map ISP-B-IN rule 10 action permit\n"
                "set policy route-map ISP-B-IN rule 10 set local-preference 200\n")],
        # Deleting only "set local-preference" leaves an empty "rule 10 set" node behind on
        # VyOS, so delete the whole "set" node (local-preference is the only thing under it)
        inject=[
            "delete policy route-map ISP-A-IN rule 10 set",
            "set policy route-map ISP-B-IN rule 10 set local-preference 200",
        ],
        revert=[
            "delete policy route-map ISP-B-IN rule 10 set",
            "set policy route-map ISP-A-IN rule 10 set local-preference 200",
        ],
        l3_diff=[
            ("missing", "route_maps.ISP-A-IN.rule 10.set local-preference"),
            ("extra", "route_maps.ISP-B-IN.rule 10.set local-preference"),
        ],
        expect=["L2.bgp_best.0.0.0.0/0", "L2.bgp_best.8.8.8.8/32", "L2.bgp_backup_default",
                "L2.default_route.HQ-Edge", "L3.config.HQ-Edge"],
        # FRR re-applies changed route-maps on its own after a few seconds; this makes it immediate
        kick=[("HQ-Edge", 'vtysh -c "clear ip bgp * soft in"')],
        settle=20, revert_settle=20,
    ),
    Fault(
        9, "HQ-Edge", "default-information originate always (should be conditional)",
        edits=[("set protocols ospf default-information originate\n",
                "set protocols ospf default-information originate always\n")],
        inject=["set protocols ospf default-information originate always"],
        revert=["delete protocols ospf default-information originate always"],
        l3_diff=[("extra", "ospf.default_originate.always")],
        expect=["L3.config.HQ-Edge"],
    ),
    Fault(
        10, "HQ-Edge", "NAT rule 110 on eth1 instead of eth2: traffic to ISP-B leaves un-NATed",
        edits=[("set nat source rule 110 outbound-interface name eth2",
                "set nat source rule 110 outbound-interface name eth1")],
        inject=["set nat source rule 110 outbound-interface name eth1"],
        revert=["set nat source rule 110 outbound-interface name eth2"],
        l3_diff=[("changed", "nat.source rule 110.outbound-interface name")],
        expect=["L1.ping.*->1.1.1.1", "L3.config.HQ-Edge"],
    ),
    Fault(
        11, "Branch-2", "LAN interface-template not passive: hellos sent toward the hosts",
        edits=[("networks=10.1.2.0/24 passive", "networks=10.1.2.0/24")],
        # passive is a flag property on RouterOS 7: "passive=no" is a syntax error
        inject=["/routing ospf interface-template set [find networks=10.1.2.0/24] !passive"],
        revert=["/routing ospf interface-template set [find networks=10.1.2.0/24] passive"],
        l3_diff=[("missing", "ospf.interfaces.ether2.passive")],
        expect=["L3.config.Branch-2"],
    ),
    Fault(
        12, "B1-PC2", "Host address 10.1.1.11/16 instead of /24: ARPs for the other area 10 LANs",
        edits=[("address 10.1.1.11/24", "address 10.1.1.11/16")],
        # Add the /16 before removing the /24 so the address (and the default route) never goes away
        inject=[
            "ip addr add 10.1.1.11/16 dev eth0",
            "ip addr del 10.1.1.11/24 dev eth0",
            "ip route show default | grep -q . || ip route add default via 10.1.1.1 dev eth0 metric 1 onlink",
            "sed -i 's#address 10.1.1.11/24#address 10.1.1.11/16#' /etc/network/interfaces",
        ],
        revert=[
            "ip addr add 10.1.1.11/24 dev eth0",
            "ip addr del 10.1.1.11/16 dev eth0",
            "ip route show default | grep -q . || ip route add default via 10.1.1.1 dev eth0 metric 1 onlink",
            "sed -i 's#address 10.1.1.11/16#address 10.1.1.11/24#' /etc/network/interfaces",
        ],
        l3_diff=[
            ("changed", "interfaces_file.eth0.address"),
            ("missing", "runtime.addresses.eth0[10.1.1.11/24]"),
            ("extra", "runtime.addresses.eth0[10.1.1.11/16]"),
            ("missing", "runtime.routes[10.1.1.0/24 dev eth0]"),
            ("extra", "runtime.routes[10.1.0.0/16 dev eth0]"),
        ],
        expect=["L1.ping.B1-PC2->B[23]-PC?", "L1.ping.B[23]-PC?->B1-PC2", "L3.config.B1-PC2"],
    ),
]

BY_NUM = {f.num: f for f in FAULTS}
FAULT_DEVICES = sorted({f.device for f in FAULTS}, key=lambda n: list(DEVICE_BY_NAME).index(n))


def faults_on(device):
    return [f for f in FAULTS if f.device == device]


def apply_edits(text, faults):
    """Apply the config edits of the given faults to a base config text."""
    for f in sorted(faults, key=lambda f: f.num):
        for old, new in f.edits:
            if text.count(old) != 1:
                raise ValueError(f"fault {f.num}: {old!r} occurs {text.count(old)} times in {f.device}'s base config")
            text = text.replace(old, new)
    return text


def broken_path(device, configs_dir=CONFIGS_DIR):
    return os.path.join(configs_dir, BROKEN_FILES[DEVICE_BY_NAME[device]["platform"]].format(name=device))


def device_model(device, nums, configs_dir=CONFIGS_DIR):
    """The model of a device with the given faults applied to its intended config."""
    platform = DEVICE_BY_NAME[device]["platform"]
    base, access = config_texts(configs_dir, device, platform)
    return model_from_texts(platform, [apply_edits(base, [BY_NUM[n] for n in nums]), access])


def diff_key(d):
    """A hashable form of one diff entry, values included."""
    return (d["kind"], d["path"], repr(d["intent"]), repr(d["live"]))


def expected_diffs(device, nums, intent=None, configs_dir=CONFIGS_DIR):
    """The Layer 3 diff a device should show with exactly the given faults present."""
    if intent is None:
        platform = DEVICE_BY_NAME[device]["platform"]
        intent = model_from_texts(platform, config_texts(configs_dir, device, platform))
    return {diff_key(d) for d in diff_models(intent, device_model(device, nums, configs_dir))}


def fault_states(device, diffs, intent=None):
    """Classify a device's live Layer 3 diff.

    Returns ({fault num: "injected" | "clean" | "partial"}, diff keys no fault explains).
    """
    keys = {diff_key(d) for d in diffs}
    states, explained = {}, set()
    for f in faults_on(device):
        own = expected_diffs(device, [f.num], intent)
        explained |= own
        hit = own & keys
        states[f.num] = "injected" if hit == own else ("partial" if hit else "clean")
    return states, keys - explained


def grade(f, checks):
    """Compare a verifier run with the fault's expect/may globs."""
    status = {c["id"]: c["status"] for c in checks}
    layers = {c["layer"] for c in checks}

    def matching(globs):
        return {i for i in status if any(fnmatchcase(i, g) for g in globs)}

    expected, allowed = matching(f.expect), matching(f.may)
    failing = {i for i, s in status.items() if s != "pass"}
    errors = sorted(i for i, s in status.items() if s == "error")
    unmatched = [g for g in f.expect + f.may if int(g[1]) in layers and not matching([g])]
    missed = sorted(expected - failing)
    unexpected = sorted(failing - expected - allowed - set(errors))
    verdict = "error" if errors else ("match" if not (missed or unexpected or unmatched) else "mismatch")
    return {
        "verdict": verdict,
        "failing_by_layer": {f"L{n}": sorted(i for i in failing if i.startswith(f"L{n}.")) for n in sorted(layers)},
        "missed": missed,                                   # predicted to fail, passed
        "unexpected": unexpected,                           # failed, not predicted
        "may_failed": sorted((failing & allowed) - expected),
        "errors": errors,
        "unmatched_globs": unmatched,                       # catalog typo or check that doesn't exist
    }


assert set(CONFIG_FILES) == set(BROKEN_FILES)
