"""Push a single system's rendered LaunchBox bundle to a configured endpoint.

RomFleet is the control plane; this deploys ONE system at a time to a Windows LaunchBox
install over SSH (server -> endpoint), independent of whether that endpoint has the
canonical share mapped in the session doing the copy. Per system:

  1. render_platform(system) -> refresh Data/Platforms/<Platform>.xml + Images/<Platform>
     in the endpoint's server-side bundle dir.
  2. tar the platform's XML + media subtree(s) into a single archive.
  3. scp the archive to the endpoint.
  4. ssh: close LaunchBox/BigBox (they rewrite Platform XML on exit), back up the target
     Platform XML, extract the archive into the LaunchBox dir, remove the archive.

The endpoint's sshd default shell is PowerShell; remote logic is sent as a base64
-EncodedCommand to sidestep all cross-shell quoting.
"""
from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
import structlog

from app.core.config import settings
from app.services.render_launchbox import render_platform, platform_for

log = structlog.get_logger()

_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "LogLevel=ERROR", "-o", "IdentitiesOnly=yes",
             "-o", "ConnectTimeout=10"]


@dataclass
class Endpoint:
    name: str
    host: str
    ssh_user: str
    ssh_key: str
    lb_path: str          # Windows LaunchBox dir, e.g. C:\Users\<user>\LaunchBox
    rom_base: str         # Windows roms root, e.g. J:\roms
    bundle_dir: str       # server-side render bundle dir


def load_endpoints() -> dict[str, Endpoint]:
    p = Path(settings.config_path) / "endpoints.yaml"
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    return {name: Endpoint(name=name, **cfg)
            for name, cfg in (data.get("endpoints") or {}).items()}


def get_endpoint(name: str) -> Endpoint:
    eps = load_endpoints()
    if name not in eps:
        raise ValueError(f"unknown endpoint '{name}' (have: {', '.join(eps) or 'none'})")
    return eps[name]


def _ssh(ep: Endpoint, remote_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", ep.ssh_key, *_SSH_OPTS, f"{ep.ssh_user}@{ep.host}", remote_cmd],
        capture_output=True, text=True)


def _scp(ep: Endpoint, local: str, remote: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["scp", "-i", ep.ssh_key, *_SSH_OPTS, local, f"{ep.ssh_user}@{ep.host}:{remote}"],
        capture_output=True, text=True)


def _ps_encoded(script: str) -> str:
    """Wrap a PowerShell script as a -EncodedCommand invocation (quote-proof over ssh)."""
    b = base64.b64encode(script.encode("utf-16-le")).decode()
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {b}"


def _load_lb_cores() -> dict:
    import json
    p = Path(__file__).resolve().parents[1] / "data" / "lb_cores.json"
    try:
        return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}
    except Exception:
        return {}


# RetroArch loads zipped carts AND raw disc images (.chd) natively — it wants the file
# passed AS-IS. LaunchBox's default AutoExtract=true pre-unpacks the .zip and hands the
# core loose files, which breaks launching (arcade romsets can't load; and it's pointless
# for single-cart cores). ensure_emulator only ever runs for RetroArch platforms (those in
# lb_cores.json), so AutoExtract=false is correct for ALL of them — not just arcade.
_NO_EXTRACT = {"arcade", "neogeo", "naomi", "naomi2", "atomiswave"}   # retained for reference


def ensure_emulator(endpoint: str, platform: str, no_extract: bool = False) -> dict:
    """Auto-add a default RetroArch EmulatorPlatform association for `platform` on the
    endpoint if it has none, using the mapped libretro core — so a freshly-deployed system
    'just works' without hand-setting the core. No-op if already associated, if there's no
    RetroArch emulator, or if the platform has no mapped core (standalone-emulator systems).
    no_extract=True forces AutoExtract=false — arcade romsets (.zip) must be passed to FBNeo
    as archives; LaunchBox's default extract breaks them (loose files, no romset)."""
    core = _load_lb_cores().get(platform)
    if not core:
        return {"platform": platform, "status": "no-core-mapping"}
    # LaunchBox holds Emulators.xml in memory and rewrites it on exit — any external edit made
    # while it's running gets silently clobbered (and merged into malformed double -L). Defer.
    if endpoint_busy(endpoint):
        return {"platform": platform, "core": core, "status": "deferred-launchbox-running"}
    ep = get_endpoint(endpoint)
    xmlp = f"{ep.lb_path.rstrip(chr(92))}\\Data\\Emulators.xml"
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p='{xmlp}'; if(!(Test-Path $p)){{Write-Output 'no-emulators-xml'; return}};"
        "[xml]$x=Get-Content $p -Raw;"
        "$ra=@($x.LaunchBox.Emulator | ? {$_.Title -eq 'RetroArch'})[0];"
        "if(-not $ra){Write-Output 'no-retroarch'; return};"
        f"$plat='{platform}'; $noext=${'true' if no_extract else 'false'};"
        # LaunchBox's core dropdown only recognises the RELATIVE 'cores\<core>' form (relative to
        # retroarch.exe). An absolute path launches fine but shows as 'no core' in the UI.
        f"$cmd='-L \"cores\\{core}\" -f';"
        "$ex=@($x.LaunchBox.EmulatorPlatform | ? {$_.Emulator -eq $ra.ID -and $_.Platform -eq $plat})[0];"
        "if($ex){"
        "  $chg=$false;"
        # leave only a single CLEAN-relative core (-L \"cores\\...\", ours or a deliberate other);
        # rewrite absolute, LB-root-relative (Emulators\\RetroArch\\cores\\...), double, or empty.
        "  $lc=([regex]::Matches([string]$ex.CommandLine,'-L\\s')).Count;"
        "  $rel=([string]$ex.CommandLine) -like '*-L \"cores\\*';"
        "  if(-not ($lc -eq 1 -and $rel)){"
        "    if($ex.CommandLine){$ex.CommandLine=$cmd}else{$c=$x.CreateElement('CommandLine');$c.InnerText=$cmd;[void]$ex.AppendChild($c)};"
        "    if(-not $ex.Default){$d=$x.CreateElement('Default');$d.InnerText='true';[void]$ex.AppendChild($d)}; $chg=$true};"
        # arcade .zip romsets must NOT be extracted (FBNeo needs the archive) — force AutoExtract=false
        "  if($noext){$ae=$ex.SelectSingleNode('AutoExtract'); if($ae){if($ae.InnerText -ne 'false'){$ae.InnerText='false';$chg=$true}}else{$a=$x.CreateElement('AutoExtract');$a.InnerText='false';[void]$ex.AppendChild($a);$chg=$true}};"
        "  if($chg){Copy-Item $p (\"{0}.bak-{1}\" -f $p,(Get-Date -f 'yyyyMMdd-HHmmss')) -Force; $x.Save($p); Write-Output 'updated'}else{Write-Output 'already-associated'}; return"
        "};"
        "Copy-Item $p (\"{0}.bak-{1}\" -f $p,(Get-Date -f 'yyyyMMdd-HHmmss')) -Force;"
        "$ep=$x.CreateElement('EmulatorPlatform');"
        "@(@('Emulator',$ra.ID),@('Platform',$plat),@('CommandLine',$cmd),@('Default','true')) | % {"
        "$e=$x.CreateElement($_[0]); $e.InnerText=[string]$_[1]; [void]$ep.AppendChild($e)};"
        "if($noext){$a=$x.CreateElement('AutoExtract'); $a.InnerText='false'; [void]$ep.AppendChild($a)};"
        "[void]$x.LaunchBox.AppendChild($ep); $x.Save($p); Write-Output 'associated'"
    )
    r = _ssh(ep, _ps_encoded(script))
    return {"platform": platform, "core": core,
            "status": (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "error",
            "stderr": r.stderr.strip()[:150]}


def endpoint_busy(endpoint: str) -> bool:
    """True if LaunchBox/BigBox is currently running on the endpoint — auto-push defers
    rather than kill a session the user is actively using."""
    try:
        ep = get_endpoint(endpoint)
    except ValueError:
        return False
    r = _ssh(ep, _ps_encoded("@(Get-Process LaunchBox,BigBox -EA SilentlyContinue).Count"))
    try:
        return int((r.stdout or "0").strip() or "0") > 0
    except ValueError:
        return False


def stamp_game_emulator(endpoint: str, platform: str) -> dict:
    """Write each <Game>'s <Emulator> in the deployed Platforms/<platform>.xml to the
    platform's associated emulator GUID (preferring the Default=true association).

    This LaunchBox build shell-opens a game to Explorer (File Browser) when its <Emulator>
    is blank — even with a valid EmulatorPlatform association — and render intentionally
    omits the field, so push_system's step-4 XML overwrite re-blanks it on EVERY push. So
    this must re-run after each deploy. Works for RetroArch AND standalone emulators
    (RAppleWin, PCSX2, Dolphin…) since it reads whatever emulator is associated. No-op if the
    platform has no association yet (nothing to point games at — a manual/standalone setup
    step still pending)."""
    if endpoint_busy(endpoint):
        return {"platform": platform, "status": "deferred-launchbox-running"}
    ep = get_endpoint(endpoint)
    lb = ep.lb_path.rstrip("\\")
    emup = f"{lb}\\Data\\Emulators.xml"
    platx = f"{lb}\\Data\\Platforms\\{platform}.xml"
    script = (
        "$ErrorActionPreference='Stop';"
        f"$ep='{emup}'; $pp='{platx}'; $plat='{platform}';"
        "if(!(Test-Path $ep) -or !(Test-Path $pp)){Write-Output 'no-xml'; return};"
        "[xml]$e=Get-Content $ep -Raw;"
        "$assoc=@($e.LaunchBox.EmulatorPlatform | ? {$_.Platform -eq $plat});"
        "if($assoc.Count -eq 0){Write-Output 'no-association'; return};"
        "$def=@($assoc | ? {$_.Default -eq 'true'})[0]; if(-not $def){$def=$assoc[0]};"
        "$guid=[string]$def.Emulator;"
        "if(-not $guid){Write-Output 'no-guid'; return};"
        "[xml]$g=Get-Content $pp -Raw; $n=0;"
        "foreach($game in $g.LaunchBox.Game){"
        "$em=$game.SelectSingleNode('Emulator');"
        "if(-not $em){$em=$g.CreateElement('Emulator');[void]$game.AppendChild($em)};"
        "if($em.InnerText -ne $guid){$em.InnerText=$guid; $n++}};"
        "$g.Save($pp); Write-Output (\"stamped-$n->$guid\")"
    )
    r = _ssh(ep, _ps_encoded(script))
    return {"platform": platform,
            "status": (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "error",
            "stderr": r.stderr.strip()[:150]}


def _appleii_stage_disks(ep: Endpoint) -> dict:
    """appleii ONLY post-deploy step: stage each game's PRIMARY (boot) disk as a loose LOCAL
    file at C:\\Apple2Disks\\<zipbase>.<ext> on the endpoint. RAppleWin can't read disk images
    off the J: SMB share and LaunchBox AutoExtract mishandles the .m3u inside multi-file Apple
    II zips, so the appleii Game ApplicationPath (render_launchbox appleii special-case)
    targets these loose LOCAL files.

    The boot disks are extracted from the zips SERVER-SIDE (the mapped J: drive is
    per-interactive-session and Unavailable in the deploy SSH session) and shipped as a small
    tarball over scp, then extracted to C:\\Apple2Disks — the same reliable server->endpoint
    transfer push_system uses for the XML/media (no SMB dependency). Same primary rule as
    render (first name-sorted inner disk-image entry, .m3u excluded) via the shared helper, so
    the staged filenames == the rendered ApplicationPaths. Idempotent: overwrites; the extract
    never deletes other files, so multi-disc AdditionalApplication disks are left untouched.
    Only the Game primary is staged. Called only for system == 'appleii'."""
    import shutil as _sh
    import tempfile as _tf
    import zipfile as _zip
    from pathlib import Path
    from app.services.render_launchbox import (
        _owned_games, _disc_num, appleii_primary_entry)
    from app.db.session import SessionLocal

    roms_root = Path(settings.src_roms_path) / "appleii"
    plan: list[tuple[Path, str, str]] = []   # (zip_path, inner_entry, target_filename)
    db = SessionLocal()
    try:
        for game in _owned_games(db, "appleii"):
            roms = [r for r in game.roms
                    if r.system_folder == "appleii" and r.match_status == "matched"]
            if not roms:
                continue
            roms.sort(key=lambda r: (_disc_num(r.filename), r.filename))
            fn = roms[0].filename
            if not fn.lower().endswith(".zip"):
                continue
            prim = appleii_primary_entry(roms_root / fn)
            if not prim:
                continue
            entry, ext = prim
            plan.append((roms_root / fn, entry, f"{fn[:-4]}.{ext}"))
    finally:
        db.close()
    if not plan:
        return {"status": "no-appleii-games", "staged": 0}

    # 1. extract each primary disk server-side under its TARGET name into a staging tree
    tmpdir = Path(_tf.mkdtemp(prefix="appleii_stage_"))
    disk_dir = tmpdir / "Apple2Disks"
    disk_dir.mkdir()
    staged = 0
    try:
        for zpath, entry, target in plan:
            try:
                with _zip.ZipFile(zpath) as zf, zf.open(entry) as src, \
                        open(disk_dir / target, "wb") as dst:
                    _sh.copyfileobj(src, dst)
                staged += 1
            except Exception:  # skip an unreadable zip rather than abort the batch
                continue
        # 2. tar the staging tree (server side)
        tgz = Path(str(tmpdir) + ".tgz")
        tp = subprocess.run(["tar", "czf", str(tgz), "-C", str(tmpdir), "Apple2Disks"],
                            capture_output=True, text=True)
        if tp.returncode != 0:
            return {"status": "tar-failed", "staged": staged, "stderr": tp.stderr.strip()[:150]}
        # 3. scp to endpoint, then remote-extract to C:\ (-> C:\Apple2Disks\<target>)
        remote_tgz = f"C:/Users/{ep.ssh_user}/.romfleet_appleii.tgz"
        sp = _scp(ep, str(tgz), remote_tgz)
        tgz.unlink(missing_ok=True)
        if sp.returncode != 0:
            return {"status": "scp-failed", "staged": staged, "stderr": sp.stderr.strip()[:150]}
        script = (
            "$ErrorActionPreference='Stop';"
            "if(!(Test-Path 'C:\\Apple2Disks')){New-Item -ItemType Directory "
            "-Path 'C:\\Apple2Disks' -Force | Out-Null};"
            f"tar xzf '{remote_tgz}' -C 'C:\\';"
            f"Remove-Item '{remote_tgz}' -Force -ErrorAction SilentlyContinue;"
            "Write-Output 'appleii-stage-ok'"
        )
        r = _ssh(ep, _ps_encoded(script))
    finally:
        _sh.rmtree(tmpdir, ignore_errors=True)
    ok = "appleii-stage-ok" in (r.stdout or "")
    return {"status": "ok" if ok else "remote-extract-failed", "staged": staged,
            "remote_stdout": (r.stdout or "").strip()[:120],
            "stderr": r.stderr.strip()[:150]}


def ensure_scrape_as(endpoint: str, platform: str, scrape_as: str) -> dict:
    """Set <ScrapeAs>`scrape_as`</ScrapeAs> on `platform`'s definition in the endpoint's
    Data\\Platforms.xml. This makes a custom LaunchBox platform (e.g. "Sega Naomi") borrow
    another platform's identity ("Arcade") for metadata scraping AND — critically — for
    LaunchBox's native RetroAchievements console mapping, so LB hashes the ROM as arcade
    (MD5 of the romset name = RA console 27) and surfaces achievement badges in BigBox.

    Like Emulators.xml, LaunchBox holds Platforms.xml in memory and rewrites it on exit, so
    an edit made while it's running is clobbered — defer if the session is live. Idempotent."""
    if not scrape_as:
        return {"platform": platform, "status": "no-scrape-as"}
    if endpoint_busy(endpoint):
        return {"platform": platform, "scrape_as": scrape_as,
                "status": "deferred-launchbox-running"}
    ep = get_endpoint(endpoint)
    xmlp = f"{ep.lb_path.rstrip(chr(92))}\\Data\\Platforms.xml"
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p='{xmlp}'; if(!(Test-Path $p)){{Write-Output 'no-platforms-xml'; return}};"
        "[xml]$x=Get-Content $p -Raw;"
        f"$plat='{platform}'; $sa='{scrape_as}';"
        "$pf=@($x.LaunchBox.Platform | ? {$_.Name -eq $plat})[0];"
        "if(-not $pf){Write-Output 'no-platform-node'; return};"
        "$n=$pf.SelectSingleNode('ScrapeAs');"
        "if($n -and $n.InnerText -eq $sa){Write-Output 'already-set'; return};"
        "Copy-Item $p (\"{0}.bak-{1}\" -f $p,(Get-Date -f 'yyyyMMdd-HHmmss')) -Force;"
        "if($n){$n.InnerText=$sa}else{$e=$x.CreateElement('ScrapeAs');$e.InnerText=$sa;[void]$pf.AppendChild($e)};"
        "$x.Save($p); Write-Output 'updated'"
    )
    r = _ssh(ep, _ps_encoded(script))
    return {"platform": platform, "scrape_as": scrape_as,
            "status": (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "error",
            "stderr": r.stderr.strip()[:150]}


def push_system(system: str, endpoint: str = "", *, dry_run: bool = False,
                xml_only: bool = False) -> dict:
    """Render `system` and deploy just that platform to `endpoint`'s LaunchBox.

    xml_only: skip media entirely — render + transfer only the Platform XML (the fast
    path for metadata/favorite/curation edits, where media didn't change). The endpoint
    already has the media from a prior full push.
    """
    endpoint = endpoint or settings.default_endpoint
    ep = get_endpoint(endpoint)
    platform = platform_for(system)
    if not platform:
        raise ValueError(f"no LaunchBox platform mapping for '{system}'")

    # 0. pull this platform's current Favorite flags back into RomFleet BEFORE we
    #    overwrite the endpoint's XML, so a re-push never clobbers a favorite the user
    #    set in the frontend (central store then renders it back in below).
    if not dry_run:
        try:
            from app.services.favorites_sync import sync_favorites
            sync_favorites(endpoint, systems=[system])
        except Exception as e:  # sync is best-effort; never block a deploy on it
            log.warning("favorite pre-sync failed", system=system, err=str(e))

    # 1. render fresh into the endpoint's bundle (central is_favorite + per-endpoint <Hide>)
    stats = render_platform(system, ep.rom_base, ep.bundle_dir, dry_run=False,
                            media=not xml_only, endpoint=endpoint)

    bundle = Path(ep.bundle_dir)
    members = [f"Data/Platforms/{platform}.xml"]
    if not xml_only:
        for root in ("Images", "Videos", "Manuals"):
            if (bundle / root / platform).is_dir():
                members.append(f"{root}/{platform}")

    # 2. tar the platform's files (server side)
    tgz = Path("/tmp") / f"romfleet_push_{system}.tgz"
    tp = subprocess.run(["tar", "czf", str(tgz), "-C", str(bundle), *members],
                        capture_output=True, text=True)
    if tp.returncode != 0:
        raise RuntimeError(f"tar failed: {tp.stderr.strip()}")

    result = {"system": system, "platform": platform, "endpoint": endpoint,
              "games": stats.games, "hidden": stats.hidden,
              "additional_apps": stats.additional_apps,
              "media_copied": stats.media_copied, "members": members,
              "xml_only": xml_only, "tarball_bytes": tgz.stat().st_size}

    if dry_run:
        tgz.unlink(missing_ok=True)
        result["dry_run"] = True
        return result

    # 3. scp to endpoint
    remote_tgz = f"C:/Users/{ep.ssh_user}/.romfleet_push_{system}.tgz"
    sp = _scp(ep, str(tgz), remote_tgz)
    tgz.unlink(missing_ok=True)
    if sp.returncode != 0:
        raise RuntimeError(f"scp failed: {sp.stderr.strip()}")

    # 4. remote: close LB/BB, back up target xml, extract, cleanup
    lb = ep.lb_path.rstrip("\\")
    xml_win = f"{lb}\\Data\\Platforms\\{platform}.xml"
    # Purge this platform's stale media ONLY on a full push, so the extract is a clean sync
    # (old flyers, extension-changed box art, removed videos don't linger). An xml_only push
    # ships NO media — purging here would DELETE all box art with nothing to restore it.
    media_purge = "" if xml_only else (
        f"Remove-Item '{lb}\\Images\\{platform}' -Recurse -Force -ErrorAction SilentlyContinue;"
        f"Remove-Item '{lb}\\Videos\\{platform}' -Recurse -Force -ErrorAction SilentlyContinue;"
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "Stop-Process -Name LaunchBox,BigBox -Force -ErrorAction SilentlyContinue;"
        f"$xml='{xml_win}';"
        "if(Test-Path $xml){Copy-Item $xml (\"{0}.bak-{1}\" -f $xml,(Get-Date -Format 'yyyyMMdd-HHmmss')) -Force};"
        + media_purge +
        f"tar xzf '{remote_tgz}' -C '{lb}';"
        f"Remove-Item '{remote_tgz}' -Force -ErrorAction SilentlyContinue;"
        "Write-Output 'push-extract-ok'"
    )
    rp = _ssh(ep, _ps_encoded(script))
    result["remote_stdout"] = rp.stdout.strip()
    result["remote_stderr"] = rp.stderr.strip()
    result["ok"] = rp.returncode == 0 and "push-extract-ok" in rp.stdout
    if not result["ok"]:
        raise RuntimeError(f"remote extract failed (rc={rp.returncode}): "
                           f"{rp.stdout.strip()} {rp.stderr.strip()}")
    # re-baseline this endpoint's play_sync to the canonical play_count we just wrote out — so the
    # next sync-IN, which reads back the value we just deployed, contributes a ZERO delta (the
    # anti-double-count invariant). Best-effort: never fail a deploy on bookkeeping.
    try:
        from app.services.favorites_sync import writeback_playcount
        result["play_writeback"] = writeback_playcount(endpoint, systems=[system])
    except Exception as e:  # noqa
        log.warning("playcount writeback failed", system=system, err=str(e))
    # record what this agent now has deployed (feeds the Agents status page)
    try:
        from app.db.session import SessionLocal
        from app.db.models import Deployment
        d_db = SessionLocal()
        try:
            d = d_db.query(Deployment).filter_by(endpoint=endpoint, system_folder=system).one_or_none()
            if not d:
                d = Deployment(endpoint=endpoint, system_folder=system)
                d_db.add(d)
            from app.db.models import _now
            d.games, d.additional_apps, d.hidden = stats.games, stats.additional_apps, stats.hidden
            d.pushed_at = _now()   # always bump: onupdate won't fire when counts are unchanged
            d_db.commit()
        finally:
            d_db.close()
    except Exception as e:  # never fail a deploy on bookkeeping
        log.warning("deployment record failed", system=system, err=str(e))
    # ensure the endpoint has a default RetroArch core for this platform (full pushes only —
    # autopush's fast xml-only pushes skip it; the association is idempotent)
    if not xml_only:
        try:
            result["emulator"] = ensure_emulator(endpoint, platform, no_extract=True)  # RetroArch: always pass file as-is
        except Exception as e:  # noqa
            log.warning("ensure_emulator failed", system=system, err=str(e))
        # ScrapeAs override (naomi/naomi2/atomiswave -> Arcade): make LaunchBox map these
        # platforms to RA console 27 so its native RetroAchievements matcher shows badges.
        try:
            from app.core.systems import get_system_by_folder
            _sc = get_system_by_folder(system)
            if _sc and _sc.scrape_as:
                result["scrape_as"] = ensure_scrape_as(endpoint, platform, _sc.scrape_as)
        except Exception as e:  # noqa
            log.warning("ensure_scrape_as failed", system=system, err=str(e))
        # per-game RetroArch overlays (Vectrex screen overlays) — assets + generated configs
        from app.services.retroarch_overlays import OVERLAY_SYSTEMS
        if system in OVERLAY_SYSTEMS:
            try:
                from app.services.retroarch_overlays import deploy_overlays
                result["overlays"] = deploy_overlays(endpoint, system)
            except Exception as e:  # noqa
                log.warning("deploy_overlays failed", system=system, err=str(e))
    # Stamp per-game <Emulator> so LaunchBox launches via the associated emulator instead of
    # shell-opening the ROM to Explorer. Runs on EVERY push (full and xml_only) because step-4's
    # extract overwrites the platform XML and render leaves <Emulator> blank. After ensure_emulator
    # above (which, for RetroArch platforms, has created the association this reads).
    try:
        result["game_emulator"] = stamp_game_emulator(endpoint, platform)
    except Exception as e:  # never fail a deploy on the stamp
        log.warning("stamp_game_emulator failed", system=system, err=str(e))
    # appleii ONLY: stage each game's boot disk as a loose LOCAL file on the endpoint
    # (C:\Apple2Disks\<zipbase>.<ext>) so the local ApplicationPaths that render emits
    # resolve — RAppleWin can't read the J: share. Idempotent; gated strictly to appleii
    # so no other system's deploy runs it.
    if system == "appleii":
        try:
            result["appleii_stage"] = _appleii_stage_disks(ep)
        except Exception as e:  # never fail a deploy on the stage
            log.warning("appleii stage disks failed", system=system, err=str(e))
    log.info("push_system", system=system, platform=platform, endpoint=endpoint,
             games=stats.games, hidden=stats.hidden, bytes=result["tarball_bytes"])
    return result
