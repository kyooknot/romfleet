"""Organize a system's source ROMs to align with the chosen archive — SAFELY.

Non-destructive by contract:
  * DRY-RUN first: build_plan() computes every action and changes NOTHING.
  * APPLY: rename members to their canonical archive name (in place) and MOVE
    non-members / 1G1R extras into <root>/_quarantine/ — never a hard delete.
  * A manifest is written BEFORE any op so rollback() can reverse every move.

Members = ROMs whose hash is in the system's gate. Under 1G1R only each game's
scorer-preferred version is kept; the other owned versions are quarantined as extras.
Under full-set every matched version is kept.
"""
from __future__ import annotations
import errno
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.orm import Session, selectinload
from app.core.config import settings
from app.core.systems import get_system_by_folder, gate_list
from app.core.hash_priority import parse_and_score
from app.core.scoring import get_scoring, score_parsed
from app.db.models import System, Game, RomFile
import structlog

log = structlog.get_logger()

QUARANTINE = "_quarantine"
MANIFEST_GLOB = ".romfleet-organize-*.json"

# inner-ROM/disc extensions RA hash-names sometimes carry (e.g. "Growl (USA).md") — strip
# before appending the file's real container extension so we don't get "Growl (USA).md.zip".
_ROM_EXTS = (".md", ".gen", ".smd", ".bin", ".sfc", ".smc", ".nes", ".fds", ".gb", ".gbc",
             ".gba", ".n64", ".z64", ".v64", ".sms", ".gg", ".pce", ".a26", ".a78", ".lnx",
             ".ws", ".wsc", ".ngp", ".ngc", ".col", ".int", ".32x", ".iso", ".gcm", ".rvz",
             ".chd", ".cue", ".vec", ".d64", ".nds", ".vb", ".min", ".sv", ".sg", ".sc",
             ".gam", ".j64", ".jag", ".vboy", ".pce", ".tg16", ".fig", ".swc")


def canonical_filename(name: str, file_ext: str) -> str:
    """<db/RA name minus any inner-rom extension> + the file's real container extension."""
    low = (name or "").lower()
    for e in _ROM_EXTS:
        if low.endswith(e):
            name = name[: -len(e)]
            break
    return f"{name}{file_ext}"


def _effective(db: Session, folder: str):
    sysc = get_system_by_folder(folder)
    if not sysc:
        raise ValueError(f"Unknown system folder: {folder}")
    srow = db.query(System).filter_by(folder=folder).one_or_none()
    gate = gate_list(srow.gate if srow else None, sysc.gate)
    selection = (srow.selection if srow else None) or "1g1r"
    return sysc, gate, selection


def _canonical(rom: RomFile, gate: list[str]) -> tuple[str | None, str | None]:
    """The archive's canonical name for this ROM, following gate priority.
    Returns (source, name) or (None, None) if no gate provider named it."""
    dm = rom.dat_matches or {}
    for gp in gate:
        v = dm.get(gp)
        if isinstance(v, str) and v.strip():
            return gp, v.strip()
    # RA may record True (matched, no hash-name) — fall back to the game title
    if dm.get("ra") is True and rom.game:
        return "ra", rom.game.title
    return None, None


def _score(rom: RomFile, gate: list[str], cfg: dict) -> int:
    src, name = _canonical(rom, gate)
    s = score_parsed(parse_and_score(md5=rom.computed_hash or "", name=name or rom.filename,
                                     labels=[], patch_url=None), cfg)
    return s if s is not None else 10_000


# Multi-part sub-systems whose parts (broadcast episodes) are all kept, like discs.
_KEEP_ALL_1G1R = {"satellaview"}
# Name markers for multi-content that RA lumps under ONE game_id but are distinct ROMs to
# keep (MSU-1 audio packs; GBA Video carts — dozens of video carts share one RA game).
_KEEP_ALL_NAME = ("msu", "advance video")
_ENG_REGIONS = {"usa", "world", "europe", "australia", "canada", "uk"}
_VS_REG = (r"usa|europe|japan|world|asia|australia|canada|korea|china|brazil|spain|germany|"
           r"france|italy|netherlands|sweden|norway|denmark|finland|uk|russia|poland|taiwan|"
           r"hong kong|latin america|scandinavia|unknown|np|ntsc|pal")
_VS_LNG = r"en|ja|fr|de|es|it|nl|pt|sv|no|da|fi|zh|ko|ru|pl|cs|hu|el|tr"
_VS_EXT_RE = re.compile(r"\.[a-z0-9]{2,4}$", re.I)
_VS_REG_RE = re.compile(r"\s*\(((?:" + _VS_REG + r")(?:[,\s]+(?:" + _VS_REG + r"))*)\)", re.I)
_VS_LNG_RE = re.compile(r"\s*\(((?:" + _VS_LNG + r")(?:[,\s]+(?:" + _VS_LNG + r"))*)\)", re.I)
_VS_REV_RE = re.compile(r"\s*\((?:Rev\s*[\dA-Za-z]+|v[\d][\w.]*)\)", re.I)
# Fan-translation suffix at the END of a name: (En)[ (v1.0)] [ (Translator)] — collapse it
# so a translation shares a 1G1R slot with its untranslated base (English then wins on
# score). The trailing translator paren is guarded so it never eats a disc/part/episode tag.
_VS_XLAT_RE = re.compile(
    r"\s*\((?:" + _VS_LNG + r")(?:[,\s]+(?:" + _VS_LNG + r"))*\)"        # (En) / (En,Fr)
    r"(?:\s*\((?:Rev\s*[\dA-Za-z]+|v[\d][\w.]*)\))?"                     # optional (v1.0)/(Rev A)
    r"(?:\s*\((?!Disc|Disk|Part|Vol|Episode|Side|Tape|Set)[^)]*\))?"    # optional (Translator)
    r"\s*$", re.I)


def _version_slot(name: str) -> str:
    """The 1G1R 'slot': the name minus region/language/revision tags AND a trailing
    fan-translation suffix. Same slot = same content in different regions/revs/translations
    (collapse to one, best wins); different slot = a distinct disc / episode /
    hack-competition entry (keep all)."""
    n = _VS_EXT_RE.sub("", name)
    n = _VS_REG_RE.sub("", n)
    n = _VS_XLAT_RE.sub("", n)     # fan-translation suffix (En)(vX)(Translator) — collapse to base
    n = _VS_LNG_RE.sub("", n)
    n = _VS_REV_RE.sub("", n)
    return re.sub(r"\s+", " ", n).strip().lower()


def _is_english(rom: RomFile, gate: list[str]) -> bool:
    """English-playable = a native English region OR an English fan-translation."""
    _, name = _canonical(rom, gate)
    p = parse_and_score(md5=rom.computed_hash or "", name=name or rom.filename or "",
                        labels=[], patch_url=None)
    if getattr(p, "is_english_translation", False):
        return True
    return bool({r.lower() for r in (getattr(p, "regions", None) or [])} & _ENG_REGIONS)


_REV_RE = re.compile(r"\((Rev\s*[\dA-Za-z]+)\)", re.I)


def _is_keep_all_name(filename: str) -> bool:
    """MSU-1 audio hacks + GBA-Video multi-carts: distinct ROMs RA lumps under one game_id."""
    low = (filename or "").lower()
    return any(k in low for k in _KEEP_ALL_NAME)


def _preserve_rev(original: str, target: str, ext: str) -> str:
    """Re-append a (Rev X) tag the RA canonical name dropped, so revision identity survives."""
    m = _REV_RE.search(original or "")
    if not m:
        return target
    rev = m.group(1).strip()
    stem = target[: -len(ext)] if ext and target.lower().endswith(ext.lower()) else target
    if rev.lower() in stem.lower():
        return target
    return f"{stem} ({rev}){ext}"


def build_plan(db: Session, folder: str) -> dict:
    """Compute the organize plan. Reads only — mutates nothing."""
    sysc, gate, selection = _effective(db, folder)
    cfg = get_scoring(db)
    roms = db.query(RomFile).options(selectinload(RomFile.game)).filter_by(
        system_folder=folder).all()

    # skip anything already sitting in _quarantine (idempotent re-runs)
    roms = [r for r in roms if QUARANTINE not in Path(r.path).parts]

    # 1G1R = one VERSION per game, but keep every distinct part (disc / broadcast episode /
    # hack-competition entry) and every file of a multi-part sub-system (Satellaview) or an
    # MSU-1 game. Two files collapse only if they differ solely by region/language/revision;
    # among a slot's versions English (native or fan-translation) beats non-English.
    primary_ids: set[int] = set()
    if selection == "1g1r":
        by_game: dict = {}
        for r in roms:
            if r.match_status == "matched":
                by_game.setdefault(r.game_id, []).append(r)
        keep_all_folder = folder in _KEEP_ALL_1G1R
        for gid, group in by_game.items():
            if keep_all_folder or any(k in (r.filename or "").lower()
                                      for r in group for k in _KEEP_ALL_NAME):
                primary_ids.update(r.id for r in group)
                continue
            by_slot: dict = {}
            for r in group:
                # slot on the FILE name (No-Intro descriptive name) — it preserves the distinct
                # part/episode/entry that the game-level canonical name would collapse.
                by_slot.setdefault(_version_slot(r.filename or ""), []).append(r)
            for sg in by_slot.values():
                primary_ids.add(max(sg, key=lambda r: (_is_english(r, gate),
                                                       -_score(r, gate, cfg))).id)

    actions = []
    seen_targets: dict[str, int] = {}
    for r in roms:
        cur = Path(r.path)
        ext = cur.suffix
        is_member = r.match_status == "matched"
        keep = is_member and (selection == "fullset" or r.game_id is None or r.id in primary_ids)

        if keep:
            if _is_keep_all_name(r.filename or ""):
                # MSU-1 audio hacks / GBA-Video multi-carts: KEEP the full descriptive filename.
                # Collapsing to the RA base name loses the MSU-1/version identity, collides with
                # the base ROM, and orphans the name-matched .pcm audio pack — RA's base
                # "canonical" name is simply wrong for these distinct hacks.
                op, reason = "keep", "MSU-1 / multi-content — name preserved"
                target_name, target_path = r.filename, r.path
            else:
                src, name = _canonical(r, gate)
                target_name = canonical_filename(name, ext) if name else r.filename
                if name:
                    target_name = _preserve_rev(r.filename or "", target_name, ext)  # keep (Rev X)
                target_path = str(cur.parent / target_name)
                if target_name == r.filename:
                    op, reason = "keep", f"already canonical ({src})" if src else "kept"
                elif target_path in seen_targets:
                    op, reason = "keep", "name collision — left as-is"
                    target_path, target_name = r.path, r.filename
                else:
                    op, reason = "rename", f"→ {src} canonical name"
            seen_targets[target_path] = r.id
        else:
            reason = ("not in " + "/".join(gate)) if not is_member else "1G1R extra version"
            target_name = r.filename
            target_path = str(Path(settings.src_roms_path) / folder / QUARANTINE / r.filename)
            op = "quarantine"

        actions.append({"rom_id": r.id, "from": r.filename, "from_path": r.path,
                        "to": target_name, "to_path": target_path, "op": op, "reason": reason})

    summary = {"total": len(actions),
               "keep": sum(1 for a in actions if a["op"] == "keep"),
               "rename": sum(1 for a in actions if a["op"] == "rename"),
               "quarantine": sum(1 for a in actions if a["op"] == "quarantine")}
    return {"folder": folder, "gate": gate, "selection": selection,
            "summary": summary, "actions": actions}


def apply_plan(db: Session, folder: str) -> dict:
    """Execute a freshly-built plan. Writes a rollback manifest FIRST, then renames
    members and moves non-members to _quarantine. Updates RomFile paths to match."""
    plan = build_plan(db, folder)
    ops = [a for a in plan["actions"] if a["op"] in ("rename", "quarantine")]
    root = Path(settings.src_roms_path) / folder
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    manifest_path = root / f".romfleet-organize-{ts}.json"

    # write manifest BEFORE touching anything (rollback safety)
    manifest = {"folder": folder, "created": ts, "gate": plan["gate"],
                "selection": plan["selection"], "ops": ops}
    root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    (root / QUARANTINE).mkdir(exist_ok=True)
    done = errors = 0
    for a in ops:
        src_p, dst_p = Path(a["from_path"]), Path(a["to_path"])
        try:
            if not src_p.exists():
                a["result"] = "source missing — skipped"
                continue
            if dst_p.exists():
                if a["op"] == "quarantine":
                    # a prior read-only-source run already copied this into _quarantine but
                    # couldn't delete the read-only source; finish the job by dropping the row.
                    r = db.get(RomFile, a["rom_id"])
                    if r:
                        db.delete(r)
                    done += 1
                    a["result"] = "already in _quarantine — dropped from catalog"
                    continue
                a["result"] = "target exists — skipped"
                continue
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_p), str(dst_p))
            r = db.get(RomFile, a["rom_id"])
            if r:
                r.path = str(dst_p)
                r.filename = dst_p.name
            done += 1
            a["result"] = "ok"
        except OSError as e:
            # Read-only source master (a canonical-cutover leftover — /mnt/src is mounted ro):
            # the file can't be moved. For a QUARANTINE (a non-member/extra we're removing
            # anyway) just drop it from the catalog so the UI reflects the cleanup — the pristine
            # file is left on the RO master and goes with that system's source-master purge; a
            # re-scan restores it if needed.
            if a["op"] == "quarantine" and e.errno in (errno.EROFS, errno.EACCES):
                r = db.get(RomFile, a["rom_id"])
                if r:
                    db.delete(r)
                done += 1
                a["result"] = "removed from catalog (source read-only)"
            else:
                errors += 1
                a["result"] = f"error: {str(e)[:120]}"
        except Exception as e:  # noqa
            errors += 1
            a["result"] = f"error: {str(e)[:120]}"
            log.warning("organize op failed", op=a["op"], src=str(src_p), error=str(e)[:200])
    db.commit()
    # re-persist manifest with per-op results so rollback skips no-ops cleanly
    manifest["ops"] = ops
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"folder": folder, "manifest": manifest_path.name,
            "applied": done, "errors": errors, "summary": plan["summary"]}


DOLPHIN_TOOL = "/usr/games/dolphin-tool"
COMPRESSIBLE = (".iso", ".gcm")   # GC/Wii raw images that RVZ can losslessly shrink


def _system_compress(db: Session, folder: str) -> str:
    row = db.query(System).filter_by(folder=folder).one_or_none()
    return (row.compress if row and row.compress else "none")


def _to_rvz(src: str, dst: str) -> None:
    """Losslessly convert a GC/Wii disc image to RVZ (zstd L5, 128KB blocks = the standard
    RVZ profile). RVZ decompresses to the identical image so the RA hash is unchanged."""
    r = subprocess.run([DOLPHIN_TOOL, "convert", "-i", src, "-o", dst,
                        "-f", "rvz", "-c", "zstd", "-l", "5", "-b", "131072"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError(f"dolphin-tool rvz failed: {(r.stderr or '').strip()[:200]}")


def build_publish_plan(db: Session, folder: str) -> dict:
    """Plan MOVING the library keepers (gated + selection primary) out of the source store
    into the canonical library romfleet/roms/<folder>/ with canonical names. Leftovers
    (non-members + 1G1R extras) stay in source for the user to purge. Reads only."""
    plan = build_plan(db, folder)
    compress = _system_compress(db, folder)
    canon_root = Path(settings.canonical_path) / "roms" / folder
    ops = []
    total = 0
    for a in plan["actions"]:
        if a["op"] not in ("keep", "rename"):
            continue  # leftovers remain in source
        src_p = Path(a["from_path"])
        # compress: GC/Wii .iso/.gcm publish as .rvz (converted, not copied)
        to_name = a["to"]
        convert = compress == "rvz" and Path(a["from"]).suffix.lower() in COMPRESSIBLE
        if convert:
            to_name = str(Path(a["to"]).with_suffix(".rvz"))
        already = str(src_p).startswith(str(canon_root) + "/")
        try:
            size = src_p.stat().st_size
        except OSError:
            size = 0
        if not already:
            total += size
        ops.append({"rom_id": a["rom_id"], "from": a["from"], "from_path": a["from_path"],
                    "to": to_name, "to_path": str(canon_root / to_name), "convert": convert,
                    "op": "published" if already else "publish", "size": size})
    return {"folder": folder, "gate": plan["gate"], "selection": plan["selection"],
            "compress": compress, "canonical_root": str(canon_root),
            "summary": {"publish": sum(1 for o in ops if o["op"] == "publish"),
                        "convert": sum(1 for o in ops if o.get("convert") and o["op"] == "publish"),
                        "already": sum(1 for o in ops if o["op"] == "published"),
                        "leftover": plan["summary"]["quarantine"], "bytes": total},
            "actions": ops}


def _prior_published(folder: str) -> dict[int, str]:
    """rom_id -> last canonical path it was published to (from publish manifests). Lets a
    re-publish RENAME an already-published file in place when its canonical name changed,
    instead of re-copying gigabytes."""
    prior: dict[int, str] = {}
    mdir = Path(settings.canonical_path) / "roms"
    for m in sorted(mdir.glob(f".romfleet-publish-{folder}-*.json")):  # ascending → latest wins
        try:
            for o in json.loads(m.read_text()).get("ops", []):
                if o.get("result") in (None, "ok", "renamed"):
                    prior[o["rom_id"]] = o["to_path"]
        except Exception:  # noqa
            continue
    return prior


def apply_publish(db: Session, folder: str) -> dict:
    """COPY the keepers into the canonical library with canonical names. The source store is
    a read-only master, so this is a copy (never a move): source stays pristine, canonical
    roms/<folder>/ is the clean projected library agents consume. Idempotent — re-running
    skips files already published. Manifest is written to the (writable) canonical store."""
    plan = build_publish_plan(db, folder)
    canon_root = Path(plan["canonical_root"])
    canon_root.mkdir(parents=True, exist_ok=True)
    ops = [o for o in plan["actions"] if o["op"] == "publish"]
    # read PRIOR published paths BEFORE writing this run's manifest (else we'd read our own
    # new paths back and never detect renames).
    prior = _prior_published(folder)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    manifest_dir = Path(settings.canonical_path) / "roms"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f".romfleet-publish-{folder}-{ts}.json"
    manifest = {"folder": folder, "created": ts, "kind": "publish-copy",
                "gate": plan["gate"], "selection": plan["selection"], "ops": ops}
    manifest_path.write_text(json.dumps(manifest, indent=2))

    done = errors = skipped = renamed = converted = 0
    copied_bytes = 0
    for o in ops:
        src_p, dst_p = Path(o["from_path"]), Path(o["to_path"])
        try:
            if dst_p.exists():
                o["result"] = "already published — skipped"
                skipped += 1
                continue
            old = prior.get(o["rom_id"])
            if o.get("convert"):
                # .iso/.gcm → .rvz. Prefer converting an already-published uncompressed copy
                # (local) over re-reading source; drop that copy once the .rvz exists.
                conv_src = old if (old and Path(old).exists()
                                   and Path(old).suffix.lower() in COMPRESSIBLE) else str(src_p)
                if not Path(conv_src).exists():
                    o["result"] = "source missing — skipped"
                    continue
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                _to_rvz(conv_src, str(dst_p))
                if old and conv_src == old:
                    Path(old).unlink(missing_ok=True)
                converted += 1
                o["result"] = "converted→rvz"
                continue
            if old and old != o["to_path"] and Path(old).exists():
                # canonical name changed (e.g. after enrichment) — rename in place, no re-copy
                dst_p.parent.mkdir(parents=True, exist_ok=True)
                Path(old).rename(dst_p)
                renamed += 1
                o["result"] = "renamed"
                continue
            if not src_p.exists():
                o["result"] = "source missing — skipped"
                continue
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_p), str(dst_p))  # copy, not move — source is the read-only master
            done += 1
            copied_bytes += o.get("size", 0)
            o["result"] = "ok"
        except Exception as e:  # noqa
            errors += 1
            o["result"] = f"error: {str(e)[:120]}"
            log.warning("publish copy/convert failed", src=str(src_p), error=str(e)[:200])
    # RomFile.path intentionally left on the source master — canonical roms/ is a render.
    manifest["ops"] = ops
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"folder": folder, "manifest": manifest_path.name, "published": done,
            "renamed": renamed, "converted": converted, "skipped": skipped, "errors": errors,
            "bytes_moved": copied_bytes, "canonical_root": str(canon_root)}


def quarantine_status(folder: str) -> dict:
    """Count + total bytes currently sitting in the system's _quarantine bin."""
    qdir = Path(settings.src_roms_path) / folder / QUARANTINE
    files = [p for p in qdir.rglob("*") if p.is_file()] if qdir.is_dir() else []
    return {"folder": folder, "count": len(files),
            "bytes": sum(p.stat().st_size for p in files)}


def purge_quarantine(db: Session, folder: str) -> dict:
    """Reclaim space: DELETE everything in _quarantine (the reviewed non-members/extras),
    writing a purge log first (audit trail of what was removed) and dropping the DB rows.
    This is the one irreversible step — only quarantined files, only on explicit confirm."""
    root = Path(settings.src_roms_path) / folder
    qdir = root / QUARANTINE
    if not qdir.is_dir():
        return {"folder": folder, "purged": 0, "bytes_freed": 0, "rows_removed": 0}

    files = [p for p in qdir.rglob("*") if p.is_file()]
    entries = [{"name": str(p.relative_to(qdir)), "size": p.stat().st_size} for p in files]
    total = sum(e["size"] for e in entries)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (root / f".romfleet-purge-{ts}.json").write_text(json.dumps(
        {"folder": folder, "created": ts, "count": len(files), "bytes": total, "files": entries}, indent=2))

    purged = 0
    for p in files:
        try:
            p.unlink()
            purged += 1
        except Exception as e:  # noqa
            log.warning("purge unlink failed", file=str(p), error=str(e)[:120])
    # remove now-empty subdirs + the bin itself
    for dpath in sorted((p for p in qdir.rglob("*") if p.is_dir()), key=lambda x: len(x.parts), reverse=True):
        try:
            dpath.rmdir()
        except OSError:
            pass
    try:
        qdir.rmdir()
    except OSError:
        pass

    # drop the DB rows whose file we just deleted (LIKE would treat '_' as a wildcard — filter in Python)
    removed = 0
    for r in db.query(RomFile).filter_by(system_folder=folder).all():
        if QUARANTINE in Path(r.path).parts:
            db.delete(r)
            removed += 1
    db.commit()
    return {"folder": folder, "purged": purged, "bytes_freed": total,
            "rows_removed": removed, "log": f".romfleet-purge-{ts}.json"}


def list_manifests(folder: str) -> list[dict]:
    root = Path(settings.src_roms_path) / folder
    out = []
    mans = list(root.glob(MANIFEST_GLOB)) + list(root.glob(".romfleet-publish-*.json"))
    for m in sorted(mans, key=lambda x: x.name, reverse=True):
        try:
            d = json.loads(m.read_text())
            out.append({"manifest": m.name, "created": d.get("created"),
                        "kind": d.get("kind", "organize"),
                        "gate": d.get("gate"), "selection": d.get("selection"),
                        "ops": len(d.get("ops", []))})
        except Exception:  # noqa
            continue
    return out


def rollback(db: Session, folder: str, manifest_name: str | None = None) -> dict:
    """Reverse an organize run: move every file back to its original path. If no manifest
    is named, the most recent one is used."""
    root = Path(settings.src_roms_path) / folder
    if manifest_name:
        mpath = root / manifest_name
    else:
        cands = sorted(root.glob(MANIFEST_GLOB), reverse=True)
        if not cands:
            raise ValueError("no organize manifest to roll back")
        mpath = cands[0]
    if not mpath.exists():
        raise ValueError(f"manifest not found: {mpath.name}")

    manifest = json.loads(mpath.read_text())
    restored = skipped = 0
    for a in reversed(manifest.get("ops", [])):
        if a.get("result") not in (None, "ok"):
            skipped += 1
            continue
        cur_p, orig_p = Path(a["to_path"]), Path(a["from_path"])
        try:
            if not cur_p.exists() or orig_p.exists():
                skipped += 1
                continue
            orig_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur_p), str(orig_p))
            r = db.get(RomFile, a["rom_id"])
            if r:
                r.path = str(orig_p)
                r.filename = orig_p.name
            restored += 1
        except Exception as e:  # noqa
            skipped += 1
            log.warning("rollback op failed", error=str(e)[:200])
    db.commit()
    mpath.rename(mpath.with_suffix(".json.rolledback"))
    return {"folder": folder, "manifest": mpath.name, "restored": restored, "skipped": skipped}
