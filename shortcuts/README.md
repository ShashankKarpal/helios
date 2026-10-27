# shortcuts/

iPhone Shortcuts for Helios, one page each.

| File | What | State |
|---|---|---|
| `LOG-TO-HELIOS.md` | Log to Helios: say what happened, the Mac stores it as an event | in use |
| `BRIEF-CHECK.md`, `make_shortcuts.py`, `local.example.json` | The 09:05 brief check and its 09:10 guard alarm, and the builder for them | retired 2026-10-27, kept for history |

The brief check was retired because the Helios Bridge widget shows the brief's
state every morning. `GET /api/brief-check` stays on the Mac.

`local.json` and `build/` hold the Mac's address and are ignored by git.
