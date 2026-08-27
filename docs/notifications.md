# Discord notifications

RomFleet posts to Discord webhooks. Configuration lives in the **database**
(`Settings → Notifications`, or `PATCH /api/settings`), not in `.env`, so it changes
without a restart.

## Model

Two pieces: named **webhooks**, and a routing rule per **event kind**.

```jsonc
{
  "webhooks": {
    "default": { "url": "https://discord.com/api/webhooks/…", "label": "Main" },
    "quiet":   { "url": "https://discord.com/api/webhooks/…", "label": "Low-noise" }
  },
  "events": {
    "new_set":   { "enabled": true,  "webhook": "default" },
    "revision":  { "enabled": true,  "webhook": "default" },
    "subset":    { "enabled": true,  "webhook": "quiet"   },
    "untracked": { "enabled": false, "webhook": "quiet"   },
    "delisted":  { "enabled": true,  "webhook": "default" },
    "roundup":   { "enabled": true,  "webhook": "quiet"   }
  }
}
```

## Event kinds

| Kind | What it posts |
|---|---|
| `new_set` | 🆕 a new tracked set appeared — **and** the ✅/❌ sourcing outcome |
| `revision` | 🔄 an existing set was revised — **and** the re-source outcome |
| `subset` | ℹ️ a `[Subset]`/`[Bonus]` set; nothing to source |
| `untracked` | ℹ️ a new set on a system RomFleet does not track |
| `delisted` | ⚠️ RA removed or demoted a set |
| `roundup` | 🏆 the weekly coverage summary |

## Two rules worth knowing

**`new_set` and `revision` each cover a pair of posts.** The outcome is a *PATCH to the
original message*, not a new one. So the pair cannot be split across webhooks or
half-disabled — a different webhook cannot edit another's message, and disabling only the
result would strand a "Sourcing…" card that never resolves. They are deliberately one
switch each.

**A dangling webhook reference falls back to `default` rather than dropping the event.**
Same for an event kind the code emits but the config has never heard of. A stray post in
the wrong channel is easy to notice; a silently missing alert is not.

## Behaviour with no configuration

An install that has only ever set a single webhook keeps working unchanged: that URL
becomes `default` and every event is enabled and routed to it. There is no migration step.

Set every `enabled` to `false`, or leave the URL blank, and RomFleet posts nothing —
`send_test` still uses the default webhook regardless, so "does this webhook work?"
stays answerable when everything else is off.

## Testing

`python3 tests/test_discord_routing.py` — no DB, no network.
