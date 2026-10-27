from datetime import datetime, timezone
from pathlib import Path
import secrets

import aiosqlite
from datetime import timedelta


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, last_check_at TEXT, last_success_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS watched_cases (
                chat_id INTEGER NOT NULL, case_number TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, PRIMARY KEY(chat_id, case_number),
                FOREIGN KEY(chat_id) REFERENCES users(chat_id)
            );
            CREATE TABLE IF NOT EXISTS documents (
                chat_id INTEGER NOT NULL, case_number TEXT NOT NULL, document_key TEXT NOT NULL,
                registry_id TEXT, decision_date TEXT, document_type TEXT, title TEXT, url TEXT,
                first_seen_at TEXT NOT NULL, sent_at TEXT,
                PRIMARY KEY(chat_id, case_number, document_key)
            );
            """)
            try:
                await db.execute("ALTER TABLE users ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 60")
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE users ADD COLUMN next_check_at TEXT")
            except aiosqlite.OperationalError:
                pass
            for column in ("activation_expires_at TEXT", "activation_key TEXT"):
                try:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {column}")
                except aiosqlite.OperationalError:
                    pass
            await db.execute("""
                CREATE TABLE IF NOT EXISTS activation_keys (
                    key TEXT PRIMARY KEY, duration_minutes INTEGER, max_uses INTEGER NOT NULL DEFAULT 1,
                    used_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur = await db.execute("SELECT chat_id, created_at FROM users WHERE activation_expires_at IS NULL")
            for chat_id, created_at in await cur.fetchall():
                trial_until = (datetime.fromisoformat(created_at) + timedelta(hours=24)).isoformat(timespec="seconds")
                await db.execute("UPDATE users SET activation_expires_at=? WHERE chat_id=?", (trial_until, chat_id))
            await db.commit()

    async def activate_user(self, chat_id: int, default_case: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            timestamp = now()
            trial_until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
            await db.execute("INSERT INTO users(chat_id, created_at, next_check_at, activation_expires_at) VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET active=1", (chat_id, timestamp, timestamp, trial_until))
            await db.commit()

    async def remove_case_for_all(self, case_number: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM documents WHERE case_number=?", (case_number,))
            await db.execute("DELETE FROM watched_cases WHERE case_number=?", (case_number,))
            await db.commit()

    async def has_valid_activation(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT activation_expires_at FROM users WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            return bool(row and (row[0] is None or datetime.fromisoformat(row[0]) > datetime.now(timezone.utc)))

    async def activation_expiry(self, chat_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT activation_expires_at FROM users WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def activate_key(self, chat_id: int, key: str) -> tuple[bool, str]:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT duration_minutes, max_uses, used_count, revoked FROM activation_keys WHERE key=?", (key,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False, "Ключ не знайдено. Перевірте написання та спробуйте ще раз."
            duration_minutes, max_uses, used_count, revoked = row
            if revoked:
                await db.rollback()
                return False, "Цей ключ відкликано адміністратором."
            if used_count >= max_uses:
                await db.rollback()
                return False, "Ліміт використань цього ключа вже вичерпано."
            expires = None if duration_minutes is None else (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).isoformat(timespec="seconds")
            await db.execute("UPDATE users SET active=1, activation_expires_at=?, activation_key=? WHERE chat_id=?", (expires, key, chat_id))
            await db.execute("UPDATE activation_keys SET used_count=used_count+1 WHERE key=?", (key,))
            await db.commit()
            return True, "Ключ успішно активовано."

    async def create_activation_key(self, duration_minutes: int | None, max_uses: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            while True:
                key = "EDRSR-" + secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()
                cur = await db.execute("SELECT 1 FROM activation_keys WHERE key=?", (key,))
                if not await cur.fetchone():
                    break
            await db.execute("INSERT INTO activation_keys(key, duration_minutes, max_uses, created_at) VALUES (?, ?, ?, ?)", (key, duration_minutes, max_uses, now()))
            await db.commit()
            return key

    async def activation_keys(self) -> list[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT key, duration_minutes, max_uses, used_count, revoked FROM activation_keys ORDER BY created_at DESC LIMIT 50")
            return await cur.fetchall()

    async def extend_key(self, key: str, minutes: int) -> tuple[bool, str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT duration_minutes FROM activation_keys WHERE key=? AND revoked=0", (key,))
            row = await cur.fetchone()
            if not row:
                return False, "Активний ключ не знайдено."
            current_duration = row[0]
            if current_duration is None:
                return True, "Ключ уже безстроковий."
            await db.execute("UPDATE activation_keys SET duration_minutes=duration_minutes+? WHERE key=?", (minutes, key))
            cur = await db.execute("SELECT chat_id, activation_expires_at FROM users WHERE activation_key=?", (key,))
            for chat_id, expires_at in await cur.fetchall():
                if expires_at:
                    new_expiry = (datetime.fromisoformat(expires_at) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
                    await db.execute("UPDATE users SET activation_expires_at=?, active=1 WHERE chat_id=?", (new_expiry, chat_id))
            await db.commit()
            return True, "Ключ і пов’язаний доступ успішно продовжено."

    async def revoke_key(self, key: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("UPDATE activation_keys SET revoked=1 WHERE key=? AND revoked=0", (key,))
            await db.commit()
            return cur.rowcount > 0

    async def users_overview(self) -> list[tuple]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT chat_id, active, activation_expires_at, activation_key, interval_minutes FROM users ORDER BY created_at DESC LIMIT 100")
            return await cur.fetchall()

    async def extend_user(self, chat_id: int, minutes: int) -> tuple[bool, str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT activation_expires_at FROM users WHERE chat_id=?", (chat_id,))
            row = await cur.fetchone()
            if not row:
                return False, "Користувача не знайдено."
            base = datetime.now(timezone.utc) if not row[0] or datetime.fromisoformat(row[0]) < datetime.now(timezone.utc) else datetime.fromisoformat(row[0])
            expires = (base + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            await db.execute("UPDATE users SET active=1, activation_expires_at=? WHERE chat_id=?", (expires, chat_id))
            await db.commit()
            return True, "Доступ користувача успішно продовжено."

    async def expire_users(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            current = now()
            cur = await db.execute("SELECT chat_id FROM users WHERE active=1 AND activation_expires_at IS NOT NULL AND activation_expires_at<=?", (current,))
            chat_ids = [row[0] for row in await cur.fetchall()]
            if chat_ids:
                await db.executemany("UPDATE users SET active=0 WHERE chat_id=?", [(chat_id,) for chat_id in chat_ids])
                await db.commit()
            return chat_ids

    async def set_active(self, chat_id: int, active: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET active=? WHERE chat_id=?", (int(active), chat_id))
            await db.commit()

    async def set_interval(self, chat_id: int, minutes: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET interval_minutes=?, next_check_at=? WHERE chat_id=?", (minutes, now(), chat_id))
            await db.commit()

    async def claim_due_chats(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            timestamp = datetime.now(timezone.utc)
            current = timestamp.isoformat(timespec="seconds")
            cur = await db.execute(
                "SELECT chat_id, interval_minutes FROM users WHERE active=1 AND (activation_expires_at IS NULL OR activation_expires_at>?) AND (next_check_at IS NULL OR next_check_at<=?)",
                (current, current),
            )
            due = await cur.fetchall()
            for chat_id, interval_minutes in due:
                reserved_until = (timestamp + timedelta(minutes=interval_minutes or 60)).isoformat(timespec="seconds")
                await db.execute("UPDATE users SET next_check_at=? WHERE chat_id=?", (reserved_until, chat_id))
            await db.commit()
            return [row[0] for row in due]

    async def clear_all_cases(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM documents WHERE chat_id=?", (chat_id,))
            await db.execute("DELETE FROM watched_cases WHERE chat_id=?", (chat_id,))
            await db.commit()

    async def add_case(self, chat_id: int, case_number: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM watched_cases WHERE chat_id=? AND active=1", (chat_id,))
            if (await cur.fetchone())[0] >= 10:
                return False
            await db.execute("INSERT OR REPLACE INTO watched_cases(chat_id, case_number, active, created_at) VALUES (?, ?, 1, ?)", (chat_id, case_number, now()))
            await db.commit()
            return True

    async def remove_case(self, chat_id: int, case_number: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE watched_cases SET active=0 WHERE chat_id=? AND case_number=?", (chat_id, case_number))
            await db.commit()

    async def cases_for(self, chat_id: int) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT case_number FROM watched_cases WHERE chat_id=? AND active=1 ORDER BY case_number", (chat_id,))
            return [row[0] for row in await cur.fetchall()]

    async def active_chats(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT chat_id FROM users WHERE active=1")
            return [row[0] for row in await cur.fetchall()]

    async def all_chats(self) -> list[int]:
        """Return every registered chat for administrator broadcasts."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT chat_id FROM users ORDER BY created_at")
            return [row[0] for row in await cur.fetchall()]

    async def known_keys(self, chat_id: int, case_number: str) -> set[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT document_key FROM documents WHERE chat_id=? AND case_number=?", (chat_id, case_number))
            return {row[0] for row in await cur.fetchall()}

    async def save_documents(self, chat_id: int, documents: list) -> None:
        async with aiosqlite.connect(self.path) as db:
            for doc in documents:
                await db.execute("""INSERT OR IGNORE INTO documents
                    (chat_id, case_number, document_key, registry_id, decision_date, document_type, title, url, first_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (chat_id, doc.case_number, doc.document_key, doc.registry_id, doc.decision_date, doc.document_type, doc.title, doc.url, now()))
            await db.commit()

    async def record_result(self, chat_id: int, error: str | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            timestamp = now()
            await db.execute("UPDATE users SET last_check_at=?, last_success_at=CASE WHEN ? IS NULL THEN ? ELSE last_success_at END, last_error=? WHERE chat_id=?", (timestamp, error, timestamp, error, chat_id))
            await db.commit()

    async def status(self, chat_id: int) -> tuple | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT active, last_check_at, last_success_at, last_error, interval_minutes, next_check_at FROM users WHERE chat_id=?", (chat_id,))
            return await cur.fetchone()
