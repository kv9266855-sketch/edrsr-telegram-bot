"""Резервна копія SQLite у Telegram.

На безкоштовних хостингах (Render Free) диск стирається при кожному
перезапуску, тому база зберігається як закріплений документ у чаті
адміністратора і відновлюється з нього під час старту.
"""
import asyncio
import hashlib
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaDocument

log = logging.getLogger(__name__)
FILE_NAME = "edrsr_backup.sqlite3"
REQUIRED_TABLES = {"users", "watched_cases", "documents"}


def is_valid_db(path: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return REQUIRED_TABLES <= tables


def replace_db(source: Path, target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    source.replace(target)


def _snapshot(path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / FILE_NAME
        src = sqlite3.connect(path)
        out = sqlite3.connect(out_path)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        return out_path.read_bytes()


class TelegramBackup:
    def __init__(self, bot: Bot, chat_id: int, db_path: Path):
        self.bot = bot
        self.chat_id = chat_id
        self.db_path = db_path
        self.message_id: int | None = None
        self.last_hash: str | None = None
        self.lock = asyncio.Lock()

    async def restore(self) -> bool:
        """Відновлює базу із закріпленого повідомлення.

        Мережеві помилки не перехоплюються навмисно: краще не стартувати,
        ніж стартувати з порожньою базою і перезаписати нею копію.
        """
        me = await self.bot.get_me()
        chat = await self.bot.get_chat(self.chat_id)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or pinned.document.file_name != FILE_NAME \
                or not pinned.from_user or pinned.from_user.id != me.id:
            log.warning("Backup not found in chat %s, starting with local database", self.chat_id)
            return False
        tmp = self.db_path.with_suffix(".restore")
        await self.bot.download(pinned.document, destination=tmp)
        if not await asyncio.to_thread(is_valid_db, tmp):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Pinned backup is not a valid bot database")
        replace_db(tmp, self.db_path)
        self.message_id = pinned.message_id
        log.info("Database restored from Telegram backup (message %s)", self.message_id)
        return True

    async def save(self, force: bool = False) -> None:
        if not self.db_path.exists():
            return
        async with self.lock:
            data = await asyncio.to_thread(_snapshot, self.db_path)
            digest = hashlib.sha256(data).hexdigest()
            if digest == self.last_hash and not force:
                return
            stamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")
            caption = f"Резервна копія бази бота ({stamp}). Не відкріплюйте це повідомлення."
            file = BufferedInputFile(data, filename=FILE_NAME)
            if self.message_id:
                try:
                    await self.bot.edit_message_media(
                        media=InputMediaDocument(media=file, caption=caption),
                        chat_id=self.chat_id, message_id=self.message_id,
                    )
                except TelegramBadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        log.warning("Cannot edit backup message, sending a new one: %s", exc)
                        self.message_id = None
            if not self.message_id:
                message = await self.bot.send_document(self.chat_id, file, caption=caption, disable_notification=True)
                await self.bot.pin_chat_message(self.chat_id, message.message_id, disable_notification=True)
                self.message_id = message.message_id
            self.last_hash = digest

    async def save_safely(self) -> None:
        try:
            await self.save()
        except Exception:
            log.exception("Telegram backup failed")
