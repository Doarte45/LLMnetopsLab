"""Three-layer verifier for the v2 lab.

Layer 1 pings from the hosts, Layer 2 reads control-plane state from the routers,
and Layer 3 compares each device's configuration to the intent in v2/configs.
Collection (SSH) and analysis are kept apart so that saved raw output can be
re-graded offline with verify_lab.py --from-raw.
"""
