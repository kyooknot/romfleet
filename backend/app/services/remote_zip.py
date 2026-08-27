"""Read individual members out of a huge remote ZIP over HTTP range requests.

The Myrient "RetroAchievements" sets on archive.org are exactly the curated collections this
project wants — every ROM in them is one RA recognises — but each system ships as ONE zip:
PS2 alone is 79 GB (A-L) plus 60 GB (M-Z). archive.org will not index the members of a zip
that size, so its usual `<item>/<file>.zip/<member>` extraction view returns the site chrome
instead of a listing, and downloading 139 GB to obtain a 2 GB game is absurd.

A zip's central directory sits at the END of the file, so a couple of ranged GETs are enough to
list every member and then fetch just the one wanted. Verified against the PS2 M-Z set:
33 members listed by transferring 8 KB.

Notes that cost time to discover:
  * These are ZIP64 archives — the classic EOCD's 4-byte fields cannot address 60 GB, so the
    real offsets live in the ZIP64 EOCD record found via the ZIP64 locator (PK\\x06\\x07).
  * A ranged GET **must** return 206. archive.org intermittently answers 200 or 500 with an
    HTML error page, and parsing that as zip bytes yields absurd offsets (a central directory
    "3403721241372 MB" long) rather than an error. Every read insists on 206 and retries.
  * remotezip(1) fails on these items: it follows the redirect to a dnNNNNN.ca.archive.org
    node that 500s on ranged requests. Talking to archive.org/download directly works.
"""
from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass

import structlog

log = structlog.get_logger()

_EOCD64_LOC = b"PK\x06\x07"
_EOCD64 = b"PK\x06\x06"
_EOCD = b"PK\x05\x06"
_CDFH = b"PK\x01\x02"


@dataclass
class Member:
    name: str
    compress_type: int
    comp_size: int
    size: int
    header_offset: int


class RemoteZip:
    def __init__(self, url: str, cookies: str | None = None, ua: str = "RomFleet/1.0"):
        import requests
        self.url = url
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": ua})
        if cookies:
            self.s.headers["Cookie"] = cookies
        self._members: list[Member] | None = None

    def _range(self, spec: str, tries: int = 5) -> bytes:
        last = None
        for a in range(tries):
            r = self.s.get(self.url, headers={"Range": "bytes=%s" % spec}, timeout=300)
            last = r.status_code
            if r.status_code == 206:
                return r.content
            time.sleep(4 * (a + 1))
        raise RuntimeError("ranged read %s failed (last status %s)" % (spec, last))

    def members(self) -> list[Member]:
        if self._members is not None:
            return self._members
        tail = self._range("-65536")
        i = tail.rfind(_EOCD64_LOC)
        if i >= 0:
            off = struct.unpack("<Q", tail[i + 8:i + 16])[0]
            rec = self._range("%d-%d" % (off, off + 55))
            if rec[:4] != _EOCD64:
                raise RuntimeError("bad ZIP64 EOCD signature %r" % rec[:4])
            cd_size, cd_off = struct.unpack("<QQ", rec[40:56])
        else:                                   # small archive: classic EOCD
            j = tail.rfind(_EOCD)
            if j < 0:
                raise RuntimeError("no end-of-central-directory found")
            cd_size, cd_off = struct.unpack("<II", tail[j + 12:j + 20])
        cd = self._range("%d-%d" % (cd_off, cd_off + cd_size - 1))
        out, p = [], 0
        while p < len(cd) - 46 and cd[p:p + 4] == _CDFH:
            method = struct.unpack("<H", cd[p + 10:p + 12])[0]
            csize, usize = struct.unpack("<II", cd[p + 20:p + 28])
            nlen, elen, clen = struct.unpack("<HHH", cd[p + 28:p + 34])
            hoff = struct.unpack("<I", cd[p + 42:p + 46])[0]
            name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
            extra = cd[p + 46 + nlen:p + 46 + nlen + elen]
            # ZIP64 extra: 0xFFFFFFFF is a sentinel meaning "the real value is over here"
            if 0xFFFFFFFF in (csize, usize, hoff):
                q = 0
                while q + 4 <= len(extra):
                    hid, hsz = struct.unpack("<HH", extra[q:q + 4])
                    if hid == 0x0001:
                        vals, r = [], q + 4
                        while r + 8 <= q + 4 + hsz:
                            vals.append(struct.unpack("<Q", extra[r:r + 8])[0]); r += 8
                        k = 0
                        if usize == 0xFFFFFFFF and k < len(vals): usize = vals[k]; k += 1
                        if csize == 0xFFFFFFFF and k < len(vals): csize = vals[k]; k += 1
                        if hoff == 0xFFFFFFFF and k < len(vals): hoff = vals[k]; k += 1
                        break
                    q += 4 + hsz
            out.append(Member(name, method, csize, usize, hoff))
            p += 46 + nlen + elen + clen
        self._members = out
        return out

    def extract(self, member: Member, dest, progress_every: int = 512 << 20) -> bool:
        """Stream one member to `dest`. Returns False rather than raising on a transport error."""
        try:
            # the local header repeats the name/extra with its OWN lengths — read it to find
            # where the data actually starts
            lh = self._range("%d-%d" % (member.header_offset, member.header_offset + 29))
            if lh[:4] != b"PK\x03\x04":
                log.warning("remote zip: bad local header", name=member.name[:60]); return False
            nlen, elen = struct.unpack("<HH", lh[26:30])
            start = member.header_offset + 30 + nlen + elen
            end = start + member.comp_size - 1
            # archive.org answers a ranged GET with 500/200 often enough that a single attempt
            # loses a multi-GB transfer; retry the way _range does.
            r = None
            for a in range(5):
                r = self.s.get(self.url, headers={"Range": "bytes=%d-%d" % (start, end)},
                               stream=True, timeout=600)
                if r.status_code == 206:
                    break
                log.info("remote zip: retrying ranged extract", status=r.status_code,
                         attempt=a + 1)
                r.close()
                time.sleep(5 * (a + 1))
            if r is None or r.status_code != 206:
                log.warning("remote zip: extract not ranged",
                            status=(r.status_code if r else "none")); return False
            d = zlib.decompressobj(-zlib.MAX_WBITS) if member.compress_type == 8 else None
            got = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    if not chunk:
                        continue
                    got += len(chunk)
                    fh.write(d.decompress(chunk) if d else chunk)
                if d:
                    fh.write(d.flush())
            log.info("remote zip member extracted", name=member.name.split("/")[-1][:60],
                     bytes=got)
            return True
        except Exception as e:  # noqa
            log.warning("remote zip extract failed", name=member.name[:60], err=str(e)[:140])
            return False
