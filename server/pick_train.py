#!/usr/bin/env python3
"""
pick_train.py — Scan server.log and suggest the most active headcodes to trace.

Usage:
    python3 server/pick_train.py
    python3 server/pick_train.py --log server.log --top 10
"""

import re
import argparse
from pathlib import Path
from collections import Counter

parser = argparse.ArgumentParser()
parser.add_argument("--log", default="server.log")
parser.add_argument("--top", type=int, default=10)
args = parser.parse_args()

log_path = Path(args.log)
if not log_path.exists():
    print(f"No log file at {log_path}")
    raise SystemExit(1)

ca_pattern  = re.compile(r"\[CA_MSG\] (\w+)")
trust_pattern = re.compile(r"headcode=(\w+)")

ca_counts    = Counter()
trust_counts = Counter()

for line in log_path.read_text().splitlines():
    m = ca_pattern.search(line)
    if m:
        ca_counts[m.group(1)] += 1
    m = trust_pattern.search(line)
    if m:
        trust_counts[m.group(1)] += 1

# Score: trains with both TD berth steps and TRUST anchors are best candidates
both = {h for h in ca_counts if h in trust_counts}
td_only = {h for h in ca_counts if h not in trust_counts}

print(f"{'HEADCODE':<10} {'TD steps':>8} {'TRUST anchors':>14}  {'STATUS'}")
print("-" * 50)

# Show trains with both first (best for learning)
candidates = sorted(both, key=lambda h: ca_counts[h] + trust_counts[h] * 3, reverse=True)
for h in candidates[:args.top]:
    print(f"{h:<10} {ca_counts[h]:>8} {trust_counts[h]:>14}  ← best to trace")

if td_only:
    print()
    print("TD only (no TRUST anchors yet):")
    for h, n in ca_counts.most_common(5):
        if h in td_only:
            print(f"  {h:<10} {n} steps")
