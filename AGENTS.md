# MEDBOT — Repository Notes for Agents

MEDBOT is a Telegram medical-education LMS (study library, deterministic
search, student contributions, admin panel, MEDBOT-grounded AI assistant).

## Critical operating rules
- NEVER delete or recreate `medbot_v2.sqlite3`. It is the source of truth and
  is not committed (see `.gitignore`). Back it up before risky changes.
- NEVER inspect `~/.local/share/opencode/auth.json`.
- NEVER print, log, copy, or persist API keys, tokens, or `.env` contents.
- NEVER invent files, folders, paths, resources, doctors, or credentials.
- Do not send the full MEDBOT tree to an AI model; SQLite search is the
  source of truth.
- Do not kill unrelated processes. Stop only MEDBOT before schema changes.

## Environment
- Runs on Termux with a `venv`. `run_bot.sh` is the watchdog launcher.
- The operator holds the real credentials/database on Termux; CI here has no
  keys and no database. Tests use temporary SQLite files.
- Config env vars: `BOT_TOKEN`, `ADMIN_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`,
  `OPENROUTER_API_KEY`.

## Architecture (single source of truth)
- `main.py` — Telegram handlers, admin authorization, media dispatch.
- `database.py` — schema + idempotent migrations (`_migrate_v1`, `_migrate_v2`).
- `search_engine.py` — deterministic Arabic/English search (`search_library`,
  `search_library_summary`).
- `ai.py` — the ONLY AI implementation: discovery, adapters, failover,
  grounding, and both assistant entry points.
- `ai_router.py` — compatibility facade re-exporting `ai.py`. No logic here.
- `ai_discovery.py` — CLI health/verification tool over the shared layer.

## AI entry points
- `generate_medical_ai_response(prompt, user_id)` — general medical AI.
- `generate_medbot_assistant_response(prompt, user_id)` — MEDBOT-grounded
  resource assistant. Search first; if nothing is found it returns exactly
  `الموارد المطلوبة غير مسجلة حالياً في MEDBOT.` without calling a model.

## Admin security
- `ADMIN_ID` is the only admin source. `configured_admin_id()` returns 0 for
  unset/blank/`0`/non-numeric. `database.ensure_configured_admin()` is a no-op
  for IDs `<= 0`; the first user is never auto-promoted. `/whoami` grants
  nothing and only reports the caller's ID and admin status.

## Testing
- `python -m py_compile` all modules.
- `python -m unittest test_medbot_system test_medbot_router test_medbot_grounding`
- `test_db_patch.py` needs a real `medbot_v2.sqlite3`; it is skipped locally
  when absent.
- Tests must exercise real code paths against temporary SQLite; no mocks.

## Backups
- Timestamped source backups go under `backups/` (gitignored).
