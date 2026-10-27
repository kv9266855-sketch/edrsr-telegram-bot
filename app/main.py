import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from aiohttp import ClientSession, ClientTimeout, web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from playwright.async_api import async_playwright

from .backup import TelegramBackup, is_valid_db, replace_db
from .config import Settings
from .db import Database
from .emoji import ADD, BROADCAST, CHECK, LINK, NOTIFY, OK, REMOVE, SEARCH, SETTINGS, STOP
from .reyestr import EDRSRClient, RegistryUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
router = Router()
KYIV_TZ = ZoneInfo("Europe/Kyiv")
ADMIN_ID = 7747658262
ADMIN_USERNAME = "edrsrAA_admin"
BROWSER_IDLE_SECONDS = 180


class MenuStates(StatesGroup):
    waiting_add_case = State()
    waiting_remove_case = State()
    waiting_interval = State()
    waiting_activation_key = State()
    waiting_admin_limit = State()
    waiting_admin_key = State()
    waiting_admin_user = State()
    waiting_broadcast = State()


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Перевірити зараз")],
        [KeyboardButton(text="🔔 Увімкнути"), KeyboardButton(text="⛔ Вимкнути")],
        [KeyboardButton(text="⚙️ Інтервал"), KeyboardButton(text="🧹 Очистити справи")],
        [KeyboardButton(text="➕ Додати справу"), KeyboardButton(text="🗑 Прибрати справу")],
        [KeyboardButton(text="👀 Акаунт")],
    ], resize_keyboard=True, is_persistent=True)


def activation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ввести ключ активації", callback_data="activation:enter")],
        [InlineKeyboardButton(text="Придбати ключ у адміністратора", url=f"https://t.me/{ADMIN_USERNAME}?text=Вітаю%21%20Хочу%20придбати%20ключ%20активації%20для%20бота.")],
    ])


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔑 Створити ключ"), KeyboardButton(text="📋 Список ключів")],
        [KeyboardButton(text="⏩ Продовжити ключ"), KeyboardButton(text="🚫 Відкликати ключ")],
        [KeyboardButton(text="👤 Продовжити доступ"), KeyboardButton(text="👀 Список користувачів")],
        [KeyboardButton(text="💬 Сповістити всіх користувачів")],
        [KeyboardButton(text="💬 Шаблон оплати"), KeyboardButton(text="💬 Шаблон ключа")],
        [KeyboardButton(text="💬 Шаблон продовження")],
    ], resize_keyboard=True, is_persistent=True)


async def send_activation_prompt(bot: Bot, chat_id: int) -> None:
    await bot.send_message(
        chat_id,
        "Доступ до моніторингу завершився.\n\n"
        "Щоб продовжити перевірку ЄДРСР, активуйте бота ключем або придбайте новий ключ у адміністратора.",
        reply_markup=activation_keyboard(),
    )


def normalize_case(value: str) -> str:
    return "".join(value.split())


def format_kyiv(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S %z (Київ)")
    except ValueError:
        return value


async def notify_document(bot: Bot, chat_id: int, doc) -> None:
    text = (f"{NOTIFY} <b>Нове судове рішення</b>\n\n"
            f"Справа: <code>{escape(doc.case_number)}</code>\n"
            f"Дата: <b>{escape(doc.decision_date)}</b>\n"
            f"Вид документа: <b>{escape(doc.document_type)}</b>\n"
            f"№ рішення: <code>{escape(doc.registry_id)}</code>\n\n"
            f"{LINK} <a href=\"{escape(doc.url, quote=True)}\">Переглянути рішення</a>")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Перейти до рішення", url=doc.url)
    ]])
    await bot.send_message(chat_id, text, reply_markup=keyboard)


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.playwright = None
        self.context = None
        self.client = None
        self.last_browser_use = 0.0
        self.backup: TelegramBackup | None = None

    async def start(self) -> None:
        await self.db.init()
        await self.db.remove_case_for_all(self.settings.default_case_number)

    async def ensure_client(self) -> None:
        self.last_browser_use = time.monotonic()
        if self.client is not None:
            return
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.settings.browser_data_dir),
                headless=self.settings.headless,
                # Render Free має 512 МБ RAM і маленький /dev/shm
                args=["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
            )
            self.client = EDRSRClient(self.context, self.settings.timeout_ms)
        except Exception as exc:
            await self.playwright.stop()
            self.playwright = None
            raise RegistryUnavailable("Не вдалося запустити браузер ЄДРСР на хостингу") from exc

    async def close_browser_if_idle(self) -> None:
        """Закриває Chromium між перевірками, щоб не тримати пам'ять."""
        if self.client is None or self.client.lock.locked():
            return
        if time.monotonic() - self.last_browser_use < BROWSER_IDLE_SECONDS:
            return
        self.client = None
        await self.close()
        self.context = None
        self.playwright = None
        log.info("Browser closed after idle period")

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

    async def check_chat(self, bot: Bot, chat_id: int, send_errors: bool = False) -> tuple[int, bool, bool]:
        count = 0
        had_success = False
        had_error = False
        for case_number in await self.db.cases_for(chat_id):
            try:
                await self.ensure_client()
                docs = await self.client.search(case_number)
                self.last_browser_use = time.monotonic()
                had_success = True
                known = await self.db.known_keys(chat_id, case_number)
                new_docs = [doc for doc in docs if doc.document_key not in known]
                await self.db.save_documents(chat_id, docs)
                await self.db.record_result(chat_id, None)
                for doc in sorted(new_docs, key=lambda item: item.decision_date):
                    await notify_document(bot, chat_id, doc)
                    count += 1
            except RegistryUnavailable as exc:
                had_error = True
                log.warning("Registry unavailable for %s: %s", case_number, exc)
                await self.db.record_result(chat_id, str(exc))
                if send_errors:
                    await bot.send_message(chat_id, f"{SEARCH} Не вдалося перевірити <code>{escape(case_number)}</code>.\nПричина: {escape(str(exc))}\n\nСпробуйте пізніше — помилку не буде трактовано як відсутність рішень.", reply_markup=menu_keyboard())
        return count, had_success, had_error

    async def scheduled_check(self, bot: Bot) -> None:
        for chat_id in await self.db.expire_users():
            await send_activation_prompt(bot, chat_id)
        for chat_id in await self.db.claim_due_chats():
            try:
                count, had_success, had_error = await self.check_chat(bot, chat_id)
                if had_success and not had_error:
                    if count:
                        await bot.send_message(
                            chat_id,
                            f"{CHECK} <b>Планову перевірку завершено.</b>\n\n"
                            f"Знайдено нових рішень: <b>{count}</b>. "
                            "Перегляньте їх за посиланнями вище.",
                            reply_markup=menu_keyboard(),
                        )
                    else:
                        await bot.send_message(
                            chat_id,
                            f"{SEARCH} <b>Планову перевірку завершено.</b>\n\n"
                            "Нових судових рішень не знайдено. Наступна перевірка відбудеться за вашим інтервалом.",
                            reply_markup=menu_keyboard(),
                        )
            except Exception:
                log.exception("Unexpected scheduled check failure for chat %s", chat_id)


def build_handlers(app: App):
    @router.message(Command("start"))
    async def start(message: Message):
        await app.db.activate_user(message.chat.id, app.settings.default_case_number)
        if await app.db.has_valid_activation(message.chat.id):
            await message.answer(f"{OK} <b>Ви успішно підключили моніторинг.</b>\n\nПробний доступ активний 24 години. Додайте справу через кнопку «Додати справу» або одразу придбайте ключ активації в акаунті.", reply_markup=menu_keyboard())
        else:
            await send_activation_prompt(message.bot, message.chat.id)

    @router.callback_query(F.data == "activation:enter")
    async def activation_enter(callback, state: FSMContext):
        await state.set_state(MenuStates.waiting_activation_key)
        await callback.answer()
        await callback.message.answer("Введіть ключ активації одним повідомленням.")

    @router.message(MenuStates.waiting_activation_key)
    async def activation_submit(message: Message, state: FSMContext):
        await app.db.activate_user(message.chat.id, app.settings.default_case_number)
        success, result = await app.db.activate_key(message.chat.id, (message.text or "").strip().upper())
        if success:
            await state.clear()
            await message.answer(f"{OK} <b>{result}</b>\n\nМоніторинг знову доступний.", reply_markup=menu_keyboard())
        else:
            await message.answer(f"{STOP} {result}", reply_markup=activation_keyboard())

    @router.message(Command("activate"))
    async def activate_command(message: Message, command: CommandObject, state: FSMContext):
        await app.db.activate_user(message.chat.id, app.settings.default_case_number)
        success, result = await app.db.activate_key(message.chat.id, (command.args or "").strip().upper())
        if success:
            await state.clear()
            await message.answer(f"{OK} <b>{result}</b>", reply_markup=menu_keyboard())
        else:
            await message.answer(f"{STOP} {result}", reply_markup=activation_keyboard())

    @router.message(Command("check"))
    async def check(message: Message):
        if not await app.db.has_valid_activation(message.chat.id):
            await send_activation_prompt(message.bot, message.chat.id)
            return
        await message.answer(f"{CHECK} <b>Ви успішно запустили перевірку ЄДРСР.</b> Перевіряю…", reply_markup=menu_keyboard())
        count, had_success, had_error = await app.check_chat(message.bot, message.chat.id, send_errors=True)
        if had_success and not had_error and count == 0:
            await message.answer(f"{SEARCH} <b>Ви успішно виконали перевірку.</b>\n\nНа Ваш запит не знайдено жодного документа.\nСпробуйте змінити пошукові параметри.", reply_markup=menu_keyboard())

    @router.message(Command("status"))
    async def status(message: Message):
        row = await app.db.status(message.chat.id)
        cases = await app.db.cases_for(message.chat.id)
        if not row:
            await message.answer(f"{SETTINGS} Спершу виконайте /start.", reply_markup=menu_keyboard())
            return
        active, last_check, last_success, last_error, interval_minutes, next_check_at = row
        state = "увімкнено" if active else "вимкнено"
        error = f"\nПомилка: <code>{escape(last_error)}</code>" if last_error else ""
        await message.answer(f"{SETTINGS} <b>Ви успішно відкрили статус моніторингу.</b>\n\nСтан: <b>{state}</b>\nСправи: <code>{escape(', '.join(cases) or '—')}</code>\nІнтервал: <b>{interval_minutes} хв</b>\nНаступна перевірка: <code>{escape(format_kyiv(next_check_at))}</code>\nОстання спроба: <code>{escape(format_kyiv(last_check))}</code>\nОстання успішна перевірка: <code>{escape(format_kyiv(last_success))}</code>{error}", reply_markup=menu_keyboard())

    @router.message(F.text == "👀 Акаунт")
    async def account(message: Message):
        row = await app.db.status(message.chat.id)
        cases = await app.db.cases_for(message.chat.id)
        if not row:
            await message.answer("Спершу натисніть /start.", reply_markup=menu_keyboard())
            return
        active, _, _, _, interval_minutes, next_check_at = row
        valid = await app.db.has_valid_activation(message.chat.id)
        activation_expiry = await app.db.activation_expiry(message.chat.id)
        await message.answer(
            f"{SETTINGS} <b>Ваш акаунт</b>\n\n"
            f"Статус: <b>{'активний' if valid and active else 'потрібна активація'}</b>\n"
            f"Доступ до: <b>{escape(format_kyiv(activation_expiry))}</b>\n"
            f"Справи: <b>{len(cases)}/10</b>\n"
            f"Інтервал: <b>{interval_minutes} хв</b>\n"
            f"Наступна перевірка: <code>{escape(format_kyiv(next_check_at))}</code>\n\n"
            "Можна придбати ключ одразу, не чекаючи завершення пробного доступу.",
            reply_markup=activation_keyboard(),
        )

    @router.message(Command("stop"))
    async def stop(message: Message):
        await app.db.set_active(message.chat.id, False)
        await message.answer(f"{STOP} <b>Ви успішно вимкнули сповіщення.</b> Натисніть «Увімкнути», щоб продовжити моніторинг.", reply_markup=menu_keyboard())

    @router.message(Command("add"))
    async def add(message: Message, command: CommandObject):
        case_number = normalize_case(command.args or "")
        if not case_number:
            await message.answer(f"{ADD} Формат: <code>/add 761/29150/26</code>", reply_markup=menu_keyboard())
            return
        await app.db.activate_user(message.chat.id, app.settings.default_case_number)
        if not await app.db.add_case(message.chat.id, case_number):
            await message.answer(f"{STOP} До одного акаунта можна додати максимум 10 справ.", reply_markup=menu_keyboard())
            return
        await message.answer(f"{ADD} <b>Ви успішно додали справу</b> <code>{escape(case_number)}</code> до моніторингу.", reply_markup=menu_keyboard())

    @router.message(Command("remove"))
    async def remove(message: Message, command: CommandObject):
        case_number = normalize_case(command.args or "")
        if not case_number:
            await message.answer(f"{REMOVE} Формат: <code>/remove 761/29150/26</code>", reply_markup=menu_keyboard())
            return
        await app.db.remove_case(message.chat.id, case_number)
        await message.answer(f"{REMOVE} <b>Ви успішно прибрали справу</b> <code>{escape(case_number)}</code> з моніторингу.", reply_markup=menu_keyboard())

    @router.message(Command("admin"))
    async def admin(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        await message.answer("Адмін-панель активацій.", reply_markup=admin_keyboard())

    @router.message(F.document, F.caption.startswith("/restore"))
    async def admin_restore(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        upload = app.settings.database_path.with_suffix(".upload")
        await message.bot.download(message.document, destination=upload)
        if not await asyncio.to_thread(is_valid_db, upload):
            upload.unlink(missing_ok=True)
            await message.answer(f"{STOP} Це не схоже на базу бота (.sqlite3). Нічого не змінено.", reply_markup=admin_keyboard())
            return
        replace_db(upload, app.settings.database_path)
        await app.db.init()
        if app.backup:
            await app.backup.save(force=True)
        await message.answer(f"{OK} Базу успішно відновлено з файлу.", reply_markup=admin_keyboard())

    @router.message(F.text == "🔑 Створити ключ")
    async def admin_create_prompt(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="24 години", callback_data="admin_duration:1440")],
            [InlineKeyboardButton(text="1 тиждень", callback_data="admin_duration:10080")],
            [InlineKeyboardButton(text="1 місяць", callback_data="admin_duration:43200")],
            [InlineKeyboardButton(text="Безстроково", callback_data="admin_duration:forever")],
        ])
        await message.answer("Оберіть термін дії ключа:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("admin_duration:"))
    async def admin_duration(callback, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer()
            return
        value = callback.data.split(":", 1)[1]
        await state.update_data(duration_minutes=None if value == "forever" else int(value))
        await state.set_state(MenuStates.waiting_admin_limit)
        await callback.answer()
        await callback.message.answer("Введіть ліміт активацій ключа одним числом. Наприклад: 1, 5 або 100.")

    @router.message(MenuStates.waiting_admin_limit)
    async def admin_limit(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        raw_limit = (message.text or "").strip()
        if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 10000:
            await message.answer("Введіть ціле число від 1 до 10000.")
            return
        data = await state.get_data()
        key = await app.db.create_activation_key(data.get("duration_minutes"), int(raw_limit))
        await state.clear()
        term = "безстроковий" if data.get("duration_minutes") is None else f"на {data['duration_minutes']} хвилин"
        await message.answer(f"Ключ створено успішно.\n\n<code>{key}</code>\nТермін: {term}\nЛіміт активацій: {raw_limit}", reply_markup=admin_keyboard())

    @router.message(F.text == "📋 Список ключів")
    async def admin_keys(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        rows = await app.db.activation_keys()
        if not rows:
            await message.answer("Створених ключів ще немає.", reply_markup=admin_keyboard())
            return
        lines = []
        for key, duration, max_uses, used, revoked in rows:
            term = "безстроковий" if duration is None else f"{duration} хв"
            state = "відкликаний" if revoked else "активний"
            lines.append(f"<code>{key}</code>\n{term}, використано {used}/{max_uses}, {state}")
        await message.answer("Список ключів:\n\n" + "\n\n".join(lines), reply_markup=admin_keyboard())

    @router.message(F.text == "⏩ Продовжити ключ")
    async def admin_extend_prompt(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.set_state(MenuStates.waiting_admin_key)
        await state.update_data(admin_action="extend")
        await message.answer("Надішліть ключ, який потрібно продовжити.")

    @router.message(F.text == "🚫 Відкликати ключ")
    async def admin_revoke_prompt(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.set_state(MenuStates.waiting_admin_key)
        await state.update_data(admin_action="revoke")
        await message.answer("Надішліть ключ, який потрібно відкликати.")

    @router.message(MenuStates.waiting_admin_key)
    async def admin_key_action(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        key = (message.text or "").strip().upper()
        data = await state.get_data()
        if data.get("admin_action") == "revoke":
            success = await app.db.revoke_key(key)
            await state.clear()
            await message.answer("Ключ успішно відкликано." if success else "Активний ключ не знайдено.", reply_markup=admin_keyboard())
            return
        await state.update_data(admin_key=key)
        await state.set_state(MenuStates.waiting_admin_limit)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="+1 день", callback_data="admin_extend:1440")],
            [InlineKeyboardButton(text="+1 тиждень", callback_data="admin_extend:10080")],
            [InlineKeyboardButton(text="+1 місяць", callback_data="admin_extend:43200")],
            [InlineKeyboardButton(text="+1 рік", callback_data="admin_extend:525600")],
        ])
        await message.answer("Оберіть, на який термін продовжити ключ:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("admin_extend:"))
    async def admin_extend_callback(callback, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer()
            return
        minutes = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        success, result = await app.db.extend_key(data.get("admin_key", ""), minutes)
        await state.clear()
        await callback.answer()
        await callback.message.answer(result, reply_markup=admin_keyboard())

    @router.message(F.text == "👤 Продовжити доступ")
    async def admin_extend_user_prompt(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.set_state(MenuStates.waiting_admin_user)
        await message.answer("Надішліть Telegram ID користувача, якому потрібно продовжити доступ.")

    @router.message(MenuStates.waiting_admin_user)
    async def admin_user_prompt(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        value = (message.text or "").strip()
        if not value.isdigit():
            await message.answer("Надішліть коректний числовий Telegram ID.")
            return
        await state.update_data(admin_user_id=int(value))
        await state.set_state(MenuStates.waiting_admin_limit)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="+1 день", callback_data="admin_user_extend:1440")],
            [InlineKeyboardButton(text="+1 тиждень", callback_data="admin_user_extend:10080")],
            [InlineKeyboardButton(text="+1 місяць", callback_data="admin_user_extend:43200")],
            [InlineKeyboardButton(text="+1 рік", callback_data="admin_user_extend:525600")],
        ])
        await message.answer("Оберіть термін продовження доступу:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("admin_user_extend:"))
    async def admin_user_extend_callback(callback, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer()
            return
        minutes = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        success, result = await app.db.extend_user(int(data.get("admin_user_id")), minutes)
        await state.clear()
        await callback.answer()
        await callback.message.answer(result, reply_markup=admin_keyboard())

    @router.message(F.text == "👀 Список користувачів")
    async def admin_users(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        rows = await app.db.users_overview()
        if not rows:
            await message.answer("Користувачів ще немає.", reply_markup=admin_keyboard())
            return
        lines = []
        for chat_id, active, expires, key, interval in rows:
            lines.append(f"<code>{chat_id}</code> — {'активний' if active else 'вимкнений'}, до {format_kyiv(expires)}, інтервал {interval} хв")
        await message.answer("Користувачі:\n\n" + "\n".join(lines), reply_markup=admin_keyboard())

    @router.message(F.text == "💬 Сповістити всіх користувачів")
    async def admin_broadcast_prompt(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        await state.set_state(MenuStates.waiting_broadcast)
        cancel = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✖️ Скасувати", callback_data="admin_broadcast:cancel")
        ]])
        await message.answer(
            f"{BROADCAST} <b>Ви успішно відкрили розсилку.</b>\n\n"
            "Надішліть одним повідомленням текст, який потрібно відправити всім користувачам.",
            reply_markup=cancel,
        )

    @router.callback_query(F.data == "admin_broadcast:cancel")
    async def admin_broadcast_cancel(callback, state: FSMContext):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer()
            return
        await state.clear()
        await callback.answer("Розсилку скасовано")
        await callback.message.answer(
            f"{STOP} <b>Ви успішно скасували розсилку.</b>",
            reply_markup=admin_keyboard(),
        )

    @router.message(MenuStates.waiting_broadcast)
    async def admin_broadcast_send(message: Message, state: FSMContext):
        if message.from_user.id != ADMIN_ID:
            return
        raw_text = (message.text or message.caption or "").strip()
        if not raw_text:
            await message.answer(
                f"{STOP} Надішліть текстове повідомлення для розсилки або натисніть «Скасувати».",
            )
            return

        await state.clear()
        await message.answer(f"{BROADCAST} <b>Розсилку запущено.</b> Надсилаю повідомлення користувачам…")
        text = f"{BROADCAST} <b>Повідомлення від адміністратора</b>\n\n{escape(raw_text)}"
        sent = 0
        failed = 0
        for chat_id in await app.db.all_chats():
            if chat_id == ADMIN_ID:
                continue
            try:
                await message.bot.send_message(chat_id, text, reply_markup=menu_keyboard())
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
                log.warning("Broadcast delivery failed for chat %s", chat_id, exc_info=True)
        await message.answer(
            f"{OK} <b>Ви успішно завершили розсилку.</b>\n\n"
            f"Доставлено: <b>{sent}</b>\n"
            f"Не доставлено: <b>{failed}</b>",
            reply_markup=admin_keyboard(),
        )

    @router.message(F.text == "💬 Шаблон оплати")
    async def admin_template_payment(message: Message):
        if message.from_user.id == ADMIN_ID:
            await message.answer("Вітаю! Вартість ключа активації становить: ___ грн. Після оплати я надішлю вам персональний ключ. Напишіть, будь ласка, зручний для вас термін: тиждень, місяць або безстроковий доступ.", reply_markup=admin_keyboard())

    @router.message(F.text == "💬 Шаблон ключа")
    async def admin_template_key(message: Message):
        if message.from_user.id == ADMIN_ID:
            await message.answer("Оплату отримано, дякую! Ваш ключ активації: <code>ВСТАВТЕ_КЛЮЧ_ТУТ</code>\n\nНатисніть у боті «Акаунт» → «Ввести ключ активації» та надішліть цей ключ.", reply_markup=admin_keyboard())

    @router.message(F.text == "💬 Шаблон продовження")
    async def admin_template_extend(message: Message):
        if message.from_user.id == ADMIN_ID:
            await message.answer("Ваш доступ успішно продовжено. Дякую за оплату! Нову дату завершення можна переглянути в розділі «Акаунт».", reply_markup=admin_keyboard())

    @router.message(F.text == "🔍 Перевірити зараз")
    async def menu_check(message: Message):
        await check(message)

    @router.message(F.text == "Статус")
    async def menu_status(message: Message):
        await status(message)

    @router.message(F.text == "🔔 Увімкнути")
    async def menu_start(message: Message):
        try:
            if not await app.db.has_valid_activation(message.chat.id):
                await send_activation_prompt(message.bot, message.chat.id)
                return
            await app.db.activate_user(message.chat.id, app.settings.default_case_number)
            await message.answer(f"{OK} <b>Ви успішно увімкнули моніторинг.</b>", reply_markup=menu_keyboard())
        except Exception:
            log.exception("Failed to enable monitoring for chat %s", message.chat.id)
            await message.answer(f"{STOP} Не вдалося увімкнути моніторинг. Спробуйте ще раз.", reply_markup=menu_keyboard())

    @router.message(F.text == "⛔ Вимкнути")
    async def menu_stop(message: Message):
        await stop(message)

    @router.message(F.text == "⚙️ Інтервал")
    async def menu_interval(message: Message):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="30 хвилин", callback_data="interval:30"), InlineKeyboardButton(text="1 година", callback_data="interval:60")],
            [InlineKeyboardButton(text="2 години", callback_data="interval:120")],
            [InlineKeyboardButton(text="Ввести свої хвилини", callback_data="interval:custom")],
        ])
        await message.answer(f"{SETTINGS} <b>Ви успішно відкрили налаштування інтервалу.</b>\n\nОберіть, як часто перевіряти ЄДРСР:", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("interval:"))
    async def set_interval(callback, state: FSMContext):
        value = callback.data.split(":", 1)[1]
        if value == "custom":
            await state.set_state(MenuStates.waiting_interval)
            await callback.answer()
            await callback.message.answer(
                f"{SETTINGS} <b>Введіть власний інтервал.</b>\n\n"
                "Надішліть кількість хвилин одним числом від 1 до 10080.",
                reply_markup=menu_keyboard(),
            )
            return
        minutes = int(value)
        try:
            await app.db.set_interval(callback.from_user.id, minutes)
            await callback.message.edit_text(f"{OK} <b>Ви успішно змінили інтервал перевірки.</b>\n\nЄДРСР перевірятиметься кожні <b>{minutes} хвилин</b>.")
            await callback.message.answer(f"{OK} Клавіатуру оновлено. Наступна перевірка буде через <b>{minutes} хвилин</b>.", reply_markup=menu_keyboard())
            await callback.answer("Інтервал змінено")
        except Exception:
            log.exception("Failed to set interval for user %s", callback.from_user.id)
            await callback.answer("Не вдалося змінити інтервал", show_alert=True)
            if callback.message:
                await callback.message.answer(f"{STOP} Не вдалося змінити інтервал. Спробуйте ще раз.", reply_markup=menu_keyboard())

    @router.message(MenuStates.waiting_interval)
    async def custom_interval(message: Message, state: FSMContext):
        raw_value = (message.text or "").strip()
        if not raw_value.isdigit() or not 1 <= int(raw_value) <= 10080:
            await message.answer(
                f"{STOP} Введіть ціле число від 1 до 10080 хвилин.",
                reply_markup=menu_keyboard(),
            )
            return
        minutes = int(raw_value)
        try:
            await app.db.set_interval(message.chat.id, minutes)
            await state.clear()
            await message.answer(
                f"{OK} <b>Ви успішно встановили власний інтервал.</b>\n\n"
                f"ЄДРСР перевірятиметься кожні <b>{minutes} хвилин</b>.",
                reply_markup=menu_keyboard(),
            )
        except Exception:
            log.exception("Failed to set custom interval for user %s", message.chat.id)
            await message.answer(f"{STOP} Не вдалося зберегти інтервал. Спробуйте ще раз.", reply_markup=menu_keyboard())

    @router.message(F.text == "🧹 Очистити справи")
    async def clear_cases(message: Message):
        try:
            await app.db.clear_all_cases(message.chat.id)
            await message.answer(f"{OK} <b>Ви успішно очистили всі справи та збережені рішення.</b>\n\nДодайте нову справу кнопкою «Додати справу».", reply_markup=menu_keyboard())
        except Exception:
            log.exception("Failed to clear cases")
            await message.answer(f"{STOP} Не вдалося очистити справи. Спробуйте ще раз.", reply_markup=menu_keyboard())

    @router.message(F.text == "➕ Додати справу")
    async def menu_add_prompt(message: Message, state: FSMContext):
        await state.set_state(MenuStates.waiting_add_case)
        await message.answer(f"{ADD} <b>Ви успішно відкрили додавання справи.</b>\n\nНадішліть номер справи, наприклад <code>761/29150/26</code>.", reply_markup=menu_keyboard())

    @router.message(MenuStates.waiting_add_case)
    async def menu_add_case(message: Message, state: FSMContext):
        case_number = normalize_case(message.text or "")
        await state.clear()
        if not case_number:
            await message.answer(f"{ADD} Номер справи не розпізнано.", reply_markup=menu_keyboard())
            return
        await app.db.activate_user(message.chat.id, app.settings.default_case_number)
        if not await app.db.add_case(message.chat.id, case_number):
            await message.answer(f"{STOP} До одного акаунта можна додати максимум 10 справ.", reply_markup=menu_keyboard())
            return
        await message.answer(f"{ADD} <b>Ви успішно додали справу</b> <code>{escape(case_number)}</code> до моніторингу.", reply_markup=menu_keyboard())

    @router.message(F.text == "🗑 Прибрати справу")
    async def menu_remove_prompt(message: Message, state: FSMContext):
        await state.set_state(MenuStates.waiting_remove_case)
        await message.answer(f"{REMOVE} <b>Ви успішно відкрили видалення справи.</b>\n\nНадішліть номер справи, яку прибрати.", reply_markup=menu_keyboard())

    @router.message(MenuStates.waiting_remove_case)
    async def menu_remove_case(message: Message, state: FSMContext):
        case_number = normalize_case(message.text or "")
        await state.clear()
        await app.db.remove_case(message.chat.id, case_number)
        await message.answer(f"{REMOVE} <b>Ви успішно прибрали справу</b> <code>{escape(case_number)}</code> з моніторингу.", reply_markup=menu_keyboard())


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_health_server() -> web.AppRunner | None:
    """Render Web Service вимагає відкритий порт; без нього деплой не завершиться."""
    port = os.getenv("PORT")
    if not port:
        return None
    server = web.Application()
    server.router.add_get("/", health)
    server.router.add_get("/health", health)
    runner = web.AppRunner(server, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server listening on port %s", port)
    return runner


async def keep_awake(url: str) -> None:
    """Render Free засинає без вхідних запитів 15 хвилин, тому сервіс пінгує сам себе."""
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as session:
            async with session.get(url) as response:
                await response.read()
    except Exception as exc:
        log.warning("Self-ping failed: %s", exc)


async def run() -> None:
    settings = Settings.from_env()
    app = App(settings)
    health_runner = await start_health_server()
    telegram_session = AiohttpSession()
    if os.getenv("TELEGRAM_VERIFY_SSL", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        telegram_session._connector_init["ssl"] = False
    bot = Bot(settings.bot_token, session=telegram_session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    backup_chat = os.getenv("BACKUP_CHAT_ID", "").strip()
    if os.getenv("TELEGRAM_BACKUP", "true").strip().lower() in {"1", "true", "yes", "on"}:
        app.backup = TelegramBackup(bot, int(backup_chat) if backup_chat else ADMIN_ID, settings.database_path)
        await app.backup.restore()
    await app.start()
    dp = Dispatcher()
    build_handlers(app)
    dp.include_router(router)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(app.scheduled_check, "interval", minutes=1, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(app.close_browser_if_idle, "interval", minutes=1, max_instances=1, coalesce=True)
    if app.backup:
        scheduler.add_job(app.backup.save_safely, "interval", minutes=2, max_instances=1, coalesce=True)
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if health_runner and external_url:
        scheduler.add_job(keep_awake, "interval", minutes=10, args=[external_url.rstrip("/") + "/health"],
                          max_instances=1, coalesce=True)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if app.backup:
            await app.backup.save_safely()
        await app.close()
        if health_runner:
            await health_runner.cleanup()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
