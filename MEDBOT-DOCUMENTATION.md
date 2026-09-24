# MEDBOT Documentation

## Architecture

```
Student/Telegram User
    ↓
MEDBOT (python-telegram-bot)
    ↓
handle_callback / handle_text / handle_media
    ↓
database.py (aiosqlite → medbot_v2.sqlite3)
    ↓
search_engine.py (deterministic Arabic/English search)
    ↓
ai_router.py (AIFactory → Provider → GroundingValidator → response)
```

### Files
- `main.py` — Telegram bot entry point, handlers, admin security
- `database.py` — SQLite CRUD, schema migrations, contribution state machine
- `search_engine.py` — Deterministic search with Arabic normalization
- `ai_router.py` — Provider adapters (Gemini), router with failover, grounding
- `ai_discovery.py` — CLI discovery tool
- `messaging.py` — Contact Admin messaging system (isolated; see below)

## Database Schema

### users
`telegram_id (PK), full_name, joined_at`

### folders
`id (PK), parent_id (FK→folders), name, node_type, accepts_contributions`

### content
`id (PK), folder_id (FK→folders), title, file_id, file_type, source_type, source_contribution_id, created_by, created_at`

### contributions
`id (PK), user_id, user_name, folder_id, title, file_id, file_type, status, created_at`

### about_us
`id (PK CHECK=1), content, updated_at`

### ai_registry
`id (PK), provider, model, endpoint, availability, auth_status, latency_ms, success_rate, capabilities, last_success, last_failure, last_test, notes, error_category, timeout_behavior, rate_limit_behavior`

### settings
`key (PK), value`

### messages
`id (PK), user_id, user_name, category, body, status, admin_reply, reviewed_by, created_at, updated_at`

Contact Admin messaging only. `category` ∈ {message, summary, suggestion, report};
`status` ∈ {NEW, IN_REVIEW, REPLIED, CLOSED}. No FK to content/contributions.

## Migrations
- _migrate_v1: Added accepts_contributions (folders), source_type/source_contribution_id/created_by (content)
- _migrate_v2: Added error_category, timeout_behavior, rate_limit_behavior (ai_registry)
- _migrate_v3: Added reviewed_by, reviewed_at, review_note, rejection_reason, resubmitted_count (contributions) + idx_contrib_user_status
- _migrate_v4: Created `messages` table (Contact Admin) + idx_messages_status, idx_messages_user

All migrations are idempotent (ALTER TABLE ADD COLUMN / CREATE TABLE IF NOT EXISTS wrapped in try/except).
Migrations are additive only: existing rows are never rewritten, so pre-migration data stays valid.

## Modular separation (applied to new systems)

New systems must not accumulate in `main.py`/`database.py`. Each new system gets:

1. its own module (e.g. `messaging.py`),
2. its own DB access functions, namespaced to that system,
3. one entry point `register_<name>_handlers(app)` (the pattern used by
   `mcq_quiz.register_quiz_handlers` and `messaging.register_messaging_handlers`).

Existing working code is not refactored in place; the pattern is applied to new systems only.

### Contact Admin messaging (`messaging.py`)

- Isolated from library, contributions and exams: owns the `messages` table only.
- Entry point: `messaging.register_messaging_handlers(app)`, called in `main.py`
  **before** the catch-all `CallbackQueryHandler(callback_router)` so its
  `contact` / `msg_*` callbacks win.
- Callback namespace: `contact`, `msg_cat:<category>`, `msg_mine`, `msg_cancel`,
  `admin_messages`, `msg_open:<id>`, `msg_reply:<id>`, `msg_status:<id>:<status>`.
- Categories: `message`, `summary`, `suggestion`, `report`.
- Lifecycle: `NEW → IN_REVIEW → REPLIED → CLOSED`.
- Student flow: home button **📬 Contact Admin** → category → typed body →
  stored with status `NEW` + receipt confirmation → **📥 رسائلي** shows status,
  reply, and history.
- Admin flow: Admin Panel → **📬 رسائل الطلاب** → open a message → reply /
  mark in-review / close. Replying notifies the student and sets `REPLIED`.
- Authorization: every admin callback re-checks `database.is_user_admin()`;
  callback data is treated as untrusted. Malformed IDs fail safely.
- State keys (namespaced): `contact_category`, `contact_reply_id`. Cleared on
  `/cancel`, on returning home, and after a successful action.
- Text capture is wired into `ai_handler` ahead of the upload/title states, so a
  message body is never consumed by the AI or upload flows.

## Contribution Lifecycle

1. Student clicks `contrib_{folder_id}` → sends media
2. `add_contribution()` validates the submission then creates a pending entry
3. Admins are notified of the new contribution (per-recipient failures are non-fatal)
4. Admin opens the review screen: Approve / Reject / Needs Revision
5. `approve_contribution()`:
   - Transactional: BEGIN IMMEDIATE
   - Checks status ∈ ('pending', 'needs_revision')
   - Inserts into content with source_contribution_id
   - Updates contribution status to 'approved', records `reviewed_by` / `reviewed_at`
   - Notifies student
6. `reject_contribution()`:
   - Checks status ∈ ('pending', 'needs_revision')
   - Requires a typed reason from the admin; stores it in `rejection_reason`,
     records `reviewed_by` / `reviewed_at`, sets status 'rejected'
   - Notifies student with the reason
7. `request_contribution_revision()`:
   - Sets status 'needs_revision', stores the admin `review_note`, notifies student
8. Resubmission — `resubmit_contribution()`:
   - Owner-only and `needs_revision`-only
   - Replaces the media, clears review fields, returns status to 'pending',
     increments `resubmitted_count`
9. Double approval/rejection blocked by status check; publishing a rejected item is impossible

Statuses: `pending`, `approved`, `rejected`, `needs_revision`
(`pending` and `needs_revision` are the reviewable set).

## Submission validation

`validate_contribution_submission()` checks, and `add_contribution()` enforces
by raising `ContributionValidationError`:

- file id present and within `MAX_CONTRIBUTION_FILE_ID_LENGTH`
- title present and within `MAX_CONTRIBUTION_TITLE_LENGTH`
- file type ∈ `CONTRIBUTION_FILE_TYPES`
- target folder exists **and** accepts contributions
- no obvious duplicate from the same user in the same folder

## Admin Controls (ADMIN_ID env)

All sensitive operations require `is_admin(user_id)`:
- Folder: add, rename, move (with cycle prevention), delete
- Content: upload, rename, move, delete
- Contributions: approve, reject
- About Us: edit
- First-user escalation prevention: `ADMIN_ID != 0` check

## Security

- `ADMIN_ID=0` prevents all admin ops
- All admin callbacks gated by `if not authenticated: return`
- Cycle prevention: `is_descendant()` check in `move_folder()`
- Invalid ID handling: all operations check existence before modifying
- Contribution state machine prevents double approval/rejection

## AI Architecture

### Module layout (single source of truth)
- `ai.py` — the ONLY active AI implementation: provider discovery, adapters
  (Gemini / Groq / OpenRouter), error classification, failover, grounding
  validation, and the unified MEDBOT assistant.
- `ai_router.py` — thin compatibility facade that re-exports `ai.py`. It holds
  no provider logic; it exists only so older callers keep working.
- `ai_discovery.py` — CLI health/verification tool using the shared layer.

### Corrected provider class names
Earlier revisions of this document referenced `AIFactory`, `GeminiProvider`,
`OpenRouterProvider`, `OpenAIProvider`, `AIRouter`, `generate_with_failover`,
and `ai_generate_grounded`. Those names never existed in this codebase. The
real functions are:

- `_discover_gemini_models`, `_discover_groq_models`, `_discover_openrouter_models`
- `_get_candidates` (discovery → registry health → probe → VERIFIED pool)
- `_gemini_request`, `_openai_compatible_request`, `_request`
- `_classify_error`, `_record_success`, `_record_failure`
- `GroundingValidator`, `build_library_context`, `build_platform_catalog`
- `generate_medbot_unified_response` (the single student-facing assistant)
- `generate_medical_ai_response` (general medical AI path)
- `generate_medbot_assistant_response` (legacy MEDBOT-grounded resource path)

### Unified assistant (student-facing)
`generate_medbot_unified_response` is the one assistant option in the UI. It
answers general and medical questions and simultaneously helps the student
reach registered data. The pipeline is:

```
prompt → full registered platform catalog (build_platform_catalog)
       → deterministic SQLite search (search_engine)
       → verified PubMed sources
       → AI provider (failover)
       → answer
```

- The model may read and compare the ENTIRE registered catalog (every section
  and resource with its real path), which is what lets it point at the exact
  location of a resource and generate quick navigation answers.
- It may only mention items present in that catalog; it never invents a
  resource, path, or citation.
- The catalog is bounded by `ai.CATALOG_MAX_CHARS` so a huge library cannot
  overflow the provider context.
- With no provider available, the deterministic grounded search results are
  returned instead of hallucinated content; with no results either, it says so
  plainly.
- Each unified call consumes one unit of the daily AI quota.

### Medical answer format
`ai.UNIFIED_ASSISTANT_PROMPT` enforces a bilingual answer for medical
questions:

```
🩺 <topic>
**English (academic):**
<model academic answer in English — definition, key points, clinical relevance>
**العربية — شرح مختصر:**
<short, faithful Arabic explanation — not a distorted literal translation>
```

General (non-medical, e.g. "where is this resource") questions are answered in
Arabic directly with the real path, without the English block.

### Latency
The reply path is bounded so a slow provider or a cold cache does not stack
delays:

- The three independent loads (platform catalog, SQLite search, candidate
  pool) run concurrently with `asyncio.gather`.
- Provider discovery for the three providers runs concurrently, as does the
  probe batch (previously serial: up to 3 discovery round-trips + up to 6
  probes).
- Generation output is capped by `ai.MAX_OUTPUT_TOKENS` (900) so a runaway
  answer cannot dominate latency.
- `ai.warm_ai_pool()` runs once at startup (best-effort, background) so the
  first student reply does not pay for provider discovery.
- PubMed is only fetched when a provider exists to ground the answer.

### Daily allowance
The remaining balance is never displayed. It is removed from the home screen,
the account screen, `/quota`, and the assistant reply footer. The student is
told only when the allowance is used up:

- On the last allowed request the reply ends with
  "هذا آخر طلب متاح لك اليوم. تم الوصول إلى الحد المسموح به."
- The next request gets "تم الوصول إلى الحد المسموح به من الطلبات اليومية."
- The exact limit number is never disclosed.

`DAILY_LIMIT` and the quota database functions are unchanged; only the
presentation changed.

The former two-option gateway (`assistant_search` / `assistant_medical`) is
removed from the UI; those callbacks still resolve to the unified screen so
older messages never dead-end.

### Grounding (legacy path, retained)
`generate_medbot_assistant_response` enforces the original strict pipeline:

```
prompt → deterministic SQLite search (search_engine) → grounding context
       → AI Router/provider → GroundingValidator → Telegram text
```

- Search results (folders/content only) are injected as the sole permitted
  context. The full MEDBOT tree is never sent to the model.
- If search finds nothing, the model is NOT called and the exact refusal is
  returned: `الموارد المطلوبة غير مسجلة حالياً في MEDBOT.`
- If no provider is available, deterministic library results are returned
  instead of hallucinated content.

### Provider Discovery
- `ai_discovery.py` scans configured env vars and tests connectivity.
- Results are stored in `ai_registry` with availability status.

### Router/Failover
- `_get_candidates()` builds a pool of `VERIFIED` models only.
- Transient errors (timeout, 429, 5xx): try next candidate.
- Permanent errors (401, 403, model not found): classified and skipped.
- Registry is updated on each success/failure. Secrets are never logged.

## Known limitations (verified, not assumed)

- Provider health numbers in this document are historical and depend on live
  keys/network on the operator's Termux host; they are not reproducible in CI.
- Live Telegram delivery of each media type requires a real bot token and
  network, so it is validated by dispatch logic rather than an end-to-end call.
- `ai_registry` may accumulate repeated discovery rows over time.

## Admin bootstrap (implemented)

- `ADMIN_ID` (environment variable) is the only source of admin identity.
- `configured_admin_id()` returns `0` for unset / blank / `0` / non-numeric.
- `database.ensure_configured_admin()` is an idempotent no-op for IDs `<= 0`,
  so a missing `ADMIN_ID` never promotes the first (or any) user.
- `/whoami` lets the owner discover their numeric Telegram ID and shows
  whether they are currently an admin. It grants nothing.
