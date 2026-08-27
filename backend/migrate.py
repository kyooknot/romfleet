"""Phase-1 additive migrations (until Alembic lands). Idempotent."""
from app.db.session import engine
from app.db.models import Base
from sqlalchemy import text

with engine.begin() as c:
    c.execute(text("ALTER TABLE rom_files ADD COLUMN IF NOT EXISTS dat_matches json DEFAULT '{}'::json"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS category varchar"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS lbdb_id integer"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS tgdb_id integer"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS source_ids json DEFAULT '{}'::json"))
    c.execute(text("ALTER TABLE media_assets ADD COLUMN IF NOT EXISTS is_locked boolean DEFAULT false"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS source_names json DEFAULT '{}'::json"))
    c.execute(text("ALTER TABLE media_assets ADD COLUMN IF NOT EXISTS content_md5 varchar"))
    # generalized membership: per-game anchor source + non-RA grouping key
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS gate_source varchar DEFAULT 'ra'"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS group_key varchar"))
    c.execute(text("CREATE INDEX IF NOT EXISTS ix_games_group_key ON games (group_key)"))
    c.execute(text("CREATE INDEX IF NOT EXISTS ix_games_gate_source ON games (gate_source)"))
    # bidirectional play-state sync (favorites_sync extension): accumulated play_count,
    # fleet-wide last_played, RA-sourced playtime_minutes, and per-surface last-observed
    # counts (play_sync) for delta-accumulation without double-counting.
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS play_count integer DEFAULT 0"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS last_played timestamptz"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS playtime_minutes integer"))
    c.execute(text("ALTER TABLE games ADD COLUMN IF NOT EXISTS play_sync json DEFAULT '{}'::json"))
    # per-system gate/selection overrides
    c.execute(text("ALTER TABLE systems ADD COLUMN IF NOT EXISTS gate varchar"))
    c.execute(text("ALTER TABLE systems ADD COLUMN IF NOT EXISTS selection varchar DEFAULT '1g1r'"))
    c.execute(text("ALTER TABLE systems ADD COLUMN IF NOT EXISTS compress varchar DEFAULT 'none'"))
Base.metadata.create_all(engine)  # create any new tables (media_assets etc.)

# fold legacy lbdb_id/tgdb_id columns into source_ids
from sqlalchemy.orm import Session
from app.db.models import Game
with Session(engine) as s:
    for g in s.query(Game).filter((Game.lbdb_id.isnot(None)) | (Game.tgdb_id.isnot(None))).all():
        sid = dict(g.source_ids or {})
        if g.lbdb_id and "lbdb" not in sid:
            sid["lbdb"] = g.lbdb_id
        if g.tgdb_id and "tgdb" not in sid:
            sid["tgdb"] = g.tgdb_id
        g.source_ids = sid
    s.commit()
print("migrated: source_ids ensured + legacy ids folded in")

# normalize existing game categories to canonical labels (e.g. "ROM Hack" -> "Hack")
from app.core.categories import normalize_category
with Session(engine) as s:
    changed = 0
    for g in s.query(Game).filter(Game.category.isnot(None)).all():
        n = normalize_category(g.category)
        if n != g.category:
            g.category = n
            changed += 1
    s.commit()
    print(f"normalized categories: {changed} games")
