"""Offline tests for score_run.py: per-fault verdicts on saved verifier output.

Fixtures: baseline.raw.json is the correct lab, broken.raw.json the lab after
fault_lab.py inject all (results/verify/reference_broken), symptoms.json the symptom table
from the single-fault runs (results/faults/reference_single_faults).

Run from v2/scripts:  python -m pytest tests -q
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pytest  # noqa: E402

from score_run import analyze, load_symptoms, score  # noqa: E402
from verifier import layer3  # noqa: E402

CONFIGS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "configs")
INTENT = layer3.load_intent(CONFIGS)
SYMPTOMS = load_symptoms(os.path.join(HERE, "fixtures", "symptoms.json"))


def load_raw(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)["raw"]


CLEAN, BROKEN = load_raw("baseline.raw.json"), load_raw("broken.raw.json")
BROKEN_RESULTS = analyze(BROKEN, CONFIGS)


@pytest.fixture
def broken():
    return copy.deepcopy(BROKEN)


@pytest.fixture
def clean():
    return copy.deepcopy(CLEAN)


def run_score(raw):
    return score(analyze(raw, CONFIGS), INTENT, SYMPTOMS, BROKEN_RESULTS)


def verdicts(s):
    return {n: f["verdict"] for n, f in s["faults"].items()}


def edit(raw, device, old, new):
    """Replace text in every Layer 3 output of a device that contains it."""
    hits = 0
    for key, text in raw["layer3"][device].items():
        if old in text:
            raw["layer3"][device][key] = text.replace(old, new)
            hits += 1
    assert hits, f"{old!r} not in {device}"


def test_correct_lab_scores_every_fault_fixed(clean):
    s = run_score(clean)
    assert s["fixed"] == 12 and s["clean"]
    assert s["collateral"] == [] and s["workarounds"] == []


def test_broken_baseline_scores_nothing_fixed(broken):
    s = run_score(broken)
    assert set(verdicts(s).values()) == {"not_fixed"}
    assert s["fixed"] == 0 and s["workarounds"] == [] and s["collateral"] == []


def test_fix_with_symptoms_masked_by_another_fault(broken):
    # #10's symptom is 1.1.1.1 unreachable, which #4 (still injected) also causes
    edit(broken, "HQ-Edge", "outbound-interface name 'eth1'", "outbound-interface name 'eth2'")
    f = run_score(broken)["faults"][10]
    assert f["verdict"] == "fixed" and f["symptoms_failing"] == []
    # Branch-2 and Branch-4 hosts are also cut off by #3 and #1/#2/#5, so #4 is one of the maskers
    assert f["symptoms_masked"] and all(4 in ms for ms in f["symptoms_masked"].values())


def test_config_fixed_but_symptoms_remain_is_ineffective(broken):
    # The saved checks still show #3's Branch-2 pings failing, and no other fault explains them
    edit(broken, "Branch-2", "disable-running-check=no mtu=1400", "disable-running-check=no")
    f = run_score(broken)["faults"][3]
    assert f["verdict"] == "ineffective" and f["config"] == "clean" and f["symptoms_failing"]


def test_wrong_value_is_changed_not_fixed(broken):
    edit(broken, "Hub-A", "md5-key 'LabArea0key'", "md5-key 'SomethingElse'")
    f = run_score(broken)["faults"][4]
    assert f["verdict"] == "changed"
    assert [(d["path"], d["live"]) for d in f["config_diffs"]] == [("ospf.interfaces.eth0.auth.key", "SomethingElse")]


def test_right_link_added_next_to_wrong_one_is_partial(broken):
    for key in ("running", "saved"):
        broken["layer3"]["Hub-B"][key] += ("\nset protocols ospf area 10 virtual-link 10.255.0.2 "
                                            "authentication md5 key-id 1 md5-key 'LabArea0Key'")
    assert run_score(broken)["faults"][1]["verdict"] == "partial"


def test_workaround_does_not_unfix_anything(clean):
    clean["layer3"]["Branch-1"]["export"] += "\n/ip route add dst-address=10.2.0.0/16 gateway=172.16.10.1\n"
    s = run_score(clean)
    assert s["fixed"] == 12 and not s["clean"]
    assert [(w["device"], w["kind"]) for w in s["workarounds"]] == [("Branch-1", "extra")]


def test_leftover_empty_ospf_interface_is_not_a_workaround(clean):
    for key in ("running", "saved"):
        clean["layer3"]["HQ-Edge"][key] += "\nset protocols ospf interface eth1"
    assert run_score(clean)["clean"]


def test_collateral_damage(clean):
    results = analyze(clean, CONFIGS)
    check = next(c for c in results["checks"] if c["id"] == "L1.ping.B1-PC1->B3-PC1")
    check["status"] = "fail"
    s = score(results, INTENT, SYMPTOMS, BROKEN_RESULTS)
    assert s["collateral"] == ["L1.ping.B1-PC1->B3-PC1"] and not s["clean"]


def test_unreachable_device_is_error(broken):
    broken["layer3"]["Hub-B"] = {"error": "timed out"}
    assert run_score(broken)["faults"][1]["verdict"] == "error"
