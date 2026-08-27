"""BIOS + Save-file management: catalog the canonical system/saves stores and deploy them
to agents. BIOS = a RetroArch `system/` tree (wholesale-mirrored to each agent's RetroArch
system dir). Saves = per-standalone-emulator save data (Dolphin/3DS/Xbox/Amiga)."""
from __future__ import annotations
import subprocess
from pathlib import Path
from app.core.config import settings
from app.services.push_launchbox import get_endpoint, _ssh, _scp, _ps_encoded, load_endpoints

SYSTEM_DIR = Path(settings.canonical_path) / "system"
SAVES_DIR = Path(settings.canonical_path) / "gamesaves"   # per-system game-save backup
_BIOS_EXT = {".bin", ".rom", ".zip", ".img", ".bios", ".nds", ".sys", ".e", ".u", ".pup", ".mec"}


def _fmt(nbytes: int) -> str:
    v = float(nbytes)
    for unit in ("B", "K", "M", "G", "T"):
        if v < 1024 or unit == "T":
            return f"{v:.0f}{unit}" if unit in ("B", "K") else f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}T"


def _dir_stats(root: Path) -> tuple[int, int]:
    files = size = 0
    if root.exists():
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                size += p.stat().st_size
    return files, size


def inventory() -> dict:
    """Catalog the BIOS (system/) tree + saves, grouped for the UI."""
    bfiles, bsize = _dir_stats(SYSTEM_DIR)
    top_bios = sorted(p.name for p in SYSTEM_DIR.glob("*")
                      if p.is_file() and p.suffix.lower() in _BIOS_EXT) if SYSTEM_DIR.exists() else []
    subdirs = []
    if SYSTEM_DIR.exists():
        for d in sorted(SYSTEM_DIR.glob("*")):
            if d.is_dir():
                f, s = _dir_stats(d)
                subdirs.append({"name": d.name, "files": f, "size": _fmt(s)})
    saves = []
    if SAVES_DIR.exists():
        for d in sorted(SAVES_DIR.glob("*")):
            if d.is_dir():
                f, s = _dir_stats(d)
                saves.append({"emulator": d.name, "files": f, "size": _fmt(s)})
    agents = [{"name": n, "host": e.host} for n, e in load_endpoints().items()]
    return {
        "bios": {"files": bfiles, "size": _fmt(bsize), "top_bios_count": len(top_bios),
                 "top_bios": top_bios, "subdirs": subdirs},
        "saves": {"total_files": sum(s["files"] for s in saves), "by_emulator": saves},
        "agents": agents,
        "migrated": SYSTEM_DIR.exists(),
    }


def _tar_push(ep, src: Path, remote_subdir: str, tag: str, exclude: list[str] | None = None) -> dict:
    """tar a canonical dir, scp it, and extract into <lb_path>\\<remote_subdir> on the agent."""
    if not src.exists():
        raise RuntimeError(f"{src} not migrated yet")
    tgz = Path("/tmp") / f"romfleet_{tag}.tgz"
    args = ["tar", "czf", str(tgz), "-C", str(src)]
    for x in (exclude or []):
        args += ["--exclude", x]
    args.append(".")
    tp = subprocess.run(args, capture_output=True, text=True)
    if tp.returncode != 0:
        raise RuntimeError(f"tar failed: {tp.stderr.strip()}")
    remote_tgz = f"C:/Users/{ep.ssh_user}/.romfleet_{tag}.tgz"
    sp = _scp(ep, str(tgz), remote_tgz)
    tgz.unlink(missing_ok=True)
    if sp.returncode != 0:
        raise RuntimeError(f"scp failed: {sp.stderr.strip()}")
    lb = ep.lb_path.rstrip("\\")
    target = f"{lb}\\{remote_subdir}"
    script = (
        "$ErrorActionPreference='Stop';"
        f"$t='{target}'; if(-not (Test-Path $t)){{New-Item -ItemType Directory -Force -Path $t | Out-Null}};"
        f"tar xzf '{remote_tgz}' -C $t;"
        f"Remove-Item '{remote_tgz}' -Force -ErrorAction SilentlyContinue;"
        "Write-Output 'ok'"
    )
    rp = _ssh(ep, _ps_encoded(script))
    if rp.returncode != 0 or "ok" not in (rp.stdout or ""):
        raise RuntimeError(f"remote extract failed: {(rp.stderr or rp.stdout).strip()[:200]}")
    return {"target": target, "bytes": None}


# After the wholesale system\ sync, copy the right BIOS into each standalone emulator's own
# dir (they don't read RetroArch's system\). Source is relative to the just-synced RA system dir
# on the agent; only applied if that emulator folder exists. dest is under Emulators\<emu>\.
_STANDALONE_BIOS = [
    ("Duckstation", "bios", ["scph*.bin", "psxonpsp660.bin", "ps-*.bin"]),   # PSX
    ("PCSX2", "bios", ["ps2\\*"]),                                            # PS2
    ("Flycast", "data", ["dc\\*"]),                                          # Dreamcast/NAOMI/Atomiswave
]


def push_bios(endpoint: str) -> dict:
    """Mirror the whole canonical system/ tree into the agent's RetroArch system dir, then
    fan the PSX/PS2/DC BIOS out to the standalone emulators (Duckstation/PCSX2/Flycast)."""
    ep = get_endpoint(endpoint)
    r = _tar_push(ep, SYSTEM_DIR, "Emulators\\RetroArch\\system", f"bios_{endpoint}")
    # fan-out to standalone emulators
    lb = ep.lb_path.rstrip("\\")
    ra_sys = f"{lb}\\Emulators\\RetroArch\\system"
    emu = f"{lb}\\Emulators"
    lines = ["$ErrorActionPreference='SilentlyContinue';", "$out=@();"]
    for name, sub, globs in _STANDALONE_BIOS:
        dest = f"{emu}\\{name}\\{sub}"
        srcs = ",".join(f"'{ra_sys}\\{g}'" for g in globs)
        lines.append(
            f"if(Test-Path '{emu}\\{name}'){{"
            f"New-Item -ItemType Directory -Force -Path '{dest}' | Out-Null;"
            f"Copy-Item -Path {srcs} -Destination '{dest}' -Recurse -Force;"
            f"$out+=('{name}='+((Get-ChildItem '{dest}' -Recurse -File|Measure-Object).Count))}}")
    lines.append("Write-Output ($out -join ' ')")
    fan = _ssh(ep, _ps_encoded("".join(lines)))
    files, size = _dir_stats(SYSTEM_DIR)
    return {"ok": True, "endpoint": endpoint, "target": r["target"], "files": files, "size": _fmt(size),
            "standalone": (fan.stdout or "").strip()}


# Game saves are per-SYSTEM. Cartridge systems keep flat RetroArch .srm/.sav/.state that go to
# RetroArch's central saves dir. Disc/standalone systems route to their emulator's save dir.
_RA_SAVE_SYS = ["gb", "gba", "gbc", "nes", "snes", "pcengine", "neogeocd", "satellaview",
                "sgb", "vectrex", "gg", "gamegear", "megadrive", "genesis", "sega32x",
                "mastersystem", "n64", "megacd", "saturn", "pcenginecd", "ngp", "ngpc",
                "wonderswan", "wonderswancolor", "lynx", "gamegear"]
# system-subdir -> (emulator, dest-subpath, inner source glob to copy)
_STANDALONE_SAVE = [
    ("ps2", "PCSX2", "memcards", "pcsx2\\*"),      # ps2/pcsx2/Mcd*.ps2
    ("wii", "Dolphin", "User", "User\\*"),          # wii/User/*
    ("dolphin-emu", "Dolphin", "User", "*"),        # dolphin-emu/* (Dolphin User contents)
    ("psx", "Duckstation", "memcards", "*"),        # psx/* (Duckstation memcards)
]


def push_saves(endpoint: str) -> dict:
    """Stage the per-system game-save backup to <lb>\\Saves\\gamesaves\\, then apply each
    (CLOBBERING) into the right place: cartridge systems -> RetroArch\\saves\\ (flat .srm),
    disc/standalone systems -> their emulator's save dir (if that emulator exists)."""
    ep = get_endpoint(endpoint)
    r = _tar_push(ep, SAVES_DIR, "Saves\\gamesaves", f"saves_{endpoint}")
    lb = ep.lb_path.rstrip("\\")
    g = f"{lb}\\Saves\\gamesaves"
    emu = f"{lb}\\Emulators"
    ra_saves = f"{emu}\\RetroArch\\saves"
    lines = ["$ErrorActionPreference='SilentlyContinue';", "$out=@();"]
    # cartridge systems -> RetroArch\saves\ (flat)
    lines.append(f"New-Item -ItemType Directory -Force -Path '{ra_saves}' | Out-Null; $ra=0;")
    for sys in _RA_SAVE_SYS:
        s = f"{g}\\{sys}"
        lines.append(
            f"if(Test-Path '{s}'){{Get-ChildItem '{s}' -File -Include *.srm,*.sav,*.state* -Recurse | "
            f"ForEach-Object{{Copy-Item $_.FullName '{ra_saves}' -Force; $ra++}}}}")
    lines.append("$out+=('RetroArch\\saves='+$ra);")
    # standalone/disc systems -> their emulator dir
    for sys, name, dest, glob in _STANDALONE_SAVE:
        s = f"{g}\\{sys}\\{glob}"        # e.g. gamesaves\ps2\pcsx2\* , gamesaves\wii\User\*
        base = f"{g}\\{sys}"
        d = f"{emu}\\{name}\\{dest}"
        lines.append(
            f"if((Test-Path '{emu}\\{name}') -and (Test-Path '{base}')){{"
            f"New-Item -ItemType Directory -Force -Path '{d}' | Out-Null;"
            f"Copy-Item -Path '{s}' -Destination '{d}' -Recurse -Force;"
            f"$out+='{sys}->{name}=applied'}}elseif(Test-Path '{base}'){{$out+='{sys}->{name}=skip(no-emu)'}}")
    lines.append("Write-Output ($out -join ' ')")
    fan = _ssh(ep, _ps_encoded("".join(lines)))
    files, size = _dir_stats(SAVES_DIR)
    return {"ok": True, "endpoint": endpoint, "staged": r["target"], "files": files, "size": _fmt(size),
            "routed": (fan.stdout or "").strip(), "stderr": (fan.stderr or "")[:150]}
