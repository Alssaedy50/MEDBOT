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

## RBAC (owner + sub-admin) and Audit Log
- Roles live in `database.ROLES` (`owner`, `admin`, `reviewer`, `none`);
  active admin roles are `database.ADMIN_ROLES` (excludes `none`).
  Capabilities live in `database.PERMISSION_KEYS` (`can_folders`, `can_content`,
  `can_contributions`, `can_messages`, `can_ai`, `can_admins`).
- Migrations are additive: `_migrate_v5` adds `admins.role`/`admins.permissions`;
  `_migrate_v6` creates the isolated `audit_log` table; `_migrate_v7` backfills
  NULL/empty roles to `admin`; `_migrate_v8` adds the nullable
  `description`/`keywords` columns used by intent-aware search on `folders` and
  `content`. Never rewrite v1..v4.
- Permission storage: an EMPTY `admins.permissions` column means "legacy row,
  full access". An explicitly revoked admin is stored as the `PERMISSIONS_NONE`
  (`none`) sentinel. Never persist an all-False map as an empty string.
- `user_has_permission()` is the only capability check: owner always passes,
  non-admin always fails, unknown key fails, legacy/empty grants everything.
- Owner protection: the single `owner` must never be demoted or revoked while
  it is the only owner. `set_admin_role`/`remove_sub_admin` enforce this and
  `admin_management.change_role` reports it. A new owner must be minted first.
- Removing an admin sets `role='none'` (row + username kept) instead of
  deleting the row; `is_user_admin()` rejects `none`, and re-adding restores it.
- `audit.log_action()` is best-effort and must never raise into the audited
  operation. The audit table is independent of messages/contributions/content.
- Isolated modules: `audit.py` (viewer) and `admin_management.py` (role/permission
  UI). Register their handlers BEFORE the catch-all `callback_router`, and route
  their callbacks through their own handlers in tests.
- `admin_management` uses `database.add_sub_admin_by_any`, which does
  `INSERT OR REPLACE`; never let it touch the owner (it would reset role/perms).
- UI visibility rule: `main.home_keyboard()` is the public student keyboard and
  never contains the admin entry. Use `await main.home_for(update)` everywhere a
  home keyboard is attached; it appends the Admin Panel only for admins. The
  panel itself lists each permitted surface exactly once.
- AI Registry viewer (`show_ai_registry`) is gated by `can_ai` and renders a
  bounded, provider-grouped summary (`_registry_summary`) so it can never exceed
  Telegram's 4096-char limit, however many models are discovered.
- `ai_registry` positional columns matter: `last_test` is index 11;
  `_migrate_v2` appends `error_category`/`timeout_behavior`/`rate_limit_behavior`
  at 13/14/15. Never read `last_test` as `row[13]`.
- Contribution submission is a drill-down wizard, never a flat list:
  `contrib_browse:<id>` descends, `contrib_folder:<id>` starts the upload.
  `database.folder_has_contribution_target()` hides branches with no reachable
  target, and `_contribution_browse_buttons` disambiguates same-named siblings
  with `(#id)`. The upload prompt always names the full breadcrumb so the
  student can confirm the exact destination.
- Any inline edit must go through `edit_safe`, which clamps to
  `TELEGRAM_TEXT_LIMIT` on a line boundary. An oversized `edit_message_text`
  raises and would otherwise leave the caller on a blank, stale screen.

## Testing
- `python -m py_compile` all modules.
- `python -m unittest test_medbot_system test_medbot_router test_medbot_grounding test_medbot_phase2 test_messaging test_rbac_audit test_contribution_ux test_medbot_search_intent test_medbot_performance`
- `test_db_patch.py` needs a real `medbot_v2.sqlite3`; it is skipped locally
  when absent.
- Tests must exercise real code paths against temporary SQLite; no mocks.
- Root folders are stored with `parent_id IS NULL` (not `0`).

## Backups
- Timestamped source backups go under `backups/` (gitignored).
