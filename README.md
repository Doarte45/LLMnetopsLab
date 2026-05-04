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
claude-prompt/
└── CLAUDE.md             # Prompt for Claude Code troubleshooting runs
configs/
├── VyOS/
│   ├── broken/           # Starting configs with intentional errors
│   ├── corrected/        # Answer key — fully working configs
│   └── CCAccess/         # SSH + eth3 setup for Claude Code remote access
├── cisco/                # Original Cisco IOS scripts for comparison
│   ├── broken/
│   └── corrected/
└── vpcs/                 # VPCS startup configs (PC1, PC2, PC3)
gns3/
└── topology-notes.md     # GNS3 wiring and setup guide
scripts/
├── verify_configs.py     # Validates fixes against answer keys, writes JSON results
├── parse_results.py      # Analyzes JSON outputs and computes reasoning metrics
├── parse_transcripts.py  # Parses Claude Code transcript files
├── push_configs.py       # Loads broken/corrected configs to routers via SSH
└── connect_routers.py    # Tests SSH connectivity to all routers
notebooks/
└── visualize_results.ipynb  # Generates plots from parse_results / parse_transcripts output
results/
├── verify/               # Per-run verify_configs.py output (run_001.json ... run_050.json)
└── cctranscripts/        # Claude Code transcripts (r001.jsonl ... r050.jsonl) + helpers
    ├── extract_bash.py       # Pulls Bash tool calls out of a transcript JSONL
    └── extract_messages.py   # Pulls assistant/user messages out of a transcript JSONL
requirements-viz.txt      # Notebook deps: matplotlib, seaborn, pandas, jupyter
```

## Quick Start

1. Import VyOS and VPCS nodes into GNS3.
2. Wire the topology per `gns3/topology-notes.md`.
3. Load `configs/VyOS/broken/*.set` (or `.boot`) onto each router — or run `python scripts/push_configs.py broken` (use `corrected` for the answer key) once SSH is reachable.
4. Load `configs/vpcs/*.vpc` onto each VPCS node.
5. Point Claude Code at the lab using the prompt in `claude-prompt/CLAUDE.md`.

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

# Aggregate verify_configs.py JSON outputs into fix-rate / per-error / per-router stats
python scripts/parse_results.py results/verify/

# Parse Claude Code transcripts for duration, token cost, tool usage, and behavioral metrics
python scripts/parse_transcripts.py results/cctranscripts/
```

Both `parse_results.py` and `parse_transcripts.py` accept `--format json|table` and `--output <path>` to write a summary file.

To dig into a single Claude Code run, use the transcript helpers in `results/cctranscripts/`:

```bash
# Show every Bash command Claude ran (filter to a keyword if needed)
python results/cctranscripts/extract_bash.py results/cctranscripts/r037.jsonl --filter ospf

# Show assistant/user messages (add --with-thinking to include thinking blocks)
python results/cctranscripts/extract_messages.py results/cctranscripts/r037.jsonl
```

## Visualization

Plots over the 50 captured runs live in `notebooks/visualize_results.ipynb`. Install the extra deps and launch Jupyter:

```bash
pip install -r requirements-viz.txt
jupyter notebook notebooks/visualize_results.ipynb
```

## Requirements

- **GNS3** 2.2+
- **VyOS** 2026.02 rolling (qcow2 image)
- **VPCS** (built into GNS3)
- **Python** 3.10+ with `netmiko` (used by `push_configs.py`, `verify_configs.py`, `connect_routers.py`)
- **Optional (visualization):** `pip install -r requirements-viz.txt` for the Jupyter notebook (`matplotlib`, `seaborn`, `pandas`, `jupyter`)
