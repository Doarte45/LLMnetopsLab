# Multi-Area OSPF Troubleshooting Lab (v2)

You're the network engineer for a small enterprise WAN running live in GNS3: an edge router with two ISP uplinks, two hub routers, four branch routers, and ten hosts. The network has drifted from its design and users are reporting connectivity problems, but nobody has pinned down what is failing or why. There may be more than one fault, anywhere in the lab. Your job is to find every fault, fix each one at its root, and show that the network matches its design again.

This run is unattended, so nobody will be around to answer questions. When you hit a judgment call, make the call a careful engineer would make and explain it in your report.

## Access

Every device has an out-of-band management interface on 192.168.122.0/24 that you can reach over SSH from this machine. Python 3 and netmiko are available. With 19 devices, a small reusable script is the practical way to run commands across them.

| Device | Platform | Mgmt address | Mgmt interface | netmiko `device_type` | Login |
|---|---|---|---|---|---|
| HQ-Edge | VyOS | 192.168.122.11 | eth3 | `vyos` | vyos / test |
| Hub-A | VyOS | 192.168.122.12 | eth4 | `vyos` | vyos / test |
| Hub-B | VyOS | 192.168.122.13 | eth3 | `vyos` | vyos / test |
| ISP-A | VyOS | 192.168.122.14 | eth3 | `vyos` | vyos / test |
| ISP-B | VyOS | 192.168.122.15 | eth3 | `vyos` | vyos / test |
| Branch-1 | MikroTik RouterOS 7 | 192.168.122.21 | ether4 | `mikrotik_routeros` | admin / test |
| Branch-2 | MikroTik RouterOS 7 | 192.168.122.22 | ether4 | `mikrotik_routeros` | admin / test |
| Branch-3 | MikroTik RouterOS 7 | 192.168.122.23 | ether4 | `mikrotik_routeros` | admin / test |
| Branch-4 | MikroTik RouterOS 7 | 192.168.122.24 | ether4 | `mikrotik_routeros` | admin / test |
| B1-PC1, B1-PC2 | Alpine Linux | 192.168.122.31, .32 | eth3 | `linux` | root / alpine |
| B2-PC1, B2-PC2 | Alpine Linux | 192.168.122.33, .34 | eth3 | `linux` | root / alpine |
| B3-PC1, B3-PC2 | Alpine Linux | 192.168.122.35, .36 | eth3 | `linux` | root / alpine |
| B4-PC1 … B4-PC4 | Alpine Linux | 192.168.122.37 … .40 | eth3 | `linux` | root / alpine |

Hub-A is the exception: its management interface is eth4, and its eth3 is a lab link to Branch-3.

You reach every device through the management plane, and nobody can restore it if it breaks during the run. On every device, leave the management interface, the SSH service and the login accounts exactly as they are. The management network is not part of the lab either. It has no gateway and should never appear in OSPF, and a ping between management addresses tells you nothing about the lab, so always test with lab addresses.

Work from the live devices and this document. Don't search this machine for configuration files, answer keys or notes, because the point is to troubleshoot the network in front of you. Keep any scripts you write in the current directory.

## Topology

```
     ISP-A  lo 8.8.8.8                       ISP-B  lo 1.1.1.1
          \  203.0.113.0/30                 /  203.0.113.4/30
           \                               /
            +---------- HQ-Edge ----------+           ASBR, NAT to both ISPs
                           |  172.16.1.0/30                            area 0
                         Hub-A                        ABR 0/10
           +---------------+----------------+
     172.16.10.0/30   172.16.10.4/30   172.16.10.8/30                  area 10
           |               |                |
       Branch-1        Branch-2         Branch-3
      10.1.1.0/24     10.1.2.0/24      10.1.3.0/24
                                            |  172.16.20.0/30          area 10
                                          Hub-B                       ABR 10/20
                                            |  172.16.20.4/30          area 20
                                        Branch-4
                                10.2.1.0/24    10.2.2.0/24

        Hub-A <====== virtual link through area 10 ======> Hub-B
```

### Interfaces

| Device | Interface | Address | Connects to | OSPF |
|---|---|---|---|---|
| HQ-Edge | eth0 | 172.16.1.1/30 | Hub-A eth0 | area 0 |
| HQ-Edge | eth1 | 203.0.113.2/30 | ISP-A eth0 | not in OSPF |
| HQ-Edge | eth2 | 203.0.113.6/30 | ISP-B eth0 | not in OSPF |
| Hub-A | eth0 | 172.16.1.2/30 | HQ-Edge eth0 | area 0 |
| Hub-A | eth1 | 172.16.10.1/30 | Branch-1 ether1 | area 10 |
| Hub-A | eth2 | 172.16.10.5/30 | Branch-2 ether1 | area 10 |
| Hub-A | eth3 | 172.16.10.9/30 | Branch-3 ether1 | area 10 |
| Hub-B | eth0 | 172.16.20.1/30 | Branch-3 ether3 | area 10 |
| Hub-B | eth1 | 172.16.20.5/30 | Branch-4 ether1 | area 20 |
| Branch-1 | ether1 | 172.16.10.2/30 | Hub-A eth1 | area 10 |
| Branch-1 | ether2 | 10.1.1.1/24 | LAN: B1-PC1, B1-PC2 | area 10, passive |
| Branch-2 | ether1 | 172.16.10.6/30 | Hub-A eth2 | area 10 |
| Branch-2 | ether2 | 10.1.2.1/24 | LAN: B2-PC1, B2-PC2 | area 10, passive |
| Branch-3 | ether1 | 172.16.10.10/30 | Hub-A eth3 | area 10 |
| Branch-3 | ether2 | 10.1.3.1/24 | LAN: B3-PC1, B3-PC2 | area 10, passive |
| Branch-3 | ether3 | 172.16.20.2/30 | Hub-B eth0 | area 10 |
| Branch-4 | ether1 | 172.16.20.6/30 | Hub-B eth1 | area 20 |
| Branch-4 | ether2 | 10.2.1.1/24 | LAN: B4-PC1, B4-PC2 | area 20, passive |
| Branch-4 | ether3 | 10.2.2.1/24 | LAN: B4-PC3, B4-PC4 | area 20, passive |
| ISP-A | eth0 | 203.0.113.1/30 | HQ-Edge eth1 | none |
| ISP-A | lo | 8.8.8.8/32 | test target | none |
| ISP-B | eth0 | 203.0.113.5/30 | HQ-Edge eth2 | none |
| ISP-B | lo | 1.1.1.1/32 | test target | none |

OSPF router IDs: HQ-Edge 10.255.0.1, Hub-A 10.255.0.2, Hub-B 10.255.0.3, Branch-1 10.255.1.1, Branch-2 10.255.1.2, Branch-3 10.255.1.3, Branch-4 10.255.2.1.

### Hosts

Every host has a static address on eth0 and uses its branch router's LAN address as the default gateway.

| Host | Address | Gateway | Host | Address | Gateway |
|---|---|---|---|---|---|
| B1-PC1 | 10.1.1.10/24 | 10.1.1.1 | B3-PC2 | 10.1.3.11/24 | 10.1.3.1 |
| B1-PC2 | 10.1.1.11/24 | 10.1.1.1 | B4-PC1 | 10.2.1.10/24 | 10.2.1.1 |
| B2-PC1 | 10.1.2.10/24 | 10.1.2.1 | B4-PC2 | 10.2.1.11/24 | 10.2.1.1 |
| B2-PC2 | 10.1.2.11/24 | 10.1.2.1 | B4-PC3 | 10.2.2.10/24 | 10.2.2.1 |
| B3-PC1 | 10.1.3.10/24 | 10.1.3.1 | B4-PC4 | 10.2.2.11/24 | 10.2.2.1 |

## Design

This section and the tables above are the source of truth. When the live network disagrees with them, the network is wrong. If the design doesn't settle whether something is a fault, leave it alone and mention it in your report.

- **Areas.** All routers except the ISPs run OSPFv2. Area 0 is the HQ-Edge to Hub-A link. Area 10 contains Hub-A's three branch links, Branch-1/2/3 and the Branch-3 to Hub-B link, and Hub-A is its ABR to area 0. Area 20 contains the Hub-B to Branch-4 link and Branch-4's LANs, and Hub-B is its ABR.
- **Virtual link.** Area 20 has no physical path to area 0, so Hub-A and Hub-B run a virtual link across area 10. That's why area 10 has to stay a normal area: a virtual link can't transit a stub or NSSA area.
- **Passive LANs.** Branch LAN interfaces are in OSPF as passive interfaces. Their subnets are advertised, but no hellos are sent toward the hosts.
- **Summarization.** Both ABRs attached to area 10 (Hub-A and Hub-B) summarize the area 10 LANs as 10.1.0.0/16. Hub-B summarizes the area 20 LANs as 10.2.0.0/16. The /30 transit links are not summarized.
- **Internet edge.** HQ-Edge is the ASBR. Its default route is a static route via ISP-A (203.0.113.1). A floating static route via ISP-B (203.0.113.5, distance 10) takes over only if the ISP-A route is withdrawn. HQ-Edge originates a default route into OSPF only while it has one itself, so that if both uplinks are lost the rest of the network stops sending traffic toward a dead end. Traffic leaving either ISP link is source-NATed (masquerade) to HQ-Edge's address on that link.
- **ISPs.** ISP-A and ISP-B stand in for upstream providers and are deliberately minimal: a link address and a loopback. They run no routing protocol and have no routes to internal networks, because NAT on HQ-Edge makes such routes unnecessary. One consequence is that 1.1.1.1, which sits behind ISP-B, is unreachable from the lab while ISP-A is the active path. That's expected.
- **Nothing extra.** If the design doesn't call for something, it shouldn't be configured. Settings the design doesn't mention should be left at platform defaults.

## Definition of done

The network is fixed when all of the following hold, confirmed in a final pass using data collected after your last change:

1. **Adjacencies.** Seven OSPF adjacencies are up and Full:
   - HQ-Edge to Hub-A (area 0)
   - Hub-A to Branch-1, Hub-A to Branch-2, Hub-A to Branch-3, and Branch-3 to Hub-B (area 10)
   - Hub-B to Branch-4 (area 20)
   - the Hub-A to Hub-B virtual link
2. **Routing tables.**
   - Every OSPF router except HQ-Edge has a default route learned through OSPF from HQ-Edge.
   - On HQ-Edge, the active default route is the static route via 203.0.113.1, and the distance-10 route via 203.0.113.5 is present as a backup.
   - Every OSPF router has a route covering every lab subnet. Routers outside area 10 see the area 10 LANs only as 10.1.0.0/16, and routers outside area 20 see the area 20 LANs only as 10.2.0.0/16.
3. **Host reachability,** tested from the hosts themselves using lab addresses:
   - Every PC can ping every other PC (10 hosts, 90 pairs).
   - Every PC can ping 8.8.8.8.
4. **Backup path ready.** HQ-Edge can ping both ISP next hops, 203.0.113.1 and 203.0.113.5. Don't take the primary path down to test failover. Checking the backup route and the ISP-B link is enough, and it doesn't risk leaving the network broken.
5. **Clean, persistent config.** Every fix is saved so it survives a reboot. Nothing you added for diagnosis is left behind, and no configuration exists that the design doesn't call for.

## How to work

**Get the whole picture before changing anything.** Collect state from every device first. On routers, that means interfaces and addresses, OSPF configuration and neighbors, and routing tables. On HQ-Edge, also collect static routes and NAT. On hosts, collect addressing and routes. Read-only collection runs well in parallel, and one pass across all devices is faster and more coherent than probing them one at a time. Compare what you find against the design. Every deviation is a candidate fault, but before you act on one, make sure you can explain how it causes a failure, or would cause one.

**Fix causes, not symptoms.** Each fix should bring the configuration back in line with the design. A workaround can make pings succeed while leaving the network wrong and fragile, and here it counts as a failure. Examples include a static route that covers for a missing OSPF route, a knob that forces behavior unconditionally, an extra network statement, or a route added on an ISP. If the design-compliant fix doesn't seem to work, find out why instead of reaching for an override.

**Change one device at a time and check the effect.** Faults can mask each other, and a change can have side effects you didn't intend. Change one device, then confirm you got the effect you predicted. If a change didn't help, revert it before trying something else, so that you're never debugging a stack of speculative edits. OSPF needs time to converge. Adjacencies can take tens of seconds to form, and the virtual link comes up only after area 10 has converged, so wait before deciding a change didn't work.

**Keep going until the definition of done holds.** A symptom clearing doesn't mean the network is fixed, because there may be several faults. When everything seems to pass, do the final verification pass on fresh data.

**Know where your reach ends.** Don't reboot, reset or reload any device. A device that doesn't come back can't be recovered during the run, and a reboot can hide a fault rather than fix it. If you lose management access to a device, stop working on it, don't try to reach it through other devices, and report it. If a problem can't be fixed from the device CLIs, for example something in the virtualization layer, report it with your evidence instead of working around it.

## Platform notes

These are quirks of these specific devices and of driving them through netmiko.

**All platforms:** give every ping an explicit count, and a timeout where the platform supports one. A ping without a count never returns and hangs the session.

**VyOS** (2026.02 rolling, FRR routing stack):
- `send_config_set()` leaves the session in configuration mode, so follow it with `commit()` and then `save_config()`. When netmiko leaves configuration mode it discards any uncommitted changes (`exit discard`), so a missing commit fails silently. Confirm that each change landed.
- In configuration mode, operational commands need a `run` prefix. `save_config()` returns the session to operational mode.
- `show configuration commands` prints the running config as set-commands, which is the easiest form to compare against the design.

**MikroTik RouterOS 7:**
- There is no candidate configuration. Every command takes effect immediately and persists on its own, and the only undo is to reverse the command yourself. Check each command before you send it.
- OSPF uses the RouterOS 7 model (`/routing ospf instance`, `area`, `interface-template`, `neighbor`). RouterOS 6 syntax won't work.
- Column-formatted `print` output can come back mangled over netmiko. In this lab, `/system identity print` wraps one character per line. `/export terse`, `print terse`, `print detail` and `:put [...]` return output you can parse reliably.
- Select items with `[find ...]` instead of the numbers shown by `print`. Those numbers are valid only in the session that printed them.
- netmiko adds terminal options to the MikroTik username itself, so pass plain `admin`.

**Alpine Linux hosts** (BusyBox userland):
- Persistent network configuration is in `/etc/network/interfaces`. That file also holds the eth3 management stanza, so edit only the eth0 stanza and never rewrite the whole file. Avoid `rc-service networking restart`, because it bounces eth3 and drops your session. Apply eth0 changes to the running system as well, for example with `ip`, and check that `ip addr` and `ip route` agree with the file.

## Report

When you're done, reply with:

- **Faults found.** For each fault, give the device, what was wrong (actual versus design), the evidence that led you to it, and the exact commands you used to fix it.
- **Verification.** Give final results for each definition-of-done item: adjacency states, the routing-table checks, the host reachability matrix (summarize passes and list every failure) and the internet checks.
- **Unresolved or uncertain.** List any faults you couldn't fix, any deviations you left alone and why, and any judgment calls you made.

Report only what you verified. If something is inferred rather than observed, say so. Keep the report tight; a table per section works well.
