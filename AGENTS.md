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
  `OPENROUTER_API_KEY`, `MEDBOT_DB_PATH`.
- SQLite path is resolved by `database.resolve_db_path()` / `ensure_db_dir()`
  (the only place that decides the file) with precedence: `DB_PATH` >
  non-default `DB_NAME` > `MEDBOT_DB_PATH` env > default relative
  `medbot_v2.sqlite3`. `search_engine` defers to the same resolver, so both
  modules always open the identical file. Deployka sets
  `MEDBOT_DB_PATH=/data/medbot_v2.sqlite3`; the parent dir is created on open.
  Never add a second, independent DB-opening path.

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
  `can_contributions`, `can_messages`, `can_ai`, `can_admins`,
  `can_notifications`, `can_settings`, `can_topics`, `can_visibility`).
- Migrations are additive: `_migrate_v5` adds `admins.role`/`admins.permissions`;
  `_migrate_v6` creates the isolated `audit_log` table; `_migrate_v7` backfills
  NULL/empty roles to `admin`; `_migrate_v8` adds the nullable
  `description`/`keywords` columns used by intent-aware search on `folders` and
  `content`; `_migrate_v11` heals a database left with more than one `owner` row
  (keeps the persisted owner, demotes the rest to `admin`). Never rewrite v1..v4.
- Role scope is explicit: `ROLE_PERMISSION_PRESETS` maps each role to a baseline
  capability set (owner = all, admin = all except `can_admins`, reviewer =
  contributions + messages, none = nothing) and `ROLE_DESCRIPTIONS` is the
  user-visible scope text. `database.apply_role_preset()` persists a role AND its
  preset together; the admin UI uses it, so "reviewer"/"admin" always mean the
  same thing. `can_admins` is owner-only by design.
- Exactly one owner: `ROLE_ASSIGNABLE` excludes `owner`, so the role screen can
  never mint a second owner (`change_role` refuses `owner` with a pointer to
  transfer). Ownership moves only through `transfer_ownership`, which promotes
  the target to owner and demotes the previous owner to `admin` (with the admin
  preset) atomically. `set_admin_role(_, 'owner')` exists but is not on the UI
  path; `ensure_configured_admin`/`db_demote_stale_owners` also demote stale
  owners and strip owner-only perms from them.
- Permission storage: an EMPTY `admins.permissions` column means "legacy row,
  full access". An explicitly revoked admin is stored as the `PERMISSIONS_NONE`
  (`none`) sentinel. Never persist an all-False map as an empty string.
- `user_has_permission()` is the only capability check: owner always passes,
  non-admin always fails, unknown key fails, legacy/empty grants everything.
- Owner protection: the single `owner` must never be demoted or revoked while
  it is the only owner. `set_admin_role`/`remove_sub_admin` enforce this and
  `admin_management.change_role` reports it.
- Removing an admin sets `role='none'` (row + username kept) instead of
  deleting the row; `is_user_admin()` rejects `none`, and re-adding restores it.
- `audit.log_action()` is best-effort and must never raise into the audited
  operation. The audit table is independent of messages/contributions/content.
- Isolated modules: `audit.py` (viewer), `admin_management.py` (role/permission
  UI) and `visibility.py` (feature show/hide). Register their handlers BEFORE the
  catch-all `callback_router`, and route their callbacks through their own
  handlers in tests.
- `admin_management` uses `database.add_sub_admin_by_any`, which does
  `INSERT OR REPLACE`; never let it touch the owner (it would reset role/perms).
- UI visibility rule: `main.home_keyboard(lang, hidden)` is the public student
  keyboard and never contains the admin entry. Use `await main.home_for(update)`
  everywhere a home keyboard is attached; it appends the Admin Panel only for
  admins. The panel itself lists each permitted surface exactly once.
- Feature visibility: `database.FEATURES` maps one-to-one to the home-page
  entries; `database.get_hidden_features()`/`set_hidden_features()` persist the
  hidden set in the `settings` table (`SETTING_HIDDEN_FEATURES`, no schema
  change). `main._home_rows()` is the declarative layout, `home_keyboard()`
  drops hidden entries, and `main._feature_for_callback()` +
  `_feature_blocked_for()` block a hidden feature's callbacks for non-admins.
  Admins always bypass the gate (so they can restore a hidden feature), and the
  owner always keeps the Admin Panel entry even when `admin_panel` is hidden.
  `messaging` and the `assistant`/`contributions` text+media paths do the same
  check because they register handlers ahead of `callback_router`. Gated by
  `can_visibility` via `visibility.py`; "إظهار الكل" restores everything.
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

## Platform update subsystems (v9/v10)
- Migrations are additive: `_migrate_v9` creates `topics`/`topic_folders`;
  `_migrate_v10` creates `notifications` and adds `users.language`. Never
  rewrite v1..v8.
- `i18n.py` is the only localization table. Callers use `i18n.t(key, lang)`;
  never branch on the language in a handler. Adding a language = extend
  `_TRANSLATIONS` + `database.SUPPORTED_LANGUAGES`.
- `platform_settings.py` edits persisted user-facing strings via
  `database.get_platform_setting`/`set_platform_setting`. Defaults live in
  `PLATFORM_SETTING_DEFAULTS`; unset keys fall back there, never to a crash.
  `messaging._contact_label()` reads the configurable contact label.
- `topics.py` is the Search Topics surface: high-level academic areas linked to
  folders (`database.link_topic_folder`). Deleting a topic cascades only its
  links — folders/resources are untouched.
- `notifications.py` broadcasts to `database.get_all_user_ids()`; per-recipient
  failures are non-fatal and every send is recorded via `record_notification`.
- New permission keys: `can_notifications`, `can_settings`, `can_topics`
  (added to `PERMISSION_KEYS`/labels). The admin panel shows each surface only
  when the caller holds it; `show_admin` also appends a Runtime row.
- Ownership transfer: `database.transfer_ownership(current, new)` is atomic and
  guarantees exactly one owner. It persists `SETTING_OWNER_ID`.
  `ensure_configured_admin` respects a transferred owner on a same-ADMIN_ID
  restart (tracked via `SETTING_CONFIGURED_ADMIN`) but re-asserts the owner
  when ADMIN_ID genuinely changes.
- Message persistence: `send_safe_message`, search results and resource
  messages register their ids via `main._register_content_message`.
  `edit_safe` sends a NEW message instead of editing when the target is
  registered content, so a navigation tap can never erase a delivered answer.
  Temporary menus are the only messages edited in place (`CONTENT_MENU_KEY`).
- `workflow.py` is the single-owner text-flow registry. Every flow that awaits
  typed input calls `workflow.begin(context, name)` on entry and its text
  consumer guards with `workflow.owns(context, name)`. Starting a flow cancels
  the other flows' keys and claims `workflow.ACTIVE_KEY`, so two overlapping
  prompts can never both consume the same message. When no flow is marked
  active (direct/legacy calls) `owns` returns True, preserving the old
  contract. `workflow.clear_all` runs on the `home` reset.
- Contribution preview: `main.preview_contribution` (route `preview:<id>`) is
  read-only — it sends the exact submitted file to the reviewing admin via
  `_send_registered_media` and never mutates the contribution row. Gated by
  `can_contributions`; the received media message is registered as content.
- Role-RBAC notification routing: `database.get_admins_with_permission(perm)`
  resolves admins the same way `user_has_permission` does (owner always,
  legacy empty-perms full, `none` never) and is what admin notifications use,
  so a supervisor without the relevant capability is not pinged.

## Testing
- `python -m py_compile` all modules.
- `python -m unittest test_medbot_system test_medbot_router test_medbot_grounding test_medbot_phase2 test_messaging test_rbac_audit test_contribution_ux test_medbot_search_intent test_medbot_performance test_platform_update test_medbot_fixes test_visibility`
- `test_db_patch.py` needs a real `medbot_v2.sqlite3`; it is skipped locally
  when absent.
- Tests must exercise real code paths against temporary SQLite; no mocks.
- Root folders are stored with `parent_id IS NULL` (not `0`).

## Backups
- Timestamped source backups go under `backups/` (gitignored).
