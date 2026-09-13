import json
import datetime
import asyncio
import threading
import psycopg2
from config import DATABASE_URL
from utils.encrypt import ecs, dcs

# --- PostgreSQL Database Setup ---
db_lock = threading.Lock()
_conn = None

def get_connection():
    """Get or create a persistent PostgreSQL connection."""
    global _conn
    try:
        if _conn is not None and _conn.closed == 0:
            return _conn
    except Exception:
        pass
    _conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    _conn.autocommit = False
    return _conn

def db_execute_sync(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Thread-safe synchronous DB execute."""
    try:
        with db_lock:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(query, params)
            if commit:
                conn.commit()
            if fetchone:
                return cursor.fetchone()
            if fetchall:
                return cursor.fetchall()
            return None
    except Exception as e:
        print(f"PostgreSQL error ({query}): {e}")
        try:
            global _conn
            if _conn:
                try:
                    _conn.rollback()
                except Exception:
                    pass
                try:
                    _conn.close()
                except Exception:
                    pass
            _conn = None
        except Exception:
            pass
        return None

async def db_exec(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Async wrapper — runs DB call in background thread so bot stays responsive."""
    return await asyncio.to_thread(db_execute_sync, query, params, fetchone, fetchall, commit)

def init_pg_db():
    """Initialize PostgreSQL tables."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_seen TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id BIGINT PRIMARY KEY,
                session TEXT,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id BIGINT PRIMARY KEY,
                data TEXT
            )
        """)
        conn.commit()
        print("[OK] PostgreSQL database initialized successfully!")
    except Exception as e:
        print(f"[ERROR] PostgreSQL init error: {e}")

init_pg_db()

DEFAULT_SETTINGS = {
    "upload_mode": "Telegram",
    "public_channel": "Re-Upload",
    "large_files": "Splitting",
    "upload_format": "Default",
    "send_pm": "On",
    "set_upload": "Not Set",
    "thumbnail": "Not Set",
    "caption": "Not Set",
    "rename": "Not Set",
}

# --- User Management ---
async def register_user(user_id: int, username: str = None, first_name: str = None):
    await db_exec(
        "INSERT INTO users (user_id, username, first_name, last_seen) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_seen = EXCLUDED.last_seen",
        (user_id, username, first_name, datetime.datetime.utcnow().isoformat()),
        commit=True
    )

async def get_all_user_ids():
    user_ids = set()
    rows = await db_exec("SELECT user_id FROM users", fetchall=True)
    if rows:
        for r in rows:
            user_ids.add(r[0])
    return list(user_ids)

# --- Pyrogram Session String ---
async def save_session(user_id: int, session_string: str):
    encrypted_session = ecs(session_string)
    await db_exec(
        "INSERT INTO sessions (user_id, session, updated_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET session = EXCLUDED.session, updated_at = EXCLUDED.updated_at",
        (user_id, encrypted_session, datetime.datetime.utcnow().isoformat()),
        commit=True
    )
    print(f"[OK] Session saved in PostgreSQL for user {user_id}")

async def get_session(user_id: int):
    row = await db_exec("SELECT session FROM sessions WHERE user_id = %s", (user_id,), fetchone=True)
    if row and row[0]:
        return dcs(row[0])
    return None

async def delete_session(user_id: int):
    await db_exec("DELETE FROM sessions WHERE user_id = %s", (user_id,), commit=True)

# --- Full User Settings ---
async def get_user_settings(user_id: int) -> dict:
    settings = DEFAULT_SETTINGS.copy()
    row = await db_exec("SELECT data FROM settings WHERE user_id = %s", (user_id,), fetchone=True)
    if row and row[0]:
        try:
            settings.update(json.loads(row[0]))
        except Exception:
            pass
    return settings

async def update_user_setting(user_id: int, key: str, value):
    current = await get_user_settings(user_id)
    current[key] = value
    await db_exec(
        "INSERT INTO settings (user_id, data) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data",
        (user_id, json.dumps(current)),
        commit=True
    )

# --- Legacy Setting Helpers ---
async def save_thumbnail(user_id: int, file_id: str):
    await update_user_setting(user_id, "thumbnail", "Set")
    await update_user_setting(user_id, "thumbnail_id", file_id)

async def get_thumbnail(user_id: int):
    settings = await get_user_settings(user_id)
    return settings.get("thumbnail_id")

async def delete_thumbnail(user_id: int):
    await update_user_setting(user_id, "thumbnail", "Not Set")
    current = await get_user_settings(user_id)
    if "thumbnail_id" in current:
        del current["thumbnail_id"]
        await db_exec(
            "INSERT INTO settings (user_id, data) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data",
            (user_id, json.dumps(current)),
            commit=True
        )

async def save_caption(user_id: int, caption: str):
    await update_user_setting(user_id, "caption", "Set")
    await update_user_setting(user_id, "custom_caption", caption)

async def get_caption(user_id: int):
    settings = await get_user_settings(user_id)
    return settings.get("custom_caption")

async def save_replacements(user_id: int, replace_map: dict):
    await update_user_setting(user_id, "replacements", replace_map)

async def get_replacements(user_id: int):
    settings = await get_user_settings(user_id)
    return settings.get("replacements", {})

# --- Stats ---
async def get_stats():
    total_users = 0
    active_logins = 0
    row_u = await db_exec("SELECT COUNT(*) FROM users", fetchone=True)
    if row_u:
        total_users = row_u[0]
    row_s = await db_exec("SELECT COUNT(*) FROM sessions", fetchone=True)
    if row_s:
        active_logins = row_s[0]

    return {
        "total_users": total_users,
        "active_logins": active_logins
    }
