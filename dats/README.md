# Vendored DATs

Logiqx-format DAT XML used for **identification/naming only** (not membership — RA is the gate).

- **No-Intro** — standard cartridge dumps (region/revision/canonical name).
- **Lost Level Archive** — the RA-only long tail (hacks/homebrew/translations the standard DATs omit).
  Keyed by RA console id: filename prefix (`028 - … Virtual Boy`) and `<header><id>` == `ra_console_id`.

One parser handles all four families (No-Intro / Redump / MAME / Lost Level Archive).
The actual `.dat`/`.xml` files are gitignored — vendor them here per system (see the ingest config).
