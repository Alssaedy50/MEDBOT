import aiosqlite
import logging

from datetime import datetime, date
from typing import Tuple
logger = logging.getLogger(__name__)
DB_NAME = "medbot_v2.sqlite3"

async def get_db():
    db = await aiosqlite.connect(DB_NAME)
    await db.execute("PRAGMA foreign_keys = ON;")
    return db

async def init_db():
    db = None
    try:
        db = await get_db()

        # ------------------------------------------------------------
        # Core schema
        # ------------------------------------------------------------
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER,
                name TEXT NOT NULL,
                node_type TEXT DEFAULT 'general',
                accepts_contributions INTEGER DEFAULT 0,
                FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_type TEXT DEFAULT 'doc',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source_type TEXT DEFAULT 'direct',
                source_contribution_id INTEGER DEFAULT NULL,
                created_by INTEGER DEFAULT NULL,
                FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS contributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                folder_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_type TEXT DEFAULT 'doc',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS about_us (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_ai_usage (
                user_id INTEGER,
                usage_date TEXT,
                request_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                model TEXT,
                endpoint TEXT,
                availability TEXT,
                auth_status TEXT,
                latency_ms REAL,
                success_rate REAL,
                capabilities TEXT,
                last_success TIMESTAMP,
                last_failure TIMESTAMP,
                last_test TIMESTAMP,
                notes TEXT
            )
        """)

        # ------------------------------------------------------------
        # AI model usage / observability
        # ------------------------------------------------------------
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registry_id INTEGER NOT NULL,
                user_id INTEGER,
                latency_ms REAL,
                success INTEGER NOT NULL DEFAULT 0,
                error_category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (registry_id) REFERENCES ai_registry(id)
                    ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_usage_registry
            ON ai_model_usage(registry_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_usage_user
            ON ai_model_usage(user_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_usage_created
            ON ai_model_usage(created_at)
        """)

        # ------------------------------------------------------------
        # Backward-compatible migrations
        # ------------------------------------------------------------
        await _migrate_v1(db)
        await _migrate_v2(db)

        # ------------------------------------------------------------
        # Performance / integrity indexes
        # ------------------------------------------------------------
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_folders_parent
            ON folders(parent_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_folder
            ON content(folder_id)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_contrib_status
            ON contributions(status)
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_registry_avail
            ON ai_registry(availability)
        """)

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_registry_provider_model_endpoint
            ON ai_registry(provider, model, endpoint)
        """)

        await db.commit()
        logger.info("Database initialized successfully.")

    except Exception as e:
        logger.error(f"DB Init Error: {e}")
        raise
    finally:
        if db is not None:
            await db.close()

async def _migrate_v1(db):
    """Backward-compatible migrations. Safe to re-run."""
    try:
        await db.execute("ALTER TABLE folders ADD COLUMN accepts_contributions INTEGER DEFAULT 0")
        logger.info("Migration: added accepts_contributions to folders")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE content ADD COLUMN source_type TEXT DEFAULT 'direct'")
        logger.info("Migration: added source_type to content")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE content ADD COLUMN source_contribution_id INTEGER DEFAULT NULL")
        logger.info("Migration: added source_contribution_id to content")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE content ADD COLUMN created_by INTEGER DEFAULT NULL")
        logger.info("Migration: added created_by to content")
    except Exception:
        pass

async def _migrate_v2(db):
    """Second migration: AI registry enhancements. Safe to re-run."""
    try:
        await db.execute("ALTER TABLE ai_registry ADD COLUMN error_category TEXT")
        logger.info("Migration v2: added error_category to ai_registry")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE ai_registry ADD COLUMN timeout_behavior TEXT")
        logger.info("Migration v2: added timeout_behavior to ai_registry")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE ai_registry ADD COLUMN rate_limit_behavior TEXT")
        logger.info("Migration v2: added rate_limit_behavior to ai_registry")
    except Exception:
        pass

async def register_user(user_id: int, username: str = None, full_name: str = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (user_id, username, full_name)
        )
        await db.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id)
        )
        await db.commit()
    finally:
        await db.close()

async def get_folders(parent_id: int = None):
    db = await get_db()
    try:
        if parent_id is None or parent_id == 0:
            async with db.execute(
                "SELECT id, name, node_type, accepts_contributions "
                "FROM folders WHERE parent_id IS NULL ORDER BY id ASC"
            ) as cur:
                res = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, name, node_type, accepts_contributions "
                "FROM folders WHERE parent_id = ? ORDER BY id ASC",
                (parent_id,)
            ) as cur:
                res = await cur.fetchall()
        return res
    finally:
        await db.close()

async def get_files(folder_id: int):
    db = await get_db()
    async with db.execute("SELECT id, title, file_id, file_type, source_type, source_contribution_id, created_by FROM content WHERE folder_id = ? ORDER BY id DESC", (folder_id,)) as cur:
        res = await cur.fetchall()
    await db.close()
    return res

async def get_breadcrumbs(folder_id: int):
    if folder_id == 0: return "الرئيسية 🏠"
    db = await get_db()
    path = []
    curr = folder_id
    while curr:
        async with db.execute("SELECT parent_id, name FROM folders WHERE id = ?", (curr,)) as cur:
            row = await cur.fetchone()
            if not row: break
            path.append(row[1])
            curr = row[0]
    await db.close()
    path.append("الرئيسية 🏠")
    path.reverse()
    return " ⬅️ ".join(path)

async def get_breadcrumbs_inline(db_conn, folder_id: int):
    if folder_id == 0: return "الرئيسية 🏠"
    path = []
    curr = folder_id
    while curr:
        async with db_conn.execute("SELECT parent_id, name FROM folders WHERE id = ?", (curr,)) as cur:
            row = await cur.fetchone()
            if not row: break
            path.append(row[1])
            curr = row[0]
    path.append("الرئيسية 🏠")
    path.reverse()
    return " ⬅️ ".join(path)

async def add_folder(parent_id: int, name: str, node_type: str, accepts_contributions: int = 0):
    db = await get_db()
    pid = None if parent_id == 0 else parent_id
    await db.execute("INSERT INTO folders (parent_id, name, node_type, accepts_contributions) VALUES (?, ?, ?, ?)", (pid, name, node_type, accepts_contributions))
    await db.commit()
    await db.close()
    return True

async def delete_folder(folder_id: int) -> bool:
    """Delete an empty folder only. Non-existent or non-empty folders are refused."""
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM folders WHERE id = ?",
            (folder_id,),
        ) as cur:
            if not await cur.fetchone():
                return False

        async with db.execute(
            "SELECT COUNT(*) FROM folders WHERE parent_id = ?",
            (folder_id,),
        ) as cur:
            child_row = await cur.fetchone()

        if child_row and int(child_row[0]) > 0:
            return False

        async with db.execute(
            "SELECT COUNT(*) FROM content WHERE folder_id = ?",
            (folder_id,),
        ) as cur:
            content_row = await cur.fetchone()

        if content_row and int(content_row[0]) > 0:
            return False

        # Contributions reference the folder with ON DELETE CASCADE; deleting
        # the folder would silently discard student submissions.
        async with db.execute(
            "SELECT COUNT(*) FROM contributions WHERE folder_id = ?",
            (folder_id,),
        ) as cur:
            contribution_row = await cur.fetchone()

        if contribution_row and int(contribution_row[0]) > 0:
            return False

        await db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        await db.commit()
        return True
    except Exception:
        logger.exception("delete_folder failed for id=%s", folder_id)
        return False
    finally:
        await db.close()

async def get_folder_children_count(folder_id: int) -> int:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT COUNT(*) FROM folders WHERE parent_id = ?",
            (folder_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        await db.close()

async def update_folder_name(folder_id: int, new_name: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE folders SET name = ? WHERE id = ?",
            (new_name, folder_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def update_folder_type(folder_id: int, node_type: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE folders SET node_type = ? WHERE id = ?",
            (node_type, folder_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def update_folder_accepts_contributions(folder_id: int, value: int):
    db = await get_db()
    await db.execute("UPDATE folders SET accepts_contributions = ? WHERE id = ?", (value, folder_id))
    await db.commit()
    await db.close()

async def get_parent_id(folder_id: int):
    db = await get_db()
    async with db.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,)) as cur:
        res = await cur.fetchone()
    await db.close()
    if not res or res[0] is None: return 0
    return res[0]

async def get_folder(folder_id: int):
    db = await get_db()
    async with db.execute("SELECT id, parent_id, name, node_type, accepts_contributions FROM folders WHERE id = ?", (folder_id,)) as cur:
        res = await cur.fetchone()
    await db.close()
    return res

async def folder_accepts_contributions(folder_id: int) -> bool:
    db = await get_db()
    async with db.execute("SELECT accepts_contributions FROM folders WHERE id = ?", (folder_id,)) as cur:
        res = await cur.fetchone()
    await db.close()
    return bool(res and res[0])

async def is_descendant(db, ancestor_id: int, folder_id: int) -> bool:
    """Check if folder_id is a descendant of ancestor_id (for cycle prevention)."""
    current = folder_id
    visited = set()
    while current is not None:
        if current == ancestor_id:
            return True
        if current in visited:
            return True
        visited.add(current)
        async with db.execute("SELECT parent_id FROM folders WHERE id = ?", (current,)) as cur:
            row = await cur.fetchone()
        if not row:
            break
        current = row[0]
    return False

async def move_folder(folder_id: int, new_parent_id: int) -> tuple[bool, str]:
    db = await get_db()
    try:
        if folder_id == new_parent_id:
            return False, "لا يمكن نقل المجلد إلى نفسه"
        if new_parent_id != 0:
            async with db.execute("SELECT id FROM folders WHERE id = ?", (new_parent_id,)) as cur:
                if not await cur.fetchone():
                    return False, "المجلد الهدف غير موجود"
            if await is_descendant(db, folder_id, new_parent_id):
                return False, "لا يمكن نقل مجلد إلى داخل أحد تفرعاته"
        new_pid = None if new_parent_id == 0 else new_parent_id
        await db.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (new_pid, folder_id))
        await db.commit()
        return True, "تم نقل المجلد بنجاح"
    except Exception as e:
        return False, f"خطأ في النقل: {e}"
    finally:
        await db.close()

async def add_content(folder_id: int, title: str, file_id: str, file_type: str, source_type: str = "direct", source_contribution_id: int = None, created_by: int = None):
    db = await get_db()
    await db.execute(
        "INSERT INTO content (folder_id, title, file_id, file_type, source_type, source_contribution_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (folder_id, title, file_id, file_type, source_type, source_contribution_id, created_by)
    )
    cursor = await db.execute("SELECT last_insert_rowid()")
    row = await cursor.fetchone()
    cid = row[0]
    await db.commit()
    await db.close()
    return cid

async def get_file_record(content_id: int):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, folder_id, title, file_id, file_type, source_type, "
            "source_contribution_id, created_by FROM content WHERE id = ?",
            (content_id,),
        ) as cur:
            res = await cur.fetchone()
        return res
    finally:
        await db.close()

async def update_file_title(content_id: int, new_title: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE content SET title = ? WHERE id = ?",
            (new_title, content_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def update_content_type(content_id: int, file_type: str) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE content SET file_type = ? WHERE id = ?",
            (file_type, content_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def move_content(content_id: int, new_folder_id: int) -> tuple[bool, str]:
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM folders WHERE id = ?", (new_folder_id,)) as cur:
            if not await cur.fetchone():
                return False, "المجلد الهدف غير موجود"
        async with db.execute("SELECT id FROM content WHERE id = ?", (content_id,)) as cur:
            if not await cur.fetchone():
                return False, "الملف غير موجود"
        await db.execute("UPDATE content SET folder_id = ? WHERE id = ?", (new_folder_id, content_id))
        await db.commit()
        return True, "تم نقل الملف بنجاح"
    except Exception as e:
        return False, f"خطأ: {e}"
    finally:
        await db.close()

async def delete_file(content_id: int) -> bool:
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM content WHERE id = ?", (content_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def search_content(keyword: str):
    """
    Unified MEDBOT search.

    Searches registered content by:
    - content title
    - current folder name
    - any ancestor folder name in the full hierarchy

    Empty/whitespace queries return no results.

    Result shape:
        (content_id, title, file_type, folder_id, folder_name, full_path)
    """
    keyword = (keyword or "").strip()

    if not keyword:
        return []

    db = await get_db()

    try:
        like = f"%{keyword}%"

        sql = """
            WITH RECURSIVE folder_paths AS (
                SELECT
                    id AS folder_id,
                    parent_id,
                    name,
                    name AS path_text
                FROM folders

                UNION ALL

                SELECT
                    fp.folder_id,
                    f.parent_id,
                    f.name,
                    f.name || ' / ' || fp.path_text
                FROM folder_paths fp
                JOIN folders f
                    ON f.id = fp.parent_id
            ),
            resolved_paths AS (
                SELECT
                    folder_id,
                    MAX(path_text) AS path_text
                FROM folder_paths
                GROUP BY folder_id
            )
            SELECT DISTINCT
                c.id,
                c.title,
                c.file_type,
                c.folder_id,
                f.name AS folder_name,
                COALESCE(rp.path_text, f.name) AS full_path
            FROM content c
            JOIN folders f
                ON c.folder_id = f.id
            LEFT JOIN resolved_paths rp
                ON rp.folder_id = f.id
            WHERE
                c.title LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM folder_paths fp
                    WHERE fp.folder_id = c.folder_id
                      AND fp.path_text LIKE ?
                )
            ORDER BY c.id DESC
            LIMIT 30
        """

        async with db.execute(sql, (like, like)) as cur:
            return await cur.fetchall()

    finally:
        await db.close()


async def get_catalog_for_ai() -> str:
    db = await get_db()
    summary = []
    async with db.execute("SELECT id, name FROM folders") as cur:
        folders = await cur.fetchall()
    for fid, fname in folders:
        path = await get_breadcrumbs(fid)
        async with db.execute("SELECT title FROM content WHERE folder_id = ?", (fid,)) as fcur:
            files = await fcur.fetchall()
        file_titles = ", ".join([f[0] for f in files]) if files else "لا توجد ملفات حالياً"
        summary.append(f"- قسم: {path} | يحتوي على: [{file_titles}]")
    await db.close()
    return "\n".join(summary) if summary else "قاعدة البيانات لا تزال قيد الإنشاء من قبل الإدارة."

async def add_contribution(user_id: int, user_name: str, folder_id: int, title: str, file_id: str, file_type: str) -> int:
    db = await get_db()
    cursor = await db.execute("""
        INSERT INTO contributions (user_id, user_name, folder_id, title, file_id, file_type, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (user_id, user_name, folder_id, title, file_id, file_type))
    cid = cursor.lastrowid
    await db.commit()
    await db.close()
    return cid

async def approve_contribution(contrib_id: int):
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT folder_id, title, file_id, file_type, user_id, status
            FROM contributions
            WHERE id = ?
        """, (contrib_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await db.rollback()
            return None
        fid, title, file_id, f_type, uid, status = row
        if status != "pending":
            await db.rollback()
            return None
        cursor = await db.execute(
            "INSERT INTO content (folder_id, title, file_id, file_type, source_type, source_contribution_id, created_by) VALUES (?, ?, ?, ?, 'contribution', ?, ?)",
            (fid, title, file_id, f_type, contrib_id, uid)
        )
        cid = cursor.lastrowid
        await db.execute("""
            UPDATE contributions
            SET status = 'approved'
            WHERE id = ? AND status = 'pending'
        """, (contrib_id,))
        await db.commit()
        return (fid, title, file_id, f_type, uid)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

async def reject_contribution(contrib_id: int):
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
            SELECT user_id, title, status
            FROM contributions
            WHERE id = ?
        """, (contrib_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await db.rollback()
            return None
        uid, title, status = row
        if status != "pending":
            await db.rollback()
            return None
        await db.execute("""
            UPDATE contributions
            SET status = 'rejected'
            WHERE id = ? AND status = 'pending'
        """, (contrib_id,))
        await db.commit()
        return (uid, title)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

async def get_pending_contributions():
    db = await get_db()
    async with db.execute("""
        SELECT id, user_id, user_name, folder_id, title, file_id, file_type
        FROM contributions
        WHERE status = 'pending'
        ORDER BY created_at ASC
    """) as cur:
        rows = await cur.fetchall()
    await db.close()
    return rows

async def get_all_contributions(limit=50):
    db = await get_db()
    async with db.execute("""
        SELECT id, user_id, user_name, folder_id, title, file_type, status, created_at
        FROM contributions
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,)) as cur:
        rows = await cur.fetchall()
    await db.close()
    return rows

async def get_about_us() -> str:
    db = await get_db()
    async with db.execute("SELECT content FROM about_us WHERE id = 1") as cur:
        row = await cur.fetchone()
    await db.close()
    if row:
        return row[0]
    return None

async def set_about_us(content: str):
    db = await get_db()
    await db.execute("""
        INSERT INTO about_us (id, content, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET content = excluded.content, updated_at = CURRENT_TIMESTAMP
    """, (content,))
    await db.commit()
    await db.close()

async def get_setting(key: str, default: str = None) -> str:
    db = await get_db()
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    await db.close()
    return row[0] if row else default

async def set_setting(key: str, value: str):
    db = await get_db()
    await db.execute("""
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    await db.commit()
    await db.close()

async def ai_registry_add(provider: str, model: str = None, endpoint: str = None, availability: str = None, auth_status: str = None, latency_ms: float = None, success_rate: float = None, capabilities: str = None, notes: str = None):
    """Idempotently register an AI provider/model/endpoint.

    Canonical identity:
        (provider, model, endpoint)

    Incoming NULL values never erase existing non-NULL values.
    """
    db = await get_db()
    await db.execute("""
        INSERT INTO ai_registry (
            provider, model, endpoint, availability, auth_status,
            latency_ms, success_rate, capabilities, last_test, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(provider, model, endpoint) DO UPDATE SET
            availability = COALESCE(excluded.availability, ai_registry.availability),
            auth_status = COALESCE(excluded.auth_status, ai_registry.auth_status),
            latency_ms = COALESCE(excluded.latency_ms, ai_registry.latency_ms),
            success_rate = COALESCE(excluded.success_rate, ai_registry.success_rate),
            capabilities = COALESCE(excluded.capabilities, ai_registry.capabilities),
            notes = COALESCE(excluded.notes, ai_registry.notes),
            last_test = CURRENT_TIMESTAMP
    """, (
        provider, model, endpoint, availability, auth_status,
        latency_ms, success_rate, capabilities, notes
    ))
    await db.commit()
    await db.close()

async def ai_registry_update(id: int, **kwargs):
    db = await get_db()
    sets = []
    vals = []
    for k, v in kwargs.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("last_test = CURRENT_TIMESTAMP")
    await db.execute(f"UPDATE ai_registry SET {', '.join(sets)} WHERE id = ?", (*vals, id))
    await db.commit()
    await db.close()

async def ai_registry_mark_success(
    id: int,
    latency_ms: float = None,
    notes: str = "verified",
):
    """Record a successful provider/model operation.

    State semantics:
    - availability -> VERIFIED
    - auth_status -> valid
    - last_success -> current UTC timestamp
    - last_test -> current UTC timestamp
    - error_category -> NULL
    """
    db = await get_db()
    try:
        await db.execute("""
            UPDATE ai_registry
            SET
                availability = 'VERIFIED',
                auth_status = 'valid',
                latency_ms = COALESCE(?, latency_ms),
                last_success = CURRENT_TIMESTAMP,
                last_test = CURRENT_TIMESTAMP,
                error_category = NULL,
                notes = ?
            WHERE id = ?
        """, (latency_ms, notes, id))

        async with db.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(success), 0) AS successful
            FROM ai_model_usage
            WHERE registry_id = ?
        """, (id,)) as cur:
            row = await cur.fetchone()

        total = row[0] if row else 0
        successful = row[1] if row else 0

        if total > 0:
            success_rate = successful / total
            await db.execute(
                "UPDATE ai_registry SET success_rate = ? WHERE id = ?",
                (success_rate, id),
            )

        await db.commit()
    finally:
        await db.close()


async def ai_registry_mark_failure(
    id: int,
    availability: str,
    error_category: str,
    auth_status: str = None,
    notes: str = None,
):
    """Record a failed provider/model operation.

    The caller supplies the already-classified availability state.
    This function never invents or reclassifies errors.
    """
    allowed_states = {
        "RATE_LIMITED",
        "QUOTA_LIMITED",
        "TIMEOUT",
        "NETWORK_ERROR",
        "AUTH_FAILED",
        "UPSTREAM_ERROR",
        "UNAVAILABLE",
    }

    if availability not in allowed_states:
        raise ValueError(
            f"Invalid failure availability state: {availability!r}"
        )

    db = await get_db()
    try:
        await db.execute("""
            UPDATE ai_registry
            SET
                availability = ?,
                auth_status = COALESCE(?, auth_status),
                last_failure = CURRENT_TIMESTAMP,
                last_test = CURRENT_TIMESTAMP,
                error_category = ?,
                notes = COALESCE(?, notes)
            WHERE id = ?
        """, (
            availability,
            auth_status,
            error_category,
            notes,
            id,
        ))

        async with db.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(success), 0) AS successful
            FROM ai_model_usage
            WHERE registry_id = ?
        """, (id,)) as cur:
            row = await cur.fetchone()

        total = row[0] if row else 0
        successful = row[1] if row else 0

        if total > 0:
            success_rate = successful / total
            await db.execute(
                "UPDATE ai_registry SET success_rate = ? WHERE id = ?",
                (success_rate, id),
            )

        await db.commit()
    finally:
        await db.close()


async def ai_registry_get_all():
    db = await get_db()
    async with db.execute("SELECT * FROM ai_registry ORDER BY last_test DESC") as cur:
        rows = await cur.fetchall()
    await db.close()
    return rows

async def ai_registry_get_healthy():
    db = await get_db()
    async with db.execute("""
        SELECT * FROM ai_registry 
        WHERE availability IN ('AVAILABLE', 'VERIFIED') 
        ORDER BY success_rate DESC, latency_ms ASC
    """) as cur:
        rows = await cur.fetchall()
    await db.close()
    return rows

async def get_folder_tree(folder_id: int = None, depth: int = 0, max_depth: int = 10):
    """Get full folder tree structure for admin view."""
    db = await get_db()
    result = []
    if folder_id is None:
        async with db.execute("SELECT id, name, node_type FROM folders WHERE parent_id IS NULL ORDER BY id") as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute("SELECT id, name, node_type FROM folders WHERE parent_id = ? ORDER BY id", (folder_id,)) as cur:
            rows = await cur.fetchall()
    for fid, name, ntype in rows:
        entry = {"id": fid, "name": name, "node_type": ntype, "children": []}
        if depth < max_depth:
            entry["children"] = await get_folder_tree(fid, depth + 1, max_depth)
        result.append(entry)
    await db.close()
    return result

# --- Admin Management Functions ---
async def add_sub_admin(telegram_id: int, username: str = None) -> bool:
    db = await get_db()
    try:
        await db.execute("INSERT OR REPLACE INTO admins (telegram_id, username) VALUES (?, ?)", (telegram_id, username))
        await db.commit()
        return True
    except Exception:
        return False
    finally:
        await db.close()

async def remove_sub_admin(telegram_id: int) -> bool:
    db = await get_db()
    try:
        await db.execute("DELETE FROM admins WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
        return True
    except Exception:
        return False
    finally:
        await db.close()

async def get_all_admins():
    db = await get_db()
    async with db.execute("SELECT telegram_id, username, added_at FROM admins ORDER BY added_at ASC") as cur:
        rows = await cur.fetchall()
    await db.close()
    return rows

async def is_user_admin(telegram_id: int) -> bool:
    db = await get_db()
    async with db.execute("SELECT 1 FROM admins WHERE telegram_id = ?", (telegram_id,)) as cur:
        row = await cur.fetchone()
    await db.close()
    return bool(row)


# --- دوال الإدارة المتقدمة للمالك ---
async def add_sub_admin_by_any(identifier: str) -> tuple[bool, str]:
    db = await get_db()
    identifier = identifier.strip().lstrip("@")
    try:
        if identifier.isdigit():
            tid = int(identifier)
            # محاولة جلب الاسم إن كان مسجلاً في البوت
            async with db.execute("SELECT full_name FROM users WHERE user_id = ?", (tid,)) as cur:
                row = await cur.fetchone()
            uname = row[0] if row else ""
            await db.execute("INSERT OR REPLACE INTO admins (telegram_id, username) VALUES (?, ?)", (tid, uname))
            await db.commit()
            return True, f"تمت إضافة المشرف بنجاح (ID: {tid})"
        else:
            # البحث باليوزر في جدول المستخدمين
            async with db.execute("SELECT user_id, full_name FROM users WHERE full_name LIKE ? OR user_id = ?", (f"%{identifier}%", identifier)) as cur:
                row = await cur.fetchone()
            if row:
                tid = row[0]
                await db.execute("INSERT OR REPLACE INTO admins (telegram_id, username) VALUES (?, ?)", (tid, identifier))
                await db.commit()
                return True, f"تم العثور على المستخدم @{identifier} وإضافته كمشرف (ID: {tid})"
            else:
                return False, f"لم يتم العثور على مستخدم بالمعرف @{identifier}. اطلب منه إرسال /start للبوت أولاً أو أرسل الـ Telegram ID الرقمي مباشرة."
    except Exception as e:
        return False, f"خطأ أثناء الإضافة: {str(e)}"
    finally:
        await db.close()

async def get_all_user_ids() -> list:
    db = await get_db()
    async with db.execute("SELECT user_id FROM users") as cur:
        rows = await cur.fetchall()
    await db.close()
    return [r[0] for r in rows]

async def get_pending_contributions_count() -> int:
    db = await get_db()
    async with db.execute("SELECT COUNT(*) FROM contributions WHERE status = 'pending'") as cur:
        row = await cur.fetchone()
    await db.close()
    return row[0] if row else 0

async def get_pending_contributions_list():
    db = await get_db()
    try:
        async with db.execute("""
            SELECT
                id,
                user_id,
                user_name,
                title,
                file_id,
                file_type,
                folder_id,
                status,
                created_at
            FROM contributions
            WHERE status = 'pending'
            ORDER BY id DESC
        """) as cur:
            rows = await cur.fetchall()
        return rows
    finally:
        await db.close()

async def check_and_increment_quota(user_id: int, max_limit: int = 20) -> Tuple[bool, int]:
    """
    Atomically consume one daily AI request.

    BEGIN IMMEDIATE serializes concurrent quota updates so two requests
    cannot both read the same old count and exceed max_limit.
    """
    today = date.today().isoformat()
    db = await get_db()

    try:
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            "SELECT request_count FROM daily_ai_usage "
            "WHERE user_id = ? AND usage_date = ?",
            (user_id, today),
        )
        row = await cursor.fetchone()
        current_count = row[0] if row else 0

        if current_count >= max_limit:
            await db.rollback()
            return False, 0

        new_count = current_count + 1

        await db.execute(
            """
            INSERT INTO daily_ai_usage (
                user_id,
                usage_date,
                request_count
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, usage_date)
            DO UPDATE SET request_count = excluded.request_count
            """,
            (user_id, today, new_count),
        )

        await db.commit()
        return True, max_limit - new_count

    except Exception:
        await db.rollback()
        raise

    finally:
        await db.close()


async def get_remaining_quota(user_id: int, max_limit: int = 20) -> int:
    today = date.today().isoformat()
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT request_count FROM daily_ai_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, today)
        )
        row = await cursor.fetchone()
        current_count = row[0] if row else 0
        return max(0, max_limit - current_count)
    finally:
        await db.close()



# ---------------------------------------------------------------------------
# AI model usage analytics
# ---------------------------------------------------------------------------

async def ai_usage_record(
    registry_id: int,
    user_id: int = None,
    latency_ms: float = None,
    success: bool = False,
    error_category: str = None,
):
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO ai_model_usage (
                registry_id,
                user_id,
                latency_ms,
                success,
                error_category
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                registry_id,
                user_id,
                latency_ms,
                1 if success else 0,
                error_category,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def ai_usage_stats(days: int = 1):
    db = await get_db()
    try:
        async with db.execute(
            """
            SELECT
                r.id,
                r.provider,
                r.model,
                r.availability,
                COUNT(u.id) AS total_requests,
                COALESCE(SUM(u.success), 0) AS successful_requests,
                COUNT(DISTINCT u.user_id) AS unique_students,
                ROUND(AVG(u.latency_ms), 1) AS avg_latency_ms,
                MAX(u.created_at) AS last_used
            FROM ai_registry r
            LEFT JOIN ai_model_usage u
                ON u.registry_id = r.id
                AND u.created_at >= datetime('now', ?)
            GROUP BY
                r.id,
                r.provider,
                r.model,
                r.availability
            ORDER BY
                unique_students DESC,
                total_requests DESC,
                avg_latency_ms ASC
            """,
            (f"-{int(days)} days",),
        ) as cur:
            return await cur.fetchall()
    finally:
        await db.close()


async def ai_usage_top_students(days: int = 1, limit: int = 10):
    db = await get_db()
    try:
        async with db.execute(
            """
            SELECT
                user_id,
                COUNT(*) AS total_requests,
                SUM(success) AS successful_requests,
                ROUND(AVG(latency_ms), 1) AS avg_latency_ms
            FROM ai_model_usage
            WHERE user_id IS NOT NULL
              AND created_at >= datetime('now', ?)
            GROUP BY user_id
            ORDER BY total_requests DESC
            LIMIT ?
            """,
            (f"-{int(days)} days", int(limit)),
        ) as cur:
            return await cur.fetchall()
    finally:
        await db.close()

async def ai_registry_ensure(
    provider: str,
    model: str,
    endpoint: str,
    availability: str = "AVAILABLE",
    auth_status: str = None,
    capabilities: str = "text_generation",
):
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO ai_registry (
                provider,
                model,
                endpoint,
                availability,
                auth_status,
                capabilities
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model, endpoint)
            DO NOTHING
            """,
            (
                provider,
                model,
                endpoint,
                availability,
                auth_status,
                capabilities,
            ),
        )
        await db.commit()

        async with db.execute(
            """
            SELECT id
            FROM ai_registry
            WHERE provider = ?
              AND model = ?
              AND endpoint = ?
            LIMIT 1
            """,
            (provider, model, endpoint),
        ) as cur:
            row = await cur.fetchone()

        return row[0] if row else None
    finally:
        await db.close()
