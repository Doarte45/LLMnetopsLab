"""Check records shared by the three layers.

status is one of:
  pass   the expected result
  fail   a hard failure
  flaky  failed once and passed on the retry (Layer 1)
  error  the data needed for the check couldn't be collected
"""


def check(layer, check_id, status, expected=None, observed=None, detail="", device=None, category=None):
    return {
        "id": check_id,
        "layer": layer,
        "category": category or check_id.split(".")[1],
        "device": device,
        "status": status,
        "expected": expected,
        "observed": observed,
        "detail": detail,
    }


def passfail(ok):
    return "pass" if ok else "fail"
