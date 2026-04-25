"""Unit-test the safe unified diff helper."""
from digitorn.modules.workspace.module import _safe_unified_diff

baseline = "line one\nline two\nline three"  # NO trailing newline
current = "line one\nLINE TWO\nline three\nline four"

diff = _safe_unified_diff(baseline, current, "notes.txt")
print("DIFF:")
print(repr(diff))
print()
print("RENDERED:")
print(diff)
print("---")

bad_chars = [' ', '-', '+', '@']
bad_chars.append(chr(92))  # backslash
for i, line in enumerate(diff.rstrip("\n").split("\n")):
    if not line:
        continue
    if line[0] not in bad_chars:
        print(f"  BAD line {i}: {line!r}")
    else:
        print(f"  OK line {i}: {line[:50]!r}")
