"""Offline tests for the fault catalog (faults/catalog.py) and the broken configs.

They check that the three definitions of each fault agree before anything touches the lab:
the broken config files, the expected Layer 3 diff, and the check-ID globs.

Run from v2/scripts:  python -m pytest tests -q
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pytest  # noqa: E402

from faults.catalog import (  # noqa: E402
    BY_NUM, CONFIGS_DIR, FAULT_DEVICES, FAULTS, apply_edits, broken_path, diff_key, expected_diffs, fault_states,
    faults_on, grade,
)
from verifier import layer1, layer2, layer3  # noqa: E402
from verifier.devices import DEVICE_BY_NAME  # noqa: E402
from verifier.layer3 import CONFIG_FILES  # noqa: E402

with open(os.path.join(HERE, "fixtures", "baseline.raw.json"), encoding="utf-8") as f:
    BASELINE = json.load(f)["raw"]
INTENT = layer3.load_intent(CONFIGS_DIR)
BASELINE_CHECKS = (layer1.analyze(BASELINE["layer1"])[0] + layer2.analyze(BASELINE["layer2"])
                   + layer3.analyze(BASELINE["layer3"], INTENT)[0])
CHECK_IDS = {c["id"] for c in BASELINE_CHECKS}

MGMT = re.compile(r"\beth3\b|\beth4\b|\bether4\b|192\.168\.122\.|\bservice\b|\buser\b|\bssh\b")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_twelve_faults_numbered_in_order():
    assert [f.num for f in FAULTS] == list(range(1, 13))


def test_fault_placement():
    assert BY_NUM[3].device == "Branch-2"
    assert BY_NUM[4].device == "Hub-A"


@pytest.mark.parametrize("device", FAULT_DEVICES)
def test_broken_file_is_base_plus_catalog_edits(device):
    platform = DEVICE_BY_NAME[device]["platform"]
    base = read(os.path.join(CONFIGS_DIR, CONFIG_FILES[platform][0].format(name=device)))
    assert read(broken_path(device)) == apply_edits(base, faults_on(device))


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f"fault{f.num}")
def test_edits_give_the_documented_l3_diff(fault):
    got = sorted((kind, path) for kind, path, *_ in expected_diffs(fault.device, [fault.num], INTENT[fault.device]))
    assert got == sorted(fault.l3_diff)


@pytest.mark.parametrize("device", FAULT_DEVICES)
def test_faults_on_one_device_dont_interact(device):
    nums = [f.num for f in faults_on(device)]
    combined = expected_diffs(device, nums, INTENT[device])
    separate = set().union(*(expected_diffs(device, [n], INTENT[device]) for n in nums))
    assert combined == separate


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f"fault{f.num}")
def test_every_glob_matches_a_real_check(fault):
    from fnmatch import fnmatchcase
    for glob in fault.expect + fault.may:
        assert any(fnmatchcase(i, glob) for i in CHECK_IDS), glob


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f"fault{f.num}")
def test_l3_config_of_the_device_is_expected(fault):
    assert f"L3.config.{fault.device}" in fault.expect


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f"fault{f.num}")
def test_commands_are_well_formed_and_stay_off_management(fault):
    for cmd in fault.inject + fault.revert:
        assert not MGMT.search(cmd), cmd
        if fault.platform == "vyos":
            assert cmd.startswith(("set ", "delete ")), cmd
        elif fault.platform == "routeros":
            assert cmd.startswith("/"), cmd
            # item numbers from print are only valid in the session that printed them
            assert not re.search(r"\s(set|remove)\s+\d", cmd), cmd
    assert fault.device not in ("ISP-A", "ISP-B")
    for device, _ in fault.kick:
        assert device not in ("ISP-A", "ISP-B")


# How each fault shows up in the live output Layer 3 reads (formats taken from the fixture):
# VyOS quotes values, RouterOS sorts attributes, Alpine's route has extra fields.
LIVE_EDITS = {
    1: [("running", "virtual-link 10.255.0.2", "virtual-link 172.16.10.9")],
    2: [("export", "/system identity", "/ip firewall filter add action=drop chain=forward protocol=ospf\n/system identity")],
    3: [("export", "set [ find default-name=ether1 ] disable-running-check=no",
         "set [ find default-name=ether1 ] disable-running-check=no mtu=1400")],
    4: [("running", "interface eth0 authentication md5 key-id 1 md5-key 'LabArea0Key'",
         "interface eth0 authentication md5 key-id 1 md5-key 'LabArea0key'")],
    5: [("export", " type=stub", "")],
    6: [("running", "range 10.1.0.0/16", "range 10.1.0.0/24")],
    7: [("export", " connection-state=established,related", "")],
    8: [("running", "set policy route-map ISP-A-IN rule 10 set local-preference '200'\n", ""),
        ("running", "set policy route-map ISP-B-IN rule 10 action 'permit'\n",
         "set policy route-map ISP-B-IN rule 10 action 'permit'\nset policy route-map ISP-B-IN rule 10 set local-preference '200'\n")],
    9: [("running", "default-information originate\n", "default-information originate always\n")],
    10: [("running", "rule 110 outbound-interface name 'eth2'", "rule 110 outbound-interface name 'eth1'")],
    11: [("export", "networks=10.1.2.0/24 passive", "networks=10.1.2.0/24")],
    12: [("interfaces", "10.1.1.11/24", "10.1.1.11/16"), ("addr", "10.1.1.11/24", "10.1.1.11/16"),
         ("route", "10.1.1.0/24 dev eth0", "10.1.0.0/16 dev eth0")],
}


def _live_diffs(device, nums):
    outputs = dict(BASELINE["layer3"][device])
    for n in nums:
        for key, old, new in LIVE_EDITS[n]:
            assert outputs[key].count(old) == 1, (n, old)
            outputs[key] = outputs[key].replace(old, new)
    model = layer3.live_model(DEVICE_BY_NAME[device]["platform"], outputs)
    from verifier.model import diff_models
    return diff_models(INTENT[device], model)


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f"fault{f.num}")
def test_live_readback_recognises_the_fault(fault):
    states, unexplained = fault_states(fault.device, _live_diffs(fault.device, [fault.num]), INTENT[fault.device])
    assert states[fault.num] == "injected"
    assert all(s == "clean" for n, s in states.items() if n != fault.num)
    assert not unexplained


@pytest.mark.parametrize("device", FAULT_DEVICES)
def test_live_readback_with_every_fault_on_the_device(device):
    nums = [f.num for f in faults_on(device)]
    diffs = _live_diffs(device, nums)
    assert {diff_key(d) for d in diffs} == expected_diffs(device, nums, INTENT[device])


def test_baseline_readback_is_clean():
    for device in FAULT_DEVICES:
        states, unexplained = fault_states(device, _live_diffs(device, []), INTENT[device])
        assert set(states.values()) == {"clean"} and not unexplained


def test_guest_holes_cover_all_32_deny_pairs():
    from fnmatch import fnmatchcase
    denied = {c["id"] for c in BASELINE_CHECKS if c["category"] == "ping_denied"}
    holes = {i for i in CHECK_IDS if any(fnmatchcase(i, g) for g in BY_NUM[7].expect if g.startswith("L1."))}
    assert holes == denied and len(denied) == 32


def test_grade_match_and_mismatch():
    fault = BY_NUM[10]
    checks = [dict(c) for c in BASELINE_CHECKS]
    for c in checks:
        if c["id"].endswith("->1.1.1.1") or c["id"] == "L3.config.HQ-Edge":
            c["status"] = "fail"
    assert grade(fault, checks)["verdict"] == "match"

    checks[0]["status"] = "fail"
    g = grade(fault, checks)
    assert g["verdict"] == "mismatch" and g["unexpected"] == [checks[0]["id"]]

    for c in checks:
        if c["id"] == "L3.config.HQ-Edge":
            c["status"] = "pass"
    assert "L3.config.HQ-Edge" in grade(fault, checks)["missed"]


def test_grade_ignores_layers_not_run():
    fault = BY_NUM[10]
    checks = [dict(c, status="fail") if c["id"] == "L3.config.HQ-Edge" else c
              for c in BASELINE_CHECKS if c["layer"] == 3]
    g = grade(fault, checks)
    assert g["verdict"] == "match" and not g["unmatched_globs"]
