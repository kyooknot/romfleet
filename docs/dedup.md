# Dedup — what each mode actually does, and where it bites

    GET  /api/dedup?folder=<sys>&mode=hash|slot|core     dry run
    POST /api/dedup?folder=<sys>&mode=...&confirm=true   apply (deletes files)

**Always pass `?folder=`.** Unscoped, `mode=slot` currently proposes 129 removals across the
fleet, most of which are not what an operator means by "remove the duplicate" — see below.

| mode | groups on | catches |
|---|---|---|
| `hash` | identical `computed_hash` under one RA game | the same bytes stored under two names |
| `slot` | 1G1R version slot (region/lang/rev/translation/dump tag stripped) | two versions of one game |
| `core` | cross-naming core title | the same hack under a TOSEC and an RA name |

## The library has two roots, and one of them is read-only

    /mnt/romfleet/roms   live tree, read-write   ~11,900 rows
    /mnt/src/roms        source tree, READ-ONLY     ~206 rows

The same file can be catalogued in both. Those rows are byte-identical with identical
filenames, so every mode groups them — and the keeper contest then ties on *every* term,
leaving the winner to dict order. It can just as easily choose to drop the **live** copy and
keep the read-only one, which removes the game from the ES-DE tree.

When the drop lands on `/mnt/src`, `unlink()` raises `PermissionError`, the exception handler
catches it, and the row survives — the operation reports errors and changes nothing. That is
the safe failure, but it is still five wasted entries on `psp` alone.

**Before applying, check which root each drop is on.** A plan whose drops are all under
`/mnt/src` has nothing useful to do.

## A row deletion is not a file deletion

Both apply paths unlink only when `fp.is_file()`, then drop the row regardless. Until
`files_deleted` / `missing_on_disk` were added to the response, a row whose path no longer
resolved counted as a successful removal that freed nothing.

This happened on the psx alt-dump pass: the API reported `removed=7, errors=0` while only
**five** files were unlinked. Two rows pointed into the source tree at paths that no longer
existed, and the real files sat untracked under the live root and survived. `freed_bytes` was
the only signal — it was the sum of five files, not seven.

**Check `files_deleted` against `removed`, and confirm on disk.** A count coming back from the
API is not evidence that a file is gone.

## Same-hash does not mean safe to collapse

`mode=hash` on `pcfx` wants to drop `Blue Chicago Blues (JP) - CD2.chd` because it hashes
identically to `CD1.chd`. Two discs of one game cannot legitimately be the same bytes: this is
a bad rip where CD2 is a second copy of CD1, and the real CD2 is missing. Deleting CD2 would
tidy away the evidence of an incomplete game rather than fix anything.

Treat an identical hash across two *disc numbers* as a dump fault to investigate, never as a
duplicate to remove.

## Keeper choice differs between modes

For a byte-identical pair the two modes can pick opposite winners. On `ps2`,
`7 Wonders of the Ancient World.chd` and `7 Wonders of the Ancient World (USA).chd` are the
same bytes: `mode=slot` keeps the `(USA)` name (better 1G1R score), `mode=hash` keeps the
untagged one (it matches RA's canonical hash name). Both are defensible; pick the mode whose
naming you want to survive, rather than running both.
