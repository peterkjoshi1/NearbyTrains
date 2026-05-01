#!/usr/bin/env python3
"""
trace_train.py — Watch server.log and trace all events for one headcode.

Usage:
    python3 server/trace_train.py 1L78
    python3 server/trace_train.py 1L78 --log server.log

Prints every CA_MSG, TRUST anchor, pending match, and interpolation
attempt for the given headcode as they appear in the live log.
"""

import sys
import time
import re
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("headcode", help="Train headcode to trace, e.g. 1L78")
parser.add_argument("--log", default="server.log", help="Log file to tail")
args = parser.parse_args()

headcode = args.headcode.upper()
log_path = Path(args.log)

print(f"Tracing {headcode!r} — tailing {log_path}")
print("-" * 60)

# Patterns to match
patterns = [
    (re.compile(rf"\[CA_MSG\] {re.escape(headcode)}\b"),        "TD berth step"),
    (re.compile(rf"headcode={re.escape(headcode)}\b"),           "TRUST anchor"),
    (re.compile(rf"\[LEARN\].*\b{re.escape(headcode)}\b"),       "learner"),
]

def tail(path):
    """Yield new lines appended to path, blocking until they appear."""
    with open(path, "r") as f:
        f.seek(0, 2)   # jump to end
        while True:
            line = f.readline()
            if line:
                yield line.rstrip()
            else:
                time.sleep(0.1)

if not log_path.exists():
    print(f"Waiting for {log_path} to appear …")
    while not log_path.exists():
        time.sleep(1)

for line in tail(log_path):
    for pattern, label in patterns:
        if pattern.search(line):
            ts = time.strftime("%H:%M:%S")
            print(f"{ts}  [{label}]  {line}")
            break
