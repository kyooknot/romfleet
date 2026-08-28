"""A redundant "(Alt Dump)" must share a 1G1R slot with the file it duplicates, and lose.

No DB and no network: only the pure filename helpers are exercised, so app.db.models and the
SQLAlchemy Session import are stubbed the way test_discord_routing.py does it.

Run: python3 tests/test_alt_dump_slot.py
"""
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

# organize.py pulls in SQLAlchemy, structlog and the config/settings stack at import time.
# Only its pure filename helpers are under test, so every external module is stubbed
# UNCONDITIONALLY -- a test whose result depends on which dependencies happen to be installed
# is not testing what it claims to (see the note in test_discord_routing.py).
def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_any = lambda *a, **k: None                                          # noqa: E731
_stub("sqlalchemy"); _stub("sqlalchemy.orm", Session=object, selectinload=_any)
_stub("structlog", get_logger=lambda *a, **k: types.SimpleNamespace(
    info=_any, warning=_any, error=_any, debug=_any))
_stub("app.core.config", settings=types.SimpleNamespace())
_stub("app.core.systems", get_system_by_folder=_any, gate_list=_any)
_stub("app.core.hash_priority", parse_and_score=_any)
_stub("app.core.scoring", get_scoring=_any, score_parsed=_any)
_stub("app.db"); _stub("app.db.models", System=object, Game=object, RomFile=object)

from app.services.organize import _version_slot, is_alt_dump   # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# --- the pair collapses -------------------------------------------------------------------
for plain, alt in [
    ("Final Fantasy VII (USA) (Spirit Bug Fix) (Disc 2).chd",
     "Final Fantasy VII (USA) (Spirit Bug Fix) (Disc 2) (Alt Dump).chd"),
    ("Xenogears (USA) (Disc 2).chd", "Xenogears (USA) (Disc 2) (Alt Dump).chd"),
    ("Legend of Dragoon, The (USA) (Disc 1).chd",
     "Legend of Dragoon, The (USA) (Disc 1) (Alt Dump).chd"),
]:
    check(_version_slot(plain) == _version_slot(alt),
          f"alt dump did not share a slot with its original: {alt}")
    check(is_alt_dump(alt) and not is_alt_dump(plain), f"alt-dump tag not detected: {alt}")

# --- discs of one game must NEVER collapse into each other --------------------------------
check(_version_slot("Final Fantasy VII (USA) (Disc 1).chd")
      != _version_slot("Final Fantasy VII (USA) (Disc 2).chd"),
      "two different discs collapsed into one slot")
check(_version_slot("Legend of Dragoon, The (USA) (Disc 1) (Alt Dump).chd")
      != _version_slot("Legend of Dragoon, The (USA) (Disc 3).chd"),
      "an alt dump of disc 1 collapsed into disc 3")

# --- a bare "(Alt)" is an alternate RELEASE, not a redump: leave it alone ------------------
check(not is_alt_dump("Some Game (USA) (Alt).chd"), "bare (Alt) wrongly treated as a redump")

# --- a sole alt dump is the only copy: it must still be a normal, keepable entry -----------
check(_version_slot("LSD - Dream Emulator (Japan) (Alt Dump).chd") == "lsd - dream emulator",
      "sole alt dump did not normalise to the plain slot")

print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
