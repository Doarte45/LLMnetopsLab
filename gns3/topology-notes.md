# GNS3 Topology Setup — OSPF Troubleshooting Lab (11.6.3)

## Required Images

| Device | GNS3 Node Type | Image |
|--------|----------------|-------|
| HQ, Branch1, Branch2 | VyOS | vyos-2026.02.12-0026-rolling-generic-amd64 (qcow2) |
| PC1, PC2, PC3 | VPCS | Built-in |

## Wiring Diagram

```
              HQ
         eth1/  \eth2    eth3──Cloud
            /    \
      eth1 /      \eth2
      Branch1----Branch2
          eth2  eth1
     eth3│          │eth3
      Cloud       Cloud
```

### Cable Connections

| # | Side A | Side B | Subnet |
|---|--------|--------|--------|
| 1 | HQ eth1 | Branch1 eth1 | 172.16.7.0/30 |
| 2 | HQ eth2 | Branch2 eth2 | 172.16.7.4/30 |
| 3 | Branch1 eth2 | Branch2 eth1 | 172.16.7.8/30 |
| 4 | HQ eth0 | PC2 (e0) | 10.10.0.0/22 |
| 5 | Branch1 eth0 | PC1 (e0) | 10.10.4.0/23 |
| 6 | Branch2 eth0 | PC3 (e0) | 10.10.6.0/23 |
| 7 | HQ eth3 | Cloud (management) | 192.168.122.0/24 |
| 8 | Branch1 eth3 | Cloud (management) | 192.168.122.0/24 |
| 9 | Branch2 eth3 | Cloud (management) | 192.168.122.0/24 |

> You may place GNS3 Ethernet switches between routers and VPCS nodes if desired, but direct links work fine.
> eth3 on each router connects to a GNS3 Cloud end device for out-of-band management (Claude Code SSH access).

## Loading Broken Configs onto VyOS Routers

There are two methods to load the starting (broken) configurations.

### Method 1: Paste `set` Commands

1. Start all VyOS routers and wait for them to boot.
2. Log in (default: `vyos` / `vyos`).
3. Enter configuration mode:
   ```
   configure
   ```
4. Paste the contents of the corresponding `configs/VyOS/broken/*-broken.set` file (e.g., `HQ-broken.set`).
5. Commit and save:
   ```
   commit
   save
   ```
6. Repeat for each router.

### Method 2: Replace `config.boot`

1. Copy the `configs/VyOS/broken/*-broken.boot` file to the router's filesystem (e.g., via SCP or GNS3 file manager).
2. Replace `/config/config.boot` with the broken boot file.
3. Reboot the router:
   ```
   reboot now
   ```

## Loading VPCS Configs

1. Right-click each VPCS node in GNS3 → **Edit config**.
2. Paste the contents of the matching `.vpc` file.
3. Start or restart the VPCS node.

## Verification After Setup

Before handing the lab to students, verify:
- All routers boot and show their hostname in the prompt.
- VPCS nodes have their assigned IPs (`show ip` in VPCS console).
- Full end-to-end connectivity is **not** working (this is expected — the errors are intentional).

## Enabling Claude Code Access (CCAccess)

Each router has an eth3 interface connected to a GNS3 Cloud end device for out-of-band management. The `configs/VyOS/CCAccess/` folder contains `set` commands that configure eth3 with an IP address and enable SSH, allowing Claude Code to reach the routers over the 192.168.122.0/24 management network.

| Router | eth3 Address |
|--------|-------------|
| Branch1 | 192.168.122.5/24 |
| HQ | 192.168.122.6/24 |
| Branch2 | 192.168.122.7/24 |

To apply, enter `configure` on each router and paste the contents of the matching `*-access.set` file, then `commit` and `save`.

## Notes

- VyOS uses Ethernet interfaces (eth0, eth1, eth2) instead of Cisco's FastEthernet/Serial interfaces. All links are Ethernet in GNS3.
- The loopback on HQ (`lo`) simulates an ISP connection point — no physical link is needed for it.
- VyOS default credentials: `vyos` / `vyos`.
