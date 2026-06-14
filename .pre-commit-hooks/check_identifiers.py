#!/usr/bin/env python3
"""Block staged files containing real infra identifiers.
Patterns come from a gitignored .denylist at the repo root (one regex per line,
# comments allowed). If .denylist is absent, the hook is a no-op."""
import sys, re, os
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
deny = os.path.join(root, ".denylist")
if not os.path.exists(deny):
    sys.exit(0)
pats = [l.strip() for l in open(deny, encoding="utf-8") if l.strip() and not l.startswith("#")]
if not pats:
    sys.exit(0)
rx = re.compile("|".join(pats), re.I)
bad = []
for f in sys.argv[1:]:
    try:
        for i, line in enumerate(open(f, encoding="utf-8", errors="ignore"), 1):
            if rx.search(line):
                bad.append(f"  {f}:{i}: {line.strip()[:100]}")
    except (IsADirectoryError, FileNotFoundError):
        pass
if bad:
    print("BLOCKED -- real infra identifiers in staged files:")
    print("\n".join(bad))
    print("Sanitize/unstage, or bypass with: git commit --no-verify")
    sys.exit(1)
sys.exit(0)
