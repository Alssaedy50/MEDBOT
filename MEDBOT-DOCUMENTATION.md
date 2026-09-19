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

## Migrations
- _migrate_v1: Added accepts_contributions (folders), source_type/source_contribution_id/created_by (content)
- _migrate_v2: Added error_category, timeout_behavior, rate_limit_behavior (ai_registry)

All migrations are idempotent (ALTER TABLE ADD COLUMN wrapped in try/except).

## Contribution Lifecycle

1. Student clicks `contrib_{folder_id}` → sends media
2. `add_contribution()` creates pending entry
3. Admin receives file + approve/reject buttons
4. `approve_contribution()`:
   - Transactional: BEGIN IMMEDIATE
   - Checks status = 'pending'
   - Inserts into content with source_contribution_id
   - Updates contribution status to 'approved'
   - Notifies student
5. `reject_contribution()`:
   - Checks status = 'pending'
   - Updates status to 'rejected'
   - Notifies student
6. Double approval/rejection blocked by status check

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

### Provider Discovery
- ai_discovery.py scans env vars and tests connectivity
- Results stored in ai_registry with availability status

### Current Available Providers
- Google Gemini (gemini-flash-lite-latest): AVAILABLE, ~1.7s latency
- Google Gemini (gemma-4-26b-a4b-it): AVAILABLE, ~2.9s latency

### Router/Failover
- `_get_healthy_providers_ranked()`: sorts by success_rate/latency
- `generate_with_failover()`: tries providers in rank order
- Transient errors (timeout, 429, 5xx): retry next provider
- Permanent errors (401, 403, model not found): skip immediately
- Updates registry on each success/failure

### Grounding
- `ai_generate_grounded()`: searches library, builds system prompt with found resources
- System prompt instructs AI not to invent resources
- Absent resources return: "الموارد المطلوبة غير مسجلة حالياً في MEDBOT."

## Benchmark Results (2026-09-16)

| Provider | Model | Latency | Availability | Auth |
|----------|-------|---------|-------------|------|
| google_gemini | gemini-flash-lite-latest | 1664ms | AVAILABLE | valid |
| google_gemini | gemma-4-26b-a4b-it | 2850ms | AVAILABLE | valid |

Grounding tests: 7/7 pass. Non-existent resources correctly rejected.

## Routing/Failover

- Default order: gemini-flash-lite-latest → gemma-4-26b-a4b-it
- Each test updates registry with latency/status
- No retries for permanent errors (auth, model)
- Max 1 retry for transient errors before fallback

## Legacy Repair

Contribution #1: Approved contribution in folder 21 (Pharmacology) had no corresponding content row.
- file_id verified from contribution record
- Content inserted with source_type='contribution', source_contribution_id=1, created_by=contributor
- Contribution remains approved
- Content exists exactly once in folder 21

Contributions 3 and 4: Had content rows but missing source metadata.
- source_type updated from 'direct' to 'contribution'
- source_contribution_id set to correct contribution ID
- created_by set to contributor ID

## QA Results

All tests pass:
- Python compilation (5 files)
- Database integrity (PRAGMA integrity_check: ok)
- Foreign keys enforced via get_db()
- Schema with all migration columns
- Tree navigation and breadcrumbs
- Arabic and English search
- Content retrieval and delivery dispatch
- Contribution lifecycle (double approval/rejection blocked)
- Admin authorization (all sensitive callbacks)
- First-user security (ADMIN_ID != 0)
- Folder safety (cycle prevention, invalid target)
- Content safety (rename, move, delete)
- About Us (editable, factual)
- AI discovery, registry, health, benchmark, routing, failover, grounding

## Remaining Warnings

1. Only Google Gemini provider has keys configured. OpenRouter and OpenAI require keys for redundancy.
2. AI grounding validator's `validate()` method is a pass-through; no active hallucination filtering beyond system prompt.
3. No integration test with live Telegram API (requires real token and network).
4. AI registry has duplicate rows from repeated discovery runs.
5. IPv4 workaround is applied globally to socket.getaddrinfo — may affect other HTTP clients.