"""Deploy per-game RetroArch overlays (e.g. Vectrex screen overlays) to an endpoint.

The Vectrex shipped a translucent plastic screen overlay per game; RetroArch's vecx core shows a
digital overlay via a per-game config. Assets live centrally under
  <canonical>/retroarch/<sub>/overlays/<Overlay>.cfg + <Overlay>.png
For each owned ROM that matches an overlay by name, we GENERATE
  config/<core>/<rom filename>.cfg   -> input_overlay = ":\\overlays\\<sub>\\<Overlay>.cfg"
(the overlay filenames use No-Intro names but RomFleet deploys RA names, so the per-game config
is generated to match the deployed ROM, not copied verbatim). Overlays + configs are pushed into
the endpoint's RetroArch (overlays\\<sub>\\ + config\\<core>\\). Paths use RetroArch's ':' base
variable, so they're endpoint-portable. Wired into push_system for overlay systems.
"""
from __future__ import annotations

import io
import os
import re
import tarfile
import tempfile
from pathlib import Path

import structlog

from app.core.config import settings

log = structlog.get_logger()

# system folder -> (overlays/<sub> subdir, RetroArch config/<core> dir)
OVERLAY_SYSTEMS = {"vectrex": ("vectrex", "vecx")}

_TAG = re.compile(r"\b(proto|hack|rev\s*\d*)\b", re.I)


def _norm(s: str) -> str:
    """Name key for matching: drop region parens + proto/hack/rev tags, keep sequel numbers."""
    s = re.sub(r"\([^)]*\)", "", s)
    s = _TAG.sub("", s.replace("_", " ").replace("-", " "))
    return re.sub(r"[^a-z0-9]", "", s.lower())


_CFG = ('input_overlay = ":\\overlays\\{sub}\\{ov}.cfg"\n'
        'input_overlay_enable = "true"\n'
        'input_overlay_opacity = "1.000000"\n'
        'input_overlay_hide_in_menu = "true"\n')


def _assets_dir(sub: str) -> Path:
    return Path(settings.canonical_path) / "retroarch" / sub / "overlays"


def plan_overlays(folder: str) -> dict:
    """{rom_filename_stem: overlay_name} for owned ROMs that have a matching overlay asset."""
    if folder not in OVERLAY_SYSTEMS:
        return {}
    sub, _core = OVERLAY_SYSTEMS[folder]
    assets = _assets_dir(sub)
    if not assets.is_dir():
        return {}
    overlays = {_norm(p.stem): p.stem for p in assets.glob("*.png")}
    from app.db.session import SessionLocal
    from app.db.models import RomFile
    db = SessionLocal()
    stems = {os.path.splitext(r.filename)[0]
             for r in db.query(RomFile).filter_by(system_folder=folder).all()}
    db.close()
    return {stem: overlays[_norm(stem)] for stem in stems if _norm(stem) in overlays}


def deploy_overlays(endpoint: str, folder: str = "vectrex") -> dict:
    """Push overlay assets + generated per-game configs to the endpoint's RetroArch."""
    if folder not in OVERLAY_SYSTEMS:
        return {"status": "no-overlay-system", "system": folder}
    from app.services.push_launchbox import get_endpoint, _ssh, _scp, _ps_encoded
    sub, core = OVERLAY_SYSTEMS[folder]
    assets = _assets_dir(sub)
    matches = plan_overlays(folder)
    if not assets.is_dir() or not matches:
        return {"status": "no-matches", "system": folder}
    ep = get_endpoint(endpoint)
    ra = f"{ep.lb_path.rstrip(chr(92))}\\Emulators\\RetroArch"

    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "rf_overlays.tgz"
        with tarfile.open(tgz, "w:gz") as tar:
            for p in sorted(assets.iterdir()):                 # overlay art + .cfg
                if p.is_file():
                    tar.add(p, arcname=f"overlays/{sub}/{p.name}")
            for stem, ov in sorted(matches.items()):           # generated per-game configs
                data = _CFG.format(sub=sub, ov=ov).encode()
                ti = tarfile.TarInfo(f"config/{core}/{stem}.cfg")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
        remote = f"{ra}\\rf_overlays.tgz"
        sp = _scp(ep, str(tgz), remote)
        if sp.returncode != 0:
            return {"status": "scp-failed", "system": folder, "err": sp.stderr.strip()[:150]}
        script = (
            f"$ra='{ra}'; if(!(Test-Path $ra)){{Write-Output 'no-retroarch'; return}};"
            f"tar xzf '{remote}' -C '{ra}';"
            f"Remove-Item '{remote}' -Force -EA SilentlyContinue;"
            "Write-Output 'overlays-ok'"
        )
        r = _ssh(ep, _ps_encoded(script))
    ok = "overlays-ok" in (r.stdout or "")
    log.info("overlays deployed", endpoint=endpoint, system=folder, overlays=len(matches), ok=ok)
    return {"status": "deployed" if ok else "error", "system": folder,
            "overlays": len(matches), "detail": (r.stdout or "").strip().splitlines()[-1:]}
