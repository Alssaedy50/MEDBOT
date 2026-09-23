# MEDBOT — Phase 1 Development Audit (Baseline)

Date: 2026-09-23
Branch: `fix/medbot-grounded-ai-and-admin-bootstrap`
Base commit: `9fed7c7` (origin/main, grafted/shallow clone)
HEAD at audit: `941f7c9`
Scope: read-only audit plus test baseline. **No functional changes** were made
in this phase; the only new file is this report.

---

## 1. CURRENT ARCHITECTURE

### Runtime shape
Single-process Telegram long-polling bot (`python-telegram-bot` 22.8) started by
`main.py`. `run_bot.sh` is a restart-on-exit watchdog loop; several extra
`run_*.sh` supervisor/watchdog scripts exist (see Duplication below).

### Modules (line counts at audit)
| File | Lines | Role |
|---|---|---|
| `main.py` | 3971 | All Telegram handlers, callbacks, admin UI, routing |
| `database.py` | 1309 | Schema, migrations, all persistence |
| `ai.py` | 1365 | Single AI implementation: discovery, adapters, failover, grounding |
| `ai_router.py` | 58 | Compatibility facade re-exporting `ai.py` |
| `ai_discovery.py` | 273 | CLI provider health/verification tool |
| `search_engine.py` | 287 | Deterministic Arabic/English search |
| `medical_sources.py` | 189 | PubMed lookup + source context |
| `mcq_quiz.py` | 169 | MCQ handling |
| `ai_architect.py` | 91 | Auxiliary AI helper |

### Storage
SQLite file `medbot_v2.sqlite3` (module constant `DB_NAME`). Connections are
opened per call via `get_db()` with `PRAGMA foreign_keys = ON`. Not in repo
(`.gitignore`); the live copy exists only on the operator's Termux host.

### Configuration / environment
| Variable | Consumer | Notes |
|---|---|---|
| `BOT_TOKEN` | `main.py` | required; bot exits with a message if absent |
| `ADMIN_ID` | `main.py` → `configured_admin_id()` | owner bootstrap; `0` means "not configured" |
| `GEMINI_API_KEY` | `ai.py` | provider |
| `GROQ_API_KEY` | `ai.py` | provider |
| `OPENROUTER_API_KEY` | `ai.py` | provider |

`.env` is gitignored. `.gitignore` additionally blocks `*.sqlite3`,
`backups/`, `venv/`, `__pycache__/`, and many backup patterns.

### Handler registration order (`main.py`)
1. Commands: `/start`, `/quota`, `/whoami`, `/search`, `/cancel`, `/ask`
2. `CallbackQueryHandler(callback_router)` — one large callback router
3. `MessageHandler` for media (`DOCUMENT|PHOTO|AUDIO|VIDEO|VOICE`) → `media_router`
4. `MessageHandler` for text (`TEXT & ~COMMAND`) → `ai_handler`

`media_router` order: admin upload state → contribution state → friendly
fallback. `ai_handler` order: pending-title input → admin-upload text guard →
folder rename → folder create → resource search mode → `/ask` → assistant mode
resource → assistant gateway → general AI.

---

## 2. CONTRIBUTIONS

### What exists (reusable, working)
- **Folder opt-in**: `folders.accepts_contributions` (default 0). Only opted-in
  folders appear in the contribution picker (`contribution_folders()`).
- **Creation**: `contribution_media_handler` → `database.add_contribution()`
  inserts into `contributions` with `status='pending'`. Supports document,
  audio, video, photo. Confirms to the student with the contribution id.
- **Storage**: `contributions` table:
  `id, user_id, user_name, folder_id, title, file_id, file_type, status,
  created_at` + FK to `folders` (`ON DELETE CASCADE`).
- **Status workflow**: a strict two-state machine — `pending` → `approved` |
  `rejected`. Both transitions run inside `BEGIN IMMEDIATE` and re-check
  `status='pending'` in the `UPDATE ... WHERE status='pending'`, so double
  approval/rejection is impossible. Re-running returns `None`.
- **Publish mechanism**: approval is the publish step. `approve_contribution()`
  inserts a row into `content` with `source_type='contribution'`,
  `source_contribution_id`, `created_by`, then flips status to `approved`,
  atomically. Rejection never writes `content`.
- **Admin reachability**: `/start` → Admin panel → `admin_pending` →
  `get_pending_contributions_list()` → `review:<id>` → Approve/Reject.
- **Review UI**: `review_contribution()` shows contribution details with
  Approve/Reject buttons; `process_approval()` executes and re-authorizes.
- **Notifications**: the **contributor** is notified on approve/reject via
  `query.get_bot().send_message(...)` inside a `try/except`.
- **Count surfaces**: `get_pending_contributions_count()` shown on the admin
  dashboard and the Runtime screen.

### What is missing
- **Admin notification on new submission**: no admins are pinged when a
  contribution arrives; it is only discoverable by opening the panel.
- **Review metadata**: no `reviewed_by`, `reviewed_at`, or reviewer note column.
  The reviewer identity is used for authorization but never recorded.
- **Rejection reason**: reject is silent — the student is told the contribution
  was rejected with no rationale.
- **No unpublish / rollback**: once approved and copied to `content`, there is no
  contribution-side path to retract it (only manual resource deletion).
- **No resubmission/edit flow**: a rejected contribution is terminal.
- **Limited validation**: no file-size or duplicate-title check at submission.

### Duplicated / dead code found here
- `database.get_pending_contributions()` (older shape) is **superseded** by
  `get_pending_contributions_list()`; only the latter is used by `main.py`.
- `database.get_all_contributions()` is **not referenced anywhere**.

---

## 3. ADMIN

### What exists
- **Identity model**: flat `admins(telegram_id, username, added_at)` table.
  `is_user_admin()` is the single authorization primitive.
- **Owner bootstrap**: `configured_admin_id()` parses `ADMIN_ID` and returns `0`
  for unset/blank/`0`/non-numeric. `post_init` calls
  `ensure_configured_admin()`, which is a deliberate no-op for ids `<= 0`, so a
  missing `ADMIN_ID` never promotes the first (or any) user.
- **Dashboard** (`show_admin`): pending count + entry buttons to folder
  management, pending review, AI registry, Runtime.
- **Resource management**: create/rename/retype/move/delete folders;
  rename/retype/move/delete resources; admin direct upload with optional custom
  title; guarded by `is_user_admin` both at the callback and inside each
  delegated function.
- **Contribution management**: pending list, detail view, approve/reject.
- **Owner tools**: `ensure_configured_admin`, `add_sub_admin`,
  `remove_sub_admin`, `add_sub_admin_by_any`, `get_all_admins`, `/whoami`.
- **AI/runtime tools**: `admin_ai` → `show_ai_registry`; `admin_runtime` →
  `show_runtime` (registered users count, pending count).

### What is missing
- **No RBAC / roles**: `admins` is a flat allowlist. There is no owner vs
  sub-admin vs reviewer distinction, and no per-capability permission model.
- **No admin-management UI**: the sub-admin functions exist and are unused — no
  button or command adds or removes an admin from inside the bot.
- **No audit log**: admin actions are not persisted (who approved, who deleted).
- **Unused observability**: `ai_usage_stats` and `ai_usage_top_students` exist
  but are not surfaced anywhere.
- **Unused hierarchy helper**: `get_folder_tree` is defined but never called.

### Authorization coverage (verified, not assumed)
Every admin-named callback and its delegated handler were checked. The large
router branches mostly delegate; each delegated function (`show_admin*`,
`start_admin_*`, `admin_folder_*`, `admin_file_*`, `review_contribution`,
`process_approval`, `show_pending`, `show_ai_registry`, `show_runtime`) performs
its own `is_user_admin` check. The admin-upload media path re-checks admin
before accepting a file. **No unauthenticated admin mutation path was found.**

---

## 4. DATABASE

### Tables present
| Table | Purpose | Notes |
|---|---|---|
| `users` | registered students | `joined_date` default now |
| `folders` | resource tree | self-FK `parent_id` `ON DELETE CASCADE`, `node_type`, `accepts_contributions` |
| `content` | published resources | FK `folder_id` cascade; `source_type`, `source_contribution_id`, `created_by` |
| `contributions` | student submissions | FK `folder_id` cascade; `status` |
| `admins` | admin allowlist | flat, no roles |
| `about_us` | single-row about text | `id CHECK (id=1)` |
| `settings` | key/value store | **defined but unused by app code** |
| `daily_ai_usage` | per-user/day AI quota | PK `(user_id, usage_date)` |
| `ai_registry` | provider/model catalog | unique `(provider, model, endpoint)` |
| `ai_model_usage` | per-call AI telemetry | FK `ai_registry` cascade, 3 indexes |

### Migrations
`init_db()` is idempotent: `CREATE TABLE IF NOT EXISTS` for everything, then
`_migrate_v1` and `_migrate_v2`, then index creation.
- `_migrate_v1`: adds `folders.accepts_contributions`, `content.source_type`,
  `content.source_contribution_id`, `content.created_by` — each `ALTER TABLE`
  wrapped in `try/except` so re-runs are safe.
- `_migrate_v2`: adds `ai_registry.error_category`, `.timeout_behavior`,
  `.rate_limit_behavior` — same safe pattern.
- Indexes: `idx_folders_parent`, `idx_content_folder`, `idx_contrib_status`,
  `idx_registry_avail`, `ux_ai_registry_provider_model_endpoint`, plus three
  `ai_model_usage` indexes.

### Requested-but-absent structures
There are **no** `subjects`, `blocks`, `semesters`, `notifications`, or
`reviews` tables. The academic hierarchy is expressed loosely through
`folders.node_type`, whose allowed keys are:
`general, books, audio, video, mcq, summaries` (`FOLDER_TYPE_OPTIONS`).
There is no semester/subject/block modelling.

---

## 5. TEST BASELINE

Run in this workspace (no `.env`, no `medbot_v2.sqlite3`).

| Suite | Result | Detail |
|---|---|---|
| `test_medbot_system` | ✅ 16 passed | folder/resource/contribution lifecycle, admin auth, search |
| `test_medbot_router` | ✅ 24 passed | callback robustness, unauthorized blocking, upload/rename states |
| `test_medbot_grounding` | ✅ 8 passed | grounded answers, refusal string, admin bootstrap |
| **Total unittest** | **48 passed / 0 failed** | |

### Baseline failures (pre-existing, not introduced by this phase)
1. **`test_db_patch.py` — FAILS/ERROR locally.** It does
   `shutil.copy2("medbot_v2.sqlite3", ...)`, which raises `FileNotFoundError`
   because the database is not present here. It also references
   `database.DB_PATH`, but `database.py` only defines `DB_NAME`, so it would
   raise `AttributeError` even with a database present. **Two independent
   defects; the test is stale.** Left untouched per phase instructions.
2. **`test_ai.py` and `test_keys.py` — "0 tests ran".** Neither defines a
   `unittest.TestCase`; they are standalone scripts that need real API keys.
   They are not runnable as unit-test suites. Not fixable without keys.
3. `python main.py` cannot run here (no `BOT_TOKEN`), so no live end-to-end
   Telegram verification is possible in this workspace.

Environment: Python 3.13, dependencies installed from `requirements.txt`.

---

## 6. RISKS

| # | Risk | Severity | Why |
|---|---|---|---|
| R1 | No RBAC in `admins` | High | Any admin can do everything; cannot add reviewers or limit scope |
| R2 | No audit trail | High | No record of who approved/published/deleted, or when |
| R3 | `settings` table unused | Medium | Persisted configuration has no code path; tempting but dead |
| R4 | Stale `test_db_patch.py` (wrong path attr + missing DB) | Medium | Gives false confidence; DB-integrity checks effectively unrun |
| R5 | Dead/duplicate DB functions | Low | `get_pending_contributions`, `get_all_contributions`, `get_folder_tree`, `ai_usage_stats`, `ai_usage_top_students`, `add_sub_admin_by_any`, `set_about_us`, `get/set_setting` |
| R6 | Admin not notified on new contribution | Medium | Submissions can sit unreviewed |
| R7 | No rejection reason / review metadata | Medium | Poor contributor feedback; no accountability |
| R8 | Shallow clone (grafted history) | Medium | Merge-base/blame operations unreliable until `git fetch --unshallow` |
| R9 | Single 3971-line `main.py` with one giant callback router | Medium | High cognitive load; regression risk on any edit |
| R10 | No semester/subject/block model | Medium | Academic hierarchy cannot be expressed; blocks Phase 2 scope |
| R11 | Broad `except Exception` + silent `pass` (e.g. approval notify) | Low | Failures can pass unnoticed |
| R12 | Multiple overlapping `run_*.sh` supervisors | Low | Operational confusion; unclear which is authoritative |

### Migration risks
- Any new column must follow the existing `try/except ALTER TABLE` pattern;
  a bare `ALTER` would break re-runs on the live database.
- `folders` and `contributions` cascade on folder delete. A future "semester"
  or "subject" parent must not silently cascade-delete contributions —
  `delete_folder()` already refuses non-empty folders for this reason.
- `ux_ai_registry_provider_model_endpoint` is a UNIQUE index; new provider
  writing paths must upsert, not blind-insert.
- New tables must be added to `init_db()` only, never as a manual migration
  script that could run against the operator's live DB without a backup.

---

## 7. PHASE 2 PLAN (proposal — not started)

Ordered, minimal-risk, each step independently shippable. Every schema change
uses the existing `try/except ALTER TABLE` migration pattern and is preceded by
a database backup.

**2.1 — Contribution review hardening (no schema change first)**
- Notify all admins when a contribution is created (`get_all_admins` exists).
- Return the rejection reason text to the contributor.

**2.2 — Review metadata (additive schema)**
- Add `contributions.reviewed_by`, `reviewed_at`, `review_note` via
  `_migrate_v3` (safe ALTER pattern).
- Populate in `approve_contribution` / `reject_contribution`; display in the
  review screen.

**2.3 — Admin audit log (additive schema)**
- New `admin_actions(id, admin_id, action, target_type, target_id, created_at)`
  written on approve/reject/delete/move/rename. Surface a filtered view under
  the admin panel.

**2.4 — RBAC roles (additive schema, backward compatible)**
- Add `admins.role` (`owner` | `admin` | `reviewer`), default `admin`.
- `configured_admin_id()` owner is written as `owner`.
- Introduce `has_permission(user_id, capability)`; gate contribution review and
  destructive resource ops. Defaults preserve current behaviour.

**2.5 — Admin management UI**
- Wire the existing `add_sub_admin` / `remove_sub_admin` / `get_all_admins`
  into an admin screen. No new DB work needed.

**2.6 — Academic hierarchy (design-gated)**
- Model semester → subject → block. Proposal: extend `folders.node_type` with
  `semester`, `subject`, `block` rather than new tables, to reuse the existing
  tree, breadcrumbs, search, and move/delete safety. Requires a decision before
  implementation.

**2.7 — Cleanup and test integrity**
- Fix or retire `test_db_patch.py` (use `DB_NAME`, skip when DB absent).
- Convert `test_ai.py` / `test_keys.py` into explicit scripts or proper tests.
- Remove superseded DB functions after confirming no external caller.

### Explicitly out of scope for Phase 2
- No rewrite of `main.py`'s router structure.
- No change to the AI layer beyond what already shipped.
- No change to the cascade rules on `folders` / `contributions`.
