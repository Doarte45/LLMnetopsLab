"""Expected Layer 1 and Layer 2 results, taken from claude-prompt/CLAUDE.md (Design and
Definition of done) and gns3/topology-notes.md.

Layer 3 does not use this file: its intent is parsed from the device configs in v2/configs.
Where the validated baseline differs from the design text, the expectation follows the
baseline and the comment says why (see planning/faults-and-verifier.md, "Lab validation").
"""

ROUTER_IDS = {
    "HQ-Edge": "10.255.0.1",
    "Hub-A": "10.255.0.2",
    "Hub-B": "10.255.0.3",
    "Branch-1": "10.255.1.1",
    "Branch-2": "10.255.1.2",
    "Branch-3": "10.255.1.3",
    "Branch-4": "10.255.2.1",
}
OSPF_ROUTERS = list(ROUTER_IDS)

# Seven adjacencies. "vlink" is the Hub-A/Hub-B virtual link, matched by router ID
# because FRR renames the VLINKn interface after reconfiguration.
ADJACENCIES = [
    ("HQ-Edge", "Hub-A", "link"),
    ("Hub-A", "Branch-1", "link"),
    ("Hub-A", "Branch-2", "link"),
    ("Hub-A", "Branch-3", "link"),
    ("Branch-3", "Hub-B", "link"),
    ("Hub-B", "Branch-4", "link"),
    ("Hub-A", "Hub-B", "vlink"),
]

AREA10_LANS = ["10.1.1.0/24", "10.1.2.0/24", "10.1.3.0/24"]
AREA20_LANS = ["10.2.1.0/24", "10.2.2.0/24"]
TRANSIT_LINKS = [
    "172.16.1.0/30",
    "172.16.10.0/30", "172.16.10.4/30", "172.16.10.8/30",
    "172.16.20.0/30", "172.16.20.4/30",
]
LAB_SUBNETS = TRANSIT_LINKS + AREA10_LANS + AREA20_LANS

# Default route per OSPF router: (protocol, next hop). None means any next hop.
DEFAULT_ROUTES = {
    "HQ-Edge": ("bgp", "203.0.113.1"),
    "Hub-A": ("ospf", "172.16.1.1"),
    "Hub-B": ("ospf", None),  # learned over the virtual link; the forwarding next hop is Branch-3
    "Branch-1": ("ospf", "172.16.10.1"),
    "Branch-2": ("ospf", "172.16.10.5"),
    "Branch-3": ("ospf", "172.16.10.9"),
    "Branch-4": ("ospf", "172.16.20.5"),
}

# Summarization. "present" prefixes must be in the table, "absent" ones must not.
# Design: outside area 10 only 10.1.0.0/16, outside area 20 only 10.2.0.0/16.
# Baseline quirk: Hub-B also leaks the area 20 /24s into area 10, so Hub-A and
# Branch-1/2/3 carry them next to the /16 and are not checked for their absence.
# HQ-Edge's area 20 /24 absence has not been confirmed on the live lab yet.
SUMMARIES = {
    "HQ-Edge": {"present": ["10.1.0.0/16", "10.2.0.0/16"], "absent": AREA10_LANS + AREA20_LANS},
    "Hub-A": {"present": ["10.2.0.0/16"], "absent": []},
    "Branch-1": {"present": ["10.2.0.0/16"], "absent": []},
    "Branch-2": {"present": ["10.2.0.0/16"], "absent": []},
    "Branch-3": {"present": ["10.2.0.0/16"], "absent": []},
}

# OSPF route types on the FRR routers (from "show ip ospf route json")
OSPF_ROUTE_TYPES = {
    "HQ-Edge": {"10.1.0.0/16": "N IA", "10.2.0.0/16": "N IA"},
    "Hub-A": {"0.0.0.0/0": "N E2", "10.2.0.0/16": "N IA"},
    "Hub-B": {"0.0.0.0/0": "N E2"},
}

# Totally stubby area 20: the only OSPF routes Branch-4 may hold.
# Baseline quirk: FRR still sends the area 10 range 10.1.0.0/16 into area 20 with no-summary.
STUB_ALLOWED_OSPF_ROUTES = {"Branch-4": {"0.0.0.0/0", "10.1.0.0/16"}}

# eBGP on HQ-Edge
BGP_ROUTER = "HQ-Edge"
BGP_PEERS = {
    "203.0.113.1": {"name": "ISP-A", "remote_as": 65100, "received": {"0.0.0.0/0", "8.8.8.8/32"}},
    "203.0.113.5": {"name": "ISP-B", "remote_as": 65200, "received": {"0.0.0.0/0", "1.1.1.1/32"}},
}
# prefix -> (best next hop, local preference or None)
BGP_BEST = {
    "0.0.0.0/0": ("203.0.113.1", 200),
    "8.8.8.8/32": ("203.0.113.1", 200),
    "1.1.1.1/32": ("203.0.113.5", None),
}
BGP_BACKUP_DEFAULT = "203.0.113.5"
ENTERPRISE_AS = "65000"
# Routes each ISP may hold in BGP: only its own loopback
ISP_OWN_PREFIXES = {"ISP-A": {"8.8.8.8/32"}, "ISP-B": {"1.1.1.1/32"}}

# Hosts: lab address on eth0
HOSTS = {
    "B1-PC1": "10.1.1.10", "B1-PC2": "10.1.1.11",
    "B2-PC1": "10.1.2.10", "B2-PC2": "10.1.2.11",
    "B3-PC1": "10.1.3.10", "B3-PC2": "10.1.3.11",
    "B4-PC1": "10.2.1.10", "B4-PC2": "10.2.1.11",
    "B4-PC3": "10.2.2.10", "B4-PC4": "10.2.2.11",
}
GUEST_HOSTS = {"B4-PC3", "B4-PC4"}
INTERNET_TARGETS = {"8.8.8.8": "ISP-A", "1.1.1.1": "ISP-B"}


def ping_expectation(src, dst):
    """Return "allow" or "deny" for a ping from host src to host or internet target dst."""
    if dst in INTERNET_TARGETS:
        return "allow"
    return "allow" if (src in GUEST_HOSTS) == (dst in GUEST_HOSTS) else "deny"
