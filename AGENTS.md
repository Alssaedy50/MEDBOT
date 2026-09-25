# MEDBOT — Repository Notes for Agents

MEDBOT is a Telegram medical-education LMS (study library, deterministic
search, student contributions, admin panel, and two separate AI modes:
platform resource search and AI chat).

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
- Runs on Termux with a `venv`. `run_bot.sh` is the watchdog launcher; it
  resolves its own directory and, before starting, runs a best-effort
  `git pull --ff-only origin main` (only when the tracked tree is clean) so a
  reboot runs the latest merged code. `start_daemon.sh` does the same.
- The operator holds the real credentials/database on Termux; CI here has no
  keys and no database. Tests use temporary SQLite files.
- Config env vars: `BOT_TOKEN`, `ADMIN_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`,
  `OPENROUTER_API_KEY`, `MEDBOT_DB_PATH`, `MEDBOT_ARCHIVE_CHANNEL` (the
  archive channel: numeric id or `@username`; unset disables the archive).
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
  grounding, and the assistant entry points.
- `ai_router.py` — compatibility facade re-exporting `ai.py`. No logic here.
- `ai_discovery.py` — CLI health/verification tool over the shared layer.
- `archive.py` — the ONLY Emergency Resource Archive implementation
  (mirrors registered resources to a standalone Telegram channel;
  env-configured).

## AI architecture — TWO separate modes
The assistant is deliberately split into two INDEPENDENT workflows. They are
never merged again: platform navigation must not rely on a general model, and
conversational answers must not read the platform database.
- MODE 1 — 🔎 Platform resource search: `ai.generate_platform_search_result`.
  "Tell me what exists in MEDBOT and take me there."
- MODE 2 — 🤖 AI Chat: `ai.generate_ai_chat_result`.
  "Talk to me and explain things intelligently."
Both consume one daily quota unit per call and return
`{"text": str, "actions": [{"label", "callback"}, ...]}`.

### MODE 1 — Platform resource search (`generate_platform_search_result`)
- Purpose: discovery + speed + accuracy. The user should reach real registered
  resources in one tap, not walk the menus.
- It MAY read the complete current platform catalog (folders, sections,
  subjects, blocks, files/resources, titles, descriptions, paths) so it can
  understand natural-language Arabic/English resource queries. That is
  intentional.
- Fast path (no model): `classify_intent` + deterministic
  `_search_medbot` (search_engine), then `_genuine_registry_matches` keeps only
  hits that really correspond to what was named (so "First Year" is never
  answered with "Second Year"). Rendered by `build_platform_search_answer`.
- Overview ("what exists on MEDBOT?") is answered from `build_registry_overview`
  over registered `folders` only — no search, no model.
- Catalog fallback (model, only when the deterministic pass found nothing and a
  provider is reachable): the model receives the full catalog via
  `build_platform_catalog` + `PLATFORM_SEARCH_PROMPT` and may reason over it,
  but `_verify_catalog_answer` iterates the REAL catalog rows and keeps a row
  only when the model explicitly named it. The model can therefore only
  *select* an existing row — it can never create one. No provider -> the honest
  `PLATFORM_SEARCH_NO_MATCH`.
- ABSOLUTE RULE: no platform claim without a real registry entity, and no
  Telegram access button without a verified registry id (built by
  `build_result_actions` from real ids only, capped by `MAX_RESULT_ACTIONS`).
- It never calls PubMed; `PLATFORM_SEARCH_PROMPT` forbids the model from
  emitting callbacks/ids/links.

### MODE 2 — AI Chat (`generate_ai_chat_result`)
- Purpose: conversational answers. It does NOT navigate the platform and never
  reads the registry (no `_search_medbot`, no catalog), so it cannot expose or
  invent platform structure. Actions are always empty.
- Medical question (`_is_medical_question`: `INTENT_MEDICAL` or any recognized
  medical concept): `UNIFIED_ASSISTANT_PROMPT` requires a model academic answer
  in ENGLISH first, then a separate faithful Arabic summary section
  (`English (academic):` before `العربية — شرح مختصر:`); NCBI PubMed sources
  ground the answer via `ai._fetch_pubmed_sources`. Brief by default.
- General question: `GENERAL_ASSISTANT_PROMPT`, Arabic, concise, no PubMed.
- `_is_medical_question` uses `search_engine.implied_concepts` (shared with the
  search engine) plus `MEDICAL_CONCEPT_KEYS`/`_MEDICAL_KEYWORDS`, so a
  navigation-flavoured message that names a medical concept ("ويـن الميكرو؟")
  is answered as medical in chat.
- Trusted sources: `ai.build_sources_footer` renders a
  `🔬 مصادر موثوقة (NCBI PubMed)` footer with real links, skipped by
  `ai._ensure_sources_footer` when the model already cited PubMed/PMID. The
  no-provider fallback shows no source footer.

### Policy, compat, and latency
- Platform facts (anything equivalent to "MEDBOT contains X") in Mode 1 may only
  come from the registry. The hierarchy is never hardcoded: an admin addition
  appears automatically. Do not hardcode subjects.
- `generate_medbot_unified_result` / `generate_medbot_unified_response` are
  legacy thin dispatchers kept for CLI/auto-routing callers: they classify and
  delegate to the matching mode. New callers use the two mode functions.
- `generate_medical_ai_response` / `generate_medbot_assistant_response` remain
  for compatibility but are no longer wired to the student UI.
- Latency: classification is local and free; the overview path does one registry
  read and the deterministic search path one query. The catalog fallback runs
  only when deterministic search fails. AI chat's medical path fetches the
  candidate pool and PubMed concurrently via `asyncio.gather`; its general path
  loads only the candidate pool. Provider discovery and the probe batch are
  concurrent; generation is capped by `ai.MAX_OUTPUT_TOKENS`; `warm_ai_pool()`
  runs once at startup (best-effort) so the first student reply does not pay for
  discovery.
- Anti-repetition output guard: `ai._guard_answer` runs locally on every
  generated answer (inside `_provider_failover` and the legacy `/ask` path) and
  collapses adjacent repeated lines / paragraphs / table rows. It performs no
  network or DB calls and never caps answer length, so a long, varied answer is
  untouched. Only when `ai._has_repetition` still flags an obvious repetition is
  the answer regenerated once for that candidate (`REPETITION_RETRY_INSTRUCTION`)
  — never in a loop. Thresholds: `REPETITION_MIN_UNIT_CHARS`,
  `REPETITION_MIN_REPEATS`.

### Student UI (main.py)
- The assistant entry (`assistant` callback) opens a mode chooser
  (`main.ASSISTANT_MENU_TEXT` + `_assistant_menu_keyboard`): 🔎
  `main.MODE_PLATFORM` or 🤖 `main.MODE_CHAT`. Selecting one stores it in
  `assistant_mode` and shows that mode's screen (`PLATFORM_SEARCH_SCREEN_TEXT`
  / `AI_CHAT_SCREEN_TEXT` + `_mode_keyboard`).
- `ai_handler` dispatches on `assistant_mode`: `MODE_CHAT` ->
  `generate_ai_chat_result`; `PLATFORM_MODES` (`MODE_PLATFORM`, legacy
  `"unified"`) -> `generate_platform_search_result`. Outside both modes a typed
  message opens the chooser (no quota consumed). `/ask` selects `MODE_CHAT`.
- Legacy callbacks still resolve so old messages never dead-end:
  `assistant_search`/`assistant_start` -> `MODE_PLATFORM`, `assistant_medical`
  -> `MODE_CHAT`.
- `/search` (`run_search`) uses the platform-search workflow and registers its
  reply as content (`_register_content_message`) so a later navigation tap
  cannot overwrite it.
- `_feature_for_callback` maps `assistant`, both mode callbacks and the legacy
  ones to the `assistant` feature, so the visibility gate covers every entry.

## Daily allowance UX
- The remaining balance is never shown: it is gone from the home screen, the
  account screen, `/quota`, and the assistant reply footer.
- `DAILY_LIMIT` (`main.py`) is 25 requests/day; the limit number is never
  displayed anywhere.
- When the allowance is used up (last allowed request and every request after),
  the reply ends with `main._quota_reset_text()`: it says the stop is not an
  error and states the reset window until midnight server time (e.g. "خلال 8
  ساعة و31 دقيقة"). `database.check_and_increment_quota`/`get_remaining_quota`
  take `max_limit` from `DAILY_LIMIT`.


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
  `can_notifications`, `can_settings`, `can_topics`, `can_visibility`,
  `can_archive`, `can_news`).
- Migrations are additive: `_migrate_v5` adds `admins.role`/`admins.permissions`;
  `_migrate_v6` creates the isolated `audit_log` table; `_migrate_v7` backfills
  NULL/empty roles to `admin`; `_migrate_v8` adds the nullable
  `description`/`keywords` columns used by intent-aware search on `folders` and
  `content`; `_migrate_v11` heals a database left with more than one `owner` row
  (keeps the persisted owner, demotes the rest to `admin`); `_migrate_v12`
  creates the isolated `archive_sync` mirror table; `_migrate_v13` creates the
  News Core tables (`news`/`news_reads`/`news_subscriptions`); `_migrate_v14`
  creates the News private-delivery table (`news_deliveries`, keyed by
  `(news_id, user_id)`); `_migrate_v15` adds the partial unique index
  `idx_news_auto_resource_unique` on `news(resource_id) WHERE
  source='resource'` (collapsing any pre-existing auto duplicates first), so an
  auto resource news row is idempotent at the DB level; `_migrate_v17` folds
  the legacy `resource` news kind into `section` (see News Core below). Never
  rewrite v1..v4.
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
  `INSERT OR REPLACE`; it now refuses to touch the owner (a replace would reset
  the owner's role/perms), and `handle_add_admin_text` re-asserts the resolved
  non-owner as least-privilege `admin`.
- Adding a sub-admin by `@username` or Telegram ID resolves through
  `database.resolve_user_by_identifier()`, which reads the real `users`
  registry (the dedicated `username` column, case-insensitive) — NOT a demand
  that the person press `/start` again. It never creates a user row, and an
  unknown handle returns an explicit "no such account" message. Falls back to
  the `admins` table for an already-registered handle.
- Admin preview: `admin_management.show_admin_preview` (callback
  `amg_preview:<id>`) is a read-only admin-preview/impersonation aid gated by
  `can_admins`. It renders the target's stored role and effective permissions
  from the RBAC tables and audits the action (`admin_preview`); it never
  changes the acting admin's Telegram identity, never creates a fake session,
  and never mutates the target's role/permissions.
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
- New permission keys: `can_notifications`, `can_settings`, `can_topics`,
  `can_archive` (added to `PERMISSION_KEYS`/labels). The admin panel shows
  each surface only when the caller holds it; `show_admin` also appends a
  Runtime row.
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

## News Core (الأخبار) — Phase 1
- `news.py` is the ONLY news implementation: the student 📰 News Center and the
  admin publishing surface, built directly on migration v13's tables. Nothing
  existing is rewritten; the existing `notifications` broadcast is a separate
  subsystem and is untouched (it no longer has an Admin Panel entry — 📰 News
  replaced 🔔 Notifications — but its handlers/text flow stay registered so a
  legacy message never dead-ends).
- TWO first-class kinds share one chronological feed: `notify` (🚨 Important /
  Urgent) and `section` (📚 Section News). `database.NEWS_TYPES` is the source
  of truth. A resource, explanation post, announcement, etc. is **not** a news
  kind: it is an optional *linked reference* (`resource_id`) on a 📚 Section
  News item. The legacy `resource` kind is kept only so an old row/subscription
  is understood (`database.LEGACY_NEWS_TYPES`/`NEWS_TYPE_ALIASES`); every write
  funnels through `database.normalize_news_type`, and migration v17 rewrites
  historical rows/subscriptions to `section`. `NEWS_SOURCE_AUTO` stays the
  string `"resource"` as the auto-row marker on `news.source` (the v15 partial
  unique index depends on it), so auto-idempotency is unchanged.
- Real references only: a news row stores `section_folder_id`/
  `subject_folder_id`/`resource_id`, never a copied name or path.
  `create_news`/`update_news` validate every reference against the live
  `folders`/`content` rows and reject an unknown id, so the News Center can
  never be pointed at a non-existent entity. `get_news_detail` resolves the
  CURRENT name and breadcrumb from the registry (`build_breadcrumb_paths`), so
  a rename shows up automatically and a deleted resource/section degrades to
  no access button rather than a fabricated path.
- `database.create_news(..., status='draft')` by default: nothing reaches
  students before an explicit `publish_news`. Lifecycle helpers:
  `publish_news` (idempotent, keeps `published_at`), `archive_news`,
  `restore_news` (back to draft, never straight to students), `delete_news`.
  `list_news`/`count_news` order newest-first by
  `COALESCE(published_at, created_at)` with pagination (`NEWS_PAGE_SIZE`);
  archived rows never leak into a normal listing.
- Read tracking is standalone: `news_reads` is keyed by `(user_id, news_id)`,
  so a duplicate read is impossible. `mark_news_read` / `is_news_read` /
  `get_unread_news_count` / `get_read_news_ids` / `mark_all_news_read`.
  The home keyboard shows the unread count as a badge on the 📰 entry
  (`main._home_badges` → `home_keyboard(..., badges=...)`), never a zero badge.
- Student callbacks: `news` (feed), `news_open:<id>` (marks read + shows the
  detail and its real access button), `news_more:<page>`, `news_filter:<type>`,
  `news_readall`. A student may open **published** news only: `open_news`
  refuses `draft`/`archived` exactly like a missing row and creates NO
  `news_reads` record, so an unpublished id can never leak text or inflate the
  unread count. Admin callbacks: `admin_news` (draft+published working set),
  `news_admin_all`, `news_admin_archived` (reach archived rows),
  `news_new:<type>`, and per-row `news_admin_view:<id>` (manage actions),
  `news_admin_preview:<id>` (read-only student-style preview),
  `news_admin_pub|archive|restore|delete:<id>`. The admin list is fully
  clickable and each item shows state-appropriate actions (draft →
  Preview/Publish/Delete; published → View/Archive/Delete; archived →
  View/Restore/Delete). The admin surface is gated by `can_news`, never
  records a read for the admin, and every mutation is audited
  (`news_publish`, `news_archive`, `news_restore`, `news_delete`).
- `news` is a `database.FEATURES` key (hideable) and its public callbacks are
  feature-gated; news registers its own handler BEFORE the catch-all
  `callback_router`, so it owns its namespace and its own hidden check
  (admins bypass). The admin draft wizard consumes typed input through
  `news.handle_news_text` (called from `ai_handler`) and uses the single
  `workflow` name `news_draft` (registered in `workflow.WORKFLOWS`) so its
  two steps never cancel each other's keys.
- Future compatibility is designed in, not built: `news_subscriptions`
  (created empty, unused), `delivery_scope`, `source` and `payload` columns,
  and the `draft` lifecycle state reserve the shape for Phase 2 (publishing
  workflow + subscriptions + private delivery + resource-generated news) and
  Phase 3 (Scoped Admin / RBAC over a subject/folder) without a redesign.

## News Phase 2 (النشر + الاشتراكات + التوصيل) — built on the Phase 1 Core
- `_migrate_v14` adds `news_deliveries`, keyed by `(news_id, user_id)` with
  `status` (`pending`/`sent`/`failed`/`skipped`), `kind`, `channel`,
  `attempts`, `error`, `channel_message_id` and timestamps. The primary key is
  the idempotency guarantee: a republish, retry, restart or crash converges on
  one row and at most one Telegram message. Subscriptions need no new table —
  v13's `news_subscriptions(user_id, topic_kind, topic_value)` is used as-is
  (`type` = a news kind, `section` = a real folder id).
- `news_delivery.py` is the transport service (no handlers, no callbacks):
  `plan_delivery` resolves subscribers and reserves their rows, `deliver`
  batches + rate-limits (`DELIVERY_BATCH_SIZE`, `DELIVERY_BATCH_DELAY` — a real
  delay between batches, never `sleep(0)`) and isolates every per-recipient
  failure (one blocked chat never aborts the batch),
  `enqueue_publish_delivery` delivers inline for a small audience and hands a
  large one to a tracked background task (`drain_background` for
  tests/teardown), `retry_failed` resends only `pending`/`failed` (never a
  `sent` row). `build_delivery_text`/`build_delivery_markup` mirror the News
  Center detail and offer a direct button only for a reference that still
  exists. Telegram `RetryAfter` (429) is honoured, capped at
  `DELIVERY_RETRY_AFTER_CAP` and retried at most `DELIVERY_MAX_SEND_RETRIES`
  times per recipient before recording `failed`.
- Delivery status is `pending` → `sending` (a transient claim written BEFORE
  the Telegram call) → `sent`/`failed`; `skipped` is terminal too.
  `claim_news_delivery` takes the claim, `mark_news_delivery` resolves it
  (`sent` is terminal, increments `attempts`). DB guarantees no duplicate
  reservation and a terminal `sent`, but there is **no external exactly-once
  guarantee**: a crash between "Telegram accepted" and the DB write leaves a
  recoverable `sending` row and the item may be delivered again — documented
  at-least-once in the crash window, never claimed otherwise.
- Startup recovery: `news_delivery.start_recovery(bot)` is called from
  `main.post_init` AFTER `init_db`. It is a single background worker
  (idempotent — a second startup call never spawns a rival), never awaited, so
  polling starts immediately. `recover_pending_deliveries` resets stale
  `sending` claims (`reset_stale_news_deliveries`,
  `NEWS_DELIVERY_STALE_SECONDS`) and replays the oldest published items with
  `pending`/`failed` rows (`list_recoverable_news_ids`, bounded by
  `RECOVERY_MAX_NEWS`) through the same engine. `sent`/`skipped` are never
  reselected, so recovery cannot resend a completed delivery.
- Delivery never marks an item read: `news_reads` stays empty until the
  student opens it, so a private push and the unread badge stay consistent.
- Subscriptions control **private delivery only**: `show_news_feed` still lists
  every published item to everyone, and a non-subscriber simply is not pushed.
  `database.resolve_news_recipients(news_type, section_folder_id)` resolves the
  target set in one query per subscription shape (`notify`→notify subscribers,
  `section`→section-type + that exact folder), de-duplicated.
- `database.add_news_subscription` validates the topic (a real kind, or a real
  folder id) and is idempotent; `remove_news_subscription`,
  `get_news_subscriptions`, `get_news_subscriptions_map` (no N+1) and the
  delivery helpers `reserve_news_deliveries` /
  `get_pending_news_deliveries` / `mark_news_delivery` (sent is terminal) /
  `get_news_delivery_counts` / `get_news_deliveries` complete the layer.
- Admin publishing surface: `show_admin_news` is 📰 الأخبار, whose entry screen
  offers exactly three entries — ➕ نشر خبر (`news_new`), 📋 الأخبار المنشورة
  (`news_admin_published`) and 🗄 الأرشيف (`news_admin_archived`). `news_new`
  opens a type chooser (🚨 هام/عاجل / 📚 أخبار الأقسام) gated by scope; the
  draft wizard (`news_new:<type>`) is title → body → doctor → event (each
  optional via `/skip`), then a 📚 Section News item routes to the real-registry
  section picker (`pick_section`, callbacks `news_ref_*`) — ids are never typed.
  A linked resource is *optional*: `news_ref_resource`/`news_ref_set_resource`
  link one and `news_ref_unlink` clears it. `_reference_missing_for` blocks
  `_publish` only for a section news without its real section. On publish,
  `_publish` flips the row first and then calls
  `news_delivery.enqueue_publish_delivery` (best-effort; a delivery failure
  never rolls back the publish).
- Per-row admin callbacks add `news_admin_deliveries:<id>` (delivery log with
  counts + recent rows) and `news_admin_retry:<id>`; `_admin_item_menu` shapes
  its actions by state (draft → picker/optional-link/Preview/Publish, published
  → View/Archive/Delivery log, archived → View/Restore/Delivery log). Every
  mutation is audited (`news_create`, `news_reference`, `news_delivery_retry`
  added to `audit.ACTIONS`/`ACTION_LABELS`).
- Student subscriptions surface: the feed gains a `news_subs` entry;
  `show_subscriptions` (⚙️ اشتراكات الأخبار) toggles the two kinds
  (`news_sub:<type>`) and `show_section_subscriptions` (📚 إدارة الأقسام
  المتابَعة) browses the live folder tree (`news_subs_section:<folder_id>`) so a
  student subscribes only to real sections. Every toggle shows its state
  explicitly ("مشترك ✓" / "غير مشترك"). Dedicated i18n key `news_subs`.
- Student feed filters are explicit, self-describing labels — 📋 كل الأخبار,
  🚨 هام / عاجل, 📚 أخبار الأقسام — never icon-only/colour-only chips, and there
  is no "resource" filter.
- Resource-generated news: `news.publish_news_for_resource(bot, content_id)`
  creates AT MOST one 📚 Section News row per content id
  (`database.create_resource_news_for_content` / `get_resource_news_for_content`,
  `source='resource'`, `news_type='section'`), resolves the resource's real
  folder from the registry
  and publishes+delivers best-effort. Idempotency is DB-level (v15's partial
  unique index), not a check-then-insert race: a concurrent second call gets
  the winner's row back from `create_news` (which catches `IntegrityError` for
  an auto row only). A manual news row for the same resource is unaffected.
  `main._register_admin_upload` (admin upload) and `main.process_approval`
  (approved contribution) call it right after `database.add_content`, inside
  their own try/except, so it can never fail, delay or roll back the resource
  write.
- Phase 3 (Scoped Admin / RBAC over a subject/folder) is implemented: a scope
  layer over the same tables. `news.section_folder_id`/`subject_folder_id` (and
  the referenced resource's folder) carry the scope, and every admin mutation
  goes through the `can_news` capability + the scope layer + audit.

## Scoped RBAC (Phase 3)
- `authorization.py` is the ONLY place that combines a capability with a scope.
  Handlers call `authorization.can(...)`/`require(...)` (or the wrappers); they
  never re-implement the decision. Deny-by-default order: active admin -> known
  scoped op -> role clears the coarse key -> `admin_has_scopes` -> target in
  scope. Unknown permission, unknown/global target, or a target whose row was
  deleted -> DENY (fail-closed). It never raises.
- Scopes are **opt-in**: an admin with no `admin_scopes` rows keeps the
  pre-Phase-3 platform-wide reach, so installing the phase revokes nothing.
  `SCOPE_TYPES` = `folder` (subtree), `topic` (folders linked to a Search
  Topic), `resource` (one content row); `SCOPED_PERMISSIONS` maps each scoped op
  to exactly one coarse `PERMISSION_KEYS` entry via `SCOPED_PERMISSION_COARSE`.
- `admin_scopes` (v16) is keyed UNIQUE `(admin_id, scope_type, scope_id)`;
  `scope_id` is a polymorphic reference (no FK), always validated against the
  live registry by `add_admin_scope` and re-resolved at decision time, so a
  deleted target simply matches nothing. Never store a scope without the target
  row existing.
- Target testers: `folder_in_admin_scope` (ancestor walk), `resource_in_admin_scope`
  (direct resource scope, else its real folder), `is_topic_in_admin_scope`
  (exact topic), `news_scope_folder_ids`/`news_resource_id` (a news item is in
  scope when any real folder it references — or its resource — is),
  `contribution_folder_id`. A global operation (broadcast) has no target, so a
  scope-restricted admin is always refused; unscoped admins/owner are unchanged.
- List filtering: `scoped_folder_ids` (folders) and `list_news_ids_for_admin`
  (news) return the reachable set for a restricted admin, or None when
  platform-wide. Movement pickers must not offer the root or an out-of-scope
  branch to a restricted admin.
- Owner-only by design: `can_admins` and the whole `admin_management` scope
  surface. Owner protection (single owner) is unchanged; the owner is
  unrestricted and is refused a scope grant/revoke.
- Audit events: `scope_grant`, `scope_revoke`, `authz_denied`.

## Emergency Resource Archive (disaster recovery)
- `archive.py` is the ONLY archive implementation: it mirrors registered
  resources into a standalone Telegram channel that keeps them reachable when
  MEDBOT is down. MEDBOT's registry remains the source of truth; the channel is
  an access layer, never read back.
- The channel is configured ONLY through the environment
  (`MEDBOT_ARCHIVE_CHANNEL`, alias `ARCHIVE_CHANNEL_ID`), accepting a numeric id
  or `@username`. Nothing is hardcoded and no id is assumed; when unset the
  feature is inert (`archive.is_configured()` is False). The bot must be a
  channel administrator; the module posts with normal bot rights only.
- Flow: `MEDBOT resource created -> archive.publish_resource -> channel`. Hooks:
  `main._register_admin_upload` (admin upload) and `main.process_approval`
  (approved contribution) call `archive.publish_resource(bot, content_id,
  with_header=True)` AFTER `database.add_content`; the call is best-effort and
  never fails, delays or rolls back the MEDBOT write.
- `database.get_resource_snapshot`/`get_all_resource_snapshots` supply the
  resource plus its REAL registered path (the platform breadcrumb). The archive
  never synthesises a path or section; a post only ever names registered items.
- Idempotency: `archive_sync` (migration v12) is keyed by
  `content_fingerprint` = hash(title + file_type + file_id), so a restart, a
  retry or a resource re-added as a new row can never produce a second post.
  `database.mark_archive_published` transitions to `published` at most once and
  a per-fingerprint `asyncio.Lock` serialises concurrent publishes. Row fields:
  folder_id, content_ids, channel_id, channel_message_id, status, attempts,
  error, timestamps.
- Failure policy: a Telegram error records `failed` (+ error, attempts) and is
  retried later; it never propagates. Deleting/disabling a resource in MEDBOT
  never deletes its archived copy (the `archive_sync` row survives).
- Admin surface: `can_archive` (owner/admin presets yes, reviewer no) via
  `archive.py` (own handlers, callback prefix `archive_*`/`admin_archive`).
  Actions: resync existing resources (skips already-published, so no
  duplicates), retry failed rows, and a status view with per-status counts.
  Handlers register BEFORE the catch-all `callback_router`.
- Posts include the resource's title + path + type, so a student can find them
  by hand searching the channel (subject, `عملي`, lecture title, ...).

## Testing
- `python -m py_compile` all modules.
- `python -m unittest test_medbot_system test_medbot_router test_medbot_grounding test_medbot_phase2 test_messaging test_rbac_audit test_contribution_ux test_medbot_search_intent test_medbot_performance test_platform_update test_medbot_fixes test_visibility test_ai_policy test_ai_modes test_archive_sync test_news_core test_news_phase2 test_news_phase2_fixes test_news_phase3`
- `test_news_phase3.py` pins Scoped RBAC: migration v16 additive/idempotent,
  deny-by-default with no scope rows = platform-wide, fail-closed on a deleted
  target, folder/topic/resource/news/contribution scope resolution, the
  scope-restricted admin being refused a global broadcast, the owner-only scope
  surface, audit events, and that the existing permission/resource/broadcast
  systems are unaffected.
- `test_ai_policy.py` pins the AI behavior policy: intent classification,
  resource-hallucination refusal, and that only the medical path fetches
  PubMed. It never calls a real provider.
- `test_ai_modes.py` pins the two-mode separation: platform search resolves
  colloquial Arabic/English to real resources and returns only verified-id
  buttons (discarding an invented entity the model names), and AI chat keeps
  the bilingual medical contract, skips the registry for general questions,
  and never exposes platform structure. It replaces only provider functions
  (no business logic mocked). `RepetitionGuardTests` pins the local
  anti-repetition guard: repeated lines/paragraphs/table rows are collapsed,
  long varied answers pass untouched, the guard makes no DB/network call, and a
  persistent repetition triggers exactly one regeneration before falling back to
  the locally cleaned answer.
- `test_archive_sync.py` pins the Emergency Archive: auto-publish on
  creation, no duplicate after a restart/retry, publication failure never
  failing the resource, retry-once, resync of pre-existing resources, real
  path/order, no invented sections, environment-only channel config,
  deletion isolation, and `can_archive` gating. It stubs only the Telegram
  bot (the archive's external boundary); the database and all business
  logic are real.
- `test_db_patch.py` needs a real `medbot_v2.sqlite3`; it is skipped locally
  when absent.
- `test_rbac_audit.py` additionally pins `AddAdminLookupTests` (add by
  `@username` / ID resolves the existing registry user, no duplicate user row,
  clear message for an unknown handle, owner never touched) and
  `AdminPreviewTests` (preview is authorized, read-only, audited, and changes
  neither the caller's identity nor the target's role/permissions).
- `test_news_core.py` pins the News Core (Phase 1): migration v13 is additive
  and idempotent, migration v17 folds the legacy `resource` kind into `section`
  (rows and subscriptions, idempotently), the two news kinds and their real
  folder/resource references (an unknown id is rejected), the newest-first
  paginated feed, publish/archive/restore/delete lifecycle, independent read
  tracking with no duplicate records, per-user unread counts, the home unread
  badge, the `can_news`-gated and audited admin surface, the merged News/Admin
  UX (`NewsUXTests`: explicit feed labels, the two-kind publish form, the
  subscribe-state wording, the three admin entries, and a linked resource never
  being a separate kind), and the future-compatibility seams
  (`news_subscriptions`, `delivery_scope`, `source`, `payload`). It also
  asserts the existing notification broadcast and resource navigation still
  work.
- `test_news_phase2.py` pins News Phase 2: migration v14 is additive/idempotent
  and its `(news_id, user_id)` key makes a duplicate delivery impossible;
  subscriptions validate their topic and resolve recipients by kind/section
  without duplicates; the delivery engine marks sent, isolates a per-recipient
  failure, retries only pending/failed, runs a large audience in the background
  and never marks an item read; a Section News item cannot publish without a
  real section reference (a linked resource is optional and never blocks it)
  and the pickers reject an unknown id; the News Center still lists everything
  to non-subscribers; resource-generated Section News is created once and never
  raises; the admin surfaces are `can_news`-gated and audited. Only the
  Telegram bot boundary is stubbed; the database and all business logic are
  real.
- `test_news_phase2_fixes.py` pins the Phase 2 review fixes: delivery
  crash/restart recovery (a `pending` row is finished, a `failed` row is
  retried, a stale `sending` claim is reset and redelivered, `sent`/`skipped`
  are never resent, recovery ignores drafts, one failure does not stop the
  rest, the worker is non-blocking and idempotent, repeat passes do not
  duplicate); Telegram-aware rate limiting (real waits between batches, none
  after the last, `RetryAfter` honoured and capped, a persistent 429 records
  `failed`); DB-level resource-news idempotency (first call makes one row,
  repeat returns it, concurrent calls converge, a direct auto insert returns
  the winner, manual news for the same resource is still allowed, v15 is
  idempotent and collapses pre-existing duplicates without touching unrelated
  news); and the documented at-least-once crash-window semantics. Only the
  Telegram bot boundary is stubbed; the database and all business logic are
  real.
- Tests must exercise real code paths against temporary SQLite; no mocks.
- Root folders are stored with `parent_id IS NULL` (not `0`).

## Backups
- Timestamped source backups go under `backups/` (gitignored).
