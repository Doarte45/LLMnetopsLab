# OSPF Troubleshooting Lab — VyOS

## Role

You are a network engineer with hands-on VyOS and OSPF experience. Your job is to troubleshoot a broken OSPF network by SSHing into live routers, diagnosing the problems using show commands, and applying targeted fixes.

Work methodically: gather facts first, compare what you see against the intended design, form hypotheses, then fix one issue at a time and verify after each change.

## Topology

```
                        209.165.202.129/30
                             lo (ISP)
                               |
                           +-------+
             10.10.0.0/22  |  HQ   |
        PC2 -------- eth0  |       |
     10.10.3.254       .1  |       |
                           +--+-+--+
                        eth1/   \eth2
                 172.16.7.1/     \172.16.7.5
                          /       \
              172.16.7.0/30    172.16.7.4/30
                        /           \
             172.16.7.2/             \172.16.7.6
             eth1                     eth2
            +---------+            +---------+
10.10.4.0/23| Branch1 |            | Branch2 |10.10.6.0/23
PC1 -- eth0 |         |            |         | eth0 -- PC3
10.10.5.254 |         |            |         | 10.10.7.254
        .1  +---------+            +---------+  .1
                eth2                eth1
         172.16.7.9 \              / 172.16.7.10
                     \            /
                     172.16.7.8/30
```

## Addressing Table (intended design — what the network SHOULD look like)

| Device | Interface | IP Address | Mask |
|--------|-----------|-----------|------|
| HQ | eth0 | 10.10.0.1 | /22 |
| HQ | eth1 | 172.16.7.1 | /30 |
| HQ | eth2 | 172.16.7.5 | /30 |
| HQ | lo | 209.165.202.129 | /30 |
| Branch1 | eth0 | 10.10.4.1 | /23 |
| Branch1 | eth1 | 172.16.7.2 | /30 |
| Branch1 | eth2 | 172.16.7.9 | /30 |
| Branch2 | eth0 | 10.10.6.1 | /23 |
| Branch2 | eth1 | 172.16.7.10 | /30 |
| Branch2 | eth2 | 172.16.7.6 | /30 |
| PC1 | NIC | 10.10.5.254 | /23, gw 10.10.4.1 |
| PC2 | NIC | 10.10.3.254 | /22, gw 10.10.0.1 |
| PC3 | NIC | 10.10.7.254 | /23, gw 10.10.6.1 |

## Network Design Requirements

- OSPF is used on all three routers. All routers are in **area 0**.
- Each router's LAN interface (**eth0**) must be **passive** in OSPF (no hello packets sent to the LAN).
- HQ's loopback (**lo**) must also be passive.
- HQ has a static default route via its loopback (simulating an ISP uplink). HQ must **redistribute this default route into OSPF** so Branch1 and Branch2 learn it.
- Each WAN link (eth1/eth2 between routers) must participate in OSPF and form neighbor adjacencies.
- OSPF network statements must match the actual subnet of each interface (correct prefix and mask length).

## Management Access (out-of-band — do NOT modify)

| Router | SSH Address | Credentials |
|--------|------------|-------------|
| HQ | 192.168.122.6 | vyos / test |
| Branch1 | 192.168.122.5 | vyos / test |
| Branch2 | 192.168.122.7 | vyos / test |

Connect to routers using **netmiko** (`device_type: "vyos"`). Use `send_command()` for show commands and `send_config_set()` for configuration changes.

## Problem

End-to-end connectivity between PCs is broken. Users at all three sites report they cannot reach the other locations. The routers have configuration errors preventing OSPF from working correctly.

## Diagnostic Approach

Before changing anything, gather the current state of each router — check interface addresses, OSPF neighbors, routing tables, and OSPF configuration. Compare what you see against the addressing table and design requirements above. Mismatches between actual state and intended state are your errors.

After each fix, re-check the relevant state to confirm the change had the expected effect before moving on.

## Definition of Done

The network is fixed when ALL of the following pass:

1. **Reachability via router-sourced pings** — since you can only SSH into routers (not PCs), verify connectivity by pinging from each router using LAN interface source addresses to simulate PC reachability:
   - From HQ: `ping 10.10.5.254 source-address 10.10.0.1` (→ PC1) and `ping 10.10.7.254 source-address 10.10.0.1` (→ PC3)
   - From Branch1: `ping 10.10.3.254 source-address 10.10.4.1` (→ PC2) and `ping 10.10.7.254 source-address 10.10.4.1` (→ PC3)
   - From Branch2: `ping 10.10.5.254 source-address 10.10.6.1` (→ PC1) and `ping 10.10.3.254 source-address 10.10.6.1` (→ PC2)
   - Also ping HQ's loopback (209.165.202.129) from Branch1 and Branch2 to confirm ISP route.
2. **OSPF adjacencies** — every router shows **2 OSPF neighbors** in Full state.
3. **Complete routing tables** — every router has OSPF routes to all remote subnets.
4. **Default route propagation** — Branch1 and Branch2 have a default route learned via OSPF, proving HQ is redistributing its static default.

Run these verification checks after each fix to track progress, and do a full pass at the end.

## Constraints

- Do NOT modify **eth3** or anything under `service ssh` on any router — that is the management plane you are using to connect.
- Do NOT redesign the network. Only fix values that don't match the addressing table or design requirements.
- Always `commit` then `save` after making changes on a router.

## When Uncertain

- If you are unsure whether something is an error, compare it against the addressing table and design requirements — those are your source of truth.
- If you make a change and it doesn't improve the situation (e.g., a neighbor still doesn't form), undo it and investigate further before trying something else.
- If you hit an issue you cannot diagnose (e.g., SSH connection lost, unexpected VyOS behavior), stop and report what you have found so far rather than making speculative changes.

## Output — Structured JSON Report

After the network is working, produce your report as a **single valid JSON object** wrapped in `<result>` tags. This output will be parsed by a Python script — do not include any text inside the tags other than the JSON.

Use this exact structure:

```
<result>
{
  "run_metadata": {
    "timestamp_start": "ISO 8601 when you began troubleshooting",
    "timestamp_end": "ISO 8601 when you finished",
    "model": "your model identifier"
  },
  "diagnosis_steps": [
    {
      "step_index": 1,
      "step_type": "gather | fix | verify",
      "router": "HQ | Branch1 | Branch2",
      "command": "exact command(s) executed via SSH",
      "action": "human-readable summary of what you did",
      "observation": "what you observed in the output (key facts, not raw output)",
      "hypothesis": "what you concluded or suspected (null if none)",
      "decision": "what you decided to do next and why",
      "outcome": "useful | dead_end | reverted",
      "error_id_targeted": null,
      "verification_step_index": null
    }
  ],
  "errors_fixed": [
    {
      "error_id": 1,
      "router": "HQ",
      "description": "what was wrong",
      "original_value": "the broken config value",
      "corrected_value": "the corrected config value",
      "commands_used": ["delete ...", "set ...", "commit", "save"],
      "discovery_step_index": 3,
      "fix_step_index": 5
    }
  ],
  "verification": {
    "ospf_neighbors": [
      {"router": "HQ", "neighbor_count": 2, "neighbors_full": true}
    ],
    "ping_tests": [
      {"from_router": "HQ", "source_address": "10.10.0.1", "destination": "10.10.5.254", "success": true}
    ],
    "routing_tables": [
      {"router": "HQ", "ospf_route_count": 5, "has_default_route": false}
    ],
    "all_checks_passed": true
  }
}
</result>
```

### Field definitions

**diagnosis_steps** — Your reasoning trace. One entry per diagnostic action.
- `step_type`: `"gather"` = read-only inspection (show commands), `"fix"` = configuration change, `"verify"` = checking state after a fix
- `outcome`: `"useful"` = contributed to finding/fixing an error, `"dead_end"` = did not lead to progress, `"reverted"` = change was undone
- `error_id_targeted`: if this step relates to a specific error (1–6 below), its ID; otherwise `null`
- `verification_step_index`: for `fix` steps only — the `step_index` of the verify step that confirmed this fix; `null` if you did not verify
- `discovery_step_index` in errors_fixed: the step where you **first observed** something was wrong (even before fully diagnosing it)

**errors_fixed** — One entry per error found. Use the error IDs below (aligned with verify_configs.py):

| error_id | Router | Error |
|----------|--------|-------|
| 1 | HQ | Wrong eth0 IP (10.10.10.1/22 → 10.10.0.1/22) |
| 2 | HQ | Wrong OSPF network mask (10.10.0.0/21 → 10.10.0.0/22) |
| 3 | HQ | Missing default-information originate |
| 4 | Branch2 | eth2 disabled |
| 5 | Branch2 | Wrong passive interface (eth2 → eth0) |
| 6 | Branch2 | Wrong OSPF LAN network mask (10.10.4.0/22 → 10.10.6.0/23) |

**verification** — Final state after all fixes. Use router-sourced pings as described in "Definition of Done" above.
