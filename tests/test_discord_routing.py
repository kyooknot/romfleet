"""Routing tests for Discord notifications. No DB, no network — a fake Setting store.

Run: python3 tests/test_discord_routing.py
"""
import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

# app.db.models.Setting is a SQLAlchemy model; stub it so this needs no DB driver.
mod = types.ModuleType("app.db.models")
class Setting:
    def __init__(self, key=None, value=None): self.key, self.value = key, value
mod.Setting = Setting
sys.modules.setdefault("app.db", types.ModuleType("app.db"))
sys.modules["app.db.models"] = mod
# flag_modified() is ALWAYS stubbed, present or not. Doing this only when sqlalchemy is
# missing made the suite pass on a dev box without SQLAlchemy and fail on the deployed one,
# where the real flag_modified rejects the fake Setting
# ("'Setting' object has no attribute '_sa_instance_state'"). A test whose result depends on
# whether an unrelated dependency happens to be installed is not testing what it claims to.
try:
    import sqlalchemy.orm.attributes as _sa_attrs  # noqa
    _sa_attrs.flag_modified = lambda *a, **k: None
except Exception:
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm")
    attrs = types.ModuleType("sqlalchemy.orm.attributes")
    attrs.flag_modified = lambda *a, **k: None
    sys.modules.update({"sqlalchemy": sa, "sqlalchemy.orm": orm,
                        "sqlalchemy.orm.attributes": attrs})

from app.core import dat_sources as ds  # noqa: E402


class FakeDB:
    """Mimics just the Session surface dat_sources uses."""
    def __init__(self): self.rows = {}
    def get(self, _model, key): return self.rows.get(key)
    def add(self, row): self.rows[row.key] = row
    def commit(self): pass


def fresh():
    db = FakeDB()
    ds.set_discord_webhook(db, "https://discord.test/legacy")
    return db


def test_legacy_webhook_is_the_default_route():
    """An install that never touched the new settings must behave exactly as before."""
    db = fresh()
    for kind in ds.EVENT_KINDS:
        assert ds.route_for(db, kind) == "https://discord.test/legacy", kind


def test_disabling_a_kind_silences_only_that_kind():
    db = fresh()
    cfg = ds.get_notify_config(db)
    cfg["events"]["subset"]["enabled"] = False
    ds.set_notify_config(db, cfg)
    assert ds.route_for(db, "subset") is None
    assert ds.route_for(db, "new_set") == "https://discord.test/legacy"


def test_a_second_webhook_receives_only_what_is_routed_to_it():
    db = fresh()
    cfg = ds.get_notify_config(db)
    cfg["webhooks"]["quiet"] = {"url": "https://discord.test/quiet", "label": "Quiet"}
    cfg["events"]["roundup"]["webhook"] = "quiet"
    ds.set_notify_config(db, cfg)
    assert ds.route_for(db, "roundup") == "https://discord.test/quiet"
    assert ds.route_for(db, "new_set") == "https://discord.test/legacy"


def test_routing_to_an_undefined_webhook_falls_back_to_default():
    """A dangling reference must not silently black-hole an event."""
    db = fresh()
    ds.set_notify_config(db, {"webhooks": {"default": {"url": "https://discord.test/legacy"}},
                              "events": {"delisted": {"enabled": True, "webhook": "ghost"}}})
    assert ds.route_for(db, "delisted") == "https://discord.test/legacy"


def test_unknown_kind_still_posts_rather_than_vanishing():
    """A newly added notify_* that nobody registered should be noisy, not silent."""
    db = fresh()
    assert ds.route_for(db, "_test") == "https://discord.test/legacy"


def test_empty_webhook_url_means_do_not_post():
    db = FakeDB()
    ds.set_discord_webhook(db, "")
    assert ds.route_for(db, "new_set") is None


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL  {name}: {e}")
            except Exception as e:  # noqa
                fails += 1; print(f"  ERROR {name}: {e!r}")
    print(f"\n{'FAILED' if fails else 'ALL PASSED'} — {fails} failure(s)")
    sys.exit(1 if fails else 0)
