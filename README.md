# 11.6.3 OSPF Troubleshooting Lab — VyOS Edition

A VyOS translation of the Cisco IOS OSPF troubleshooting lab (Packet Tracer Activity 11.6.3). Designed for GNS3 with VyOS (`vyos-2026.02.12-0026-rolling-generic-amd64`) and VPCS.

## Overview

Three routers (HQ, Branch1, Branch2) are configured with OSPF but contain **6 intentional errors** (3 on HQ, 3 on Branch2). Students must identify and fix all errors to restore full connectivity.

## Topology

```
        [PC2]                 [PC1]                 [PC3]
          |                     |                     |
       HQ eth0           Branch1 eth0           Branch2 eth0
    10.10.0.1/22        10.10.4.1/23           10.10.6.1/23
          |                     |                     |
         HQ ---eth1---eth1--- Branch1               Branch2
          |   172.16.7.0/30     |   172.16.7.8/30     |
          +-----eth2-----------+----------eth1--------+
              172.16.7.4/30          (via Branch2 eth2)
```

## File Structure

```
configs/
├── VyOS/
│   ├── broken/       # Starting configs with intentional errors
│   ├── corrected/    # Answer key — fully working configs
│   └── CCAccess/     # SSH + eth3 setup for Claude Code remote access
├── cisco/            # Original Cisco IOS scripts for comparison
│   ├── broken/
│   └── corrected/
└── vpcs/             # VPCS startup configs (PC1, PC2, PC3)
gns3/
└── topology-notes.md   # GNS3 wiring and setup guide
lab-guide/
└── 11.6.3-OSPF-Troubleshooting-VyOS.md   # Full student lab document
results/
├── runs/             # Claude's JSON diagnostic output (run_001.json, ...)
└── verify/           # verify_configs.py JSON output (run_001.json, ...)
scripts/
├── verify_configs.py # Validates fixes against answer keys, writes JSON results
├── parse_results.py  # Analyzes JSON outputs and computes reasoning metrics
├── push_configs.py   # Loads broken/corrected configs to routers via SSH
└── connect_routers.py # Tests SSH connectivity to all routers
```

## Quick Start

1. Import VyOS and VPCS nodes into GNS3.
2. Wire the topology per `gns3/topology-notes.md`.
3. Load `configs/VyOS/broken/*.set` (or `.boot`) onto each router.
4. Load `configs/vpcs/*.vpc` onto each VPCS node.
5. Hand students the lab guide from `lab-guide/`.

## Errors Summary

| Router | Error | Broken Value | Correct Value |
|--------|-------|-------------|---------------|
| HQ | Wrong IP on eth0 | 10.10.10.1/22 | 10.10.0.1/22 |
| HQ | Missing default-information originate | (absent) | `set protocols ospf default-information originate` |
| HQ | Wrong OSPF network prefix | 10.10.0.0/21 | 10.10.0.0/22 |
| Branch2 | eth2 disabled | `disable` present | Remove `disable` |
| Branch2 | Wrong passive-interface | eth2 | eth0 |
| Branch2 | Wrong OSPF LAN network | 10.10.4.0/22 | 10.10.6.0/23 |

## Interface Mapping (Cisco → VyOS)

| Cisco | VyOS | Role |
|-------|------|------|
| FastEthernet0/0 | eth0 | LAN |
| Serial0/0/0 | eth1 | WAN link 1 |
| Serial0/0/1 | eth2 | WAN link 2 |
| Loopback1 | lo | Simulated ISP (HQ only) |

## Verification Scripts

After Claude Code completes a troubleshooting run, verify the fixes and analyze results:

```bash
# Verify live router configs (auto-numbers output as run_001.json, run_002.json, ...)
python scripts/verify_configs.py

# Specify a run number explicitly
python scripts/verify_configs.py --run-number 5

# Skip file output (stdout only)
python scripts/verify_configs.py --no-file

# Analyze all runs with cross-validation against verify outputs
python scripts/parse_results.py results/runs/ --verify-dir results/verify/
```

## Requirements

- **GNS3** 2.2+
- **VyOS** 2026.02 rolling (qcow2 image)
- **VPCS** (built into GNS3)
