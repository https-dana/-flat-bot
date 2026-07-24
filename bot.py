# -*- coding: utf-8 -*-
"""
Телеграм-бот для групи з пошуку квартири.

Що вміє:
  • ловить посилання в групових повідомленнях і зберігає їх;
  • якщо посилання вже кидали — попереджає і пропонує видалити повторку;
  • перевіряє, чи оголошення ще актуальні (/check + щоденна автоперевірка);
  • /list — список збережених посилань.

Налаштування — у файлі .env (див. .env.example).
"""

import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageEntity,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("flat-bot")

BASE_DIR = Path(__file__).resolve().parent
# DB_PATH задається змінною середовища на хостингу (щоб база жила на Volume)
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "links.db"))
KYIV = ZoneInfo("Europe/Kyiv")

# ---------------------------------------------------------------- .env

def load_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- база даних

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id          INTEGER PRIMARY KEY,
                chat_id     INTEGER NOT NULL,
                url_norm    TEXT    NOT NULL,
                url_orig    TEXT    NOT NULL,
                message_id  INTEGER,
                posted_by   TEXT,
                posted_at   TEXT,
                is_active   INTEGER DEFAULT 1,
                last_checked TEXT,
                UNIQUE (chat_id, url_norm)
            )
            """
        )


# ---------------------------------------------------------------- нормалізація URL

# параметри-трекери, які не впливають на те, ЩО це за оголошення
TRACKING_PARAMS = {
    "fbclid", "gclid", "yclid", "igshid", "mc_cid", "mc_eid",
    "ref", "referrer", "si", "share", "feature", "sourceFrom",
    "reason", "promoted", "sliding_type",
}
TRACKING_PREFIXES = ("utm_", "ga_", "hsa_")


def normalize_url(url: str) -> str:
    """Приводить посилання до канонічного вигляду, щоб ловити повторки
    навіть коли відрізняються utm-мітки, www, слеш у кінці тощо."""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    path = parts.path.rstrip("/")

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_pairs))

    return urlunsplit(("https", host, path, query, ""))


def extract_urls(message) -> list[str]:
    """Всі посилання з тексту або підпису повідомлення (без повторів)."""
    found: list[str] = []
    if message.entities:
        for ent, text in message.parse_entities(
            [MessageEntity.URL, MessageEntity.TEXT_LINK]
        ).items():
            found.append(ent.url if ent.type == MessageEntity.TEXT_LINK else text)
    if message.caption_entities:
        for ent, text in message.parse_caption_entities(
            [MessageEntity.URL, MessageEntity.TEXT_LINK]
        ).items():
            found.append(ent.url if ent.type == MessageEntity.TEXT_LINK else text)

    unique: list[str] = []
    seen: set[str] = set()
    for u in found:
        norm = normalize_url(u)
        if norm not in seen:
            seen.add(norm)
            unique.append(u)
    return unique


def message_link(chat_id: int, message_id: int) -> str | None:
    """Посилання на повідомлення (працює лише в супергрупах)."""
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None


# ---------------------------------------------------------------- обробка повідомлень

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    urls = extract_urls(message)
    if not urls:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    posted_by = user.first_name if user else "хтось"

    duplicates = []  # (url, рядок з БД)
    with db() as conn:
        for url in urls:
            norm = normalize_url(url)
            row = conn.execute(
                "SELECT * FROM links WHERE chat_id = ? AND url_norm = ?",
                (chat_id, norm),
            ).fetchone()
            if row:
                duplicates.append((url, row))
            else:
                conn.execute(
                    """INSERT INTO links
                       (chat_id, url_norm, url_orig, message_id, posted_by, posted_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        chat_id,
                        norm,
                        url,
                        message.message_id,
                        posted_by,
                        datetime.now(KYIV).isoformat(timespec="seconds"),
                    ),
                )

    if not duplicates:
        try:
            await message.set_reaction("👌")
        except Exception:
            pass  # реакції можуть бути вимкнені в групі — не страшно
        return

    lines = ["⚠️ <b>Це вже кидали!</b>"]
    for url, row in duplicates:
        when = ""
        if row["posted_at"]:
            try:
                when = " " + datetime.fromisoformat(row["posted_at"]).strftime("%d.%m")
            except ValueError:
                pass
        link = message_link(chat_id, row["message_id"]) if row["message_id"] else None
        origin = f'<a href="{link}">оригінал</a>' if link else "оригінал не знайшов"
        status = "" if row["is_active"] else " (до речі, воно вже неактуальне)"
        lines.append(
            f'• {html.escape(url)}\n  ↳ кидав(ла) <b>{html.escape(row["posted_by"] or "?")}</b>{when}, {origin}{status}'
        )

    # якщо в повідомленні лише повторки — пропонуємо його видалити
    only_duplicates = len(duplicates) == len(urls)
    keyboard = None
    if only_duplicates:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑 Видалити повторку", callback_data=f"rm:{message.message_id}"
                    ),
                    InlineKeyboardButton("✋ Залишити", callback_data="keep"),
                ]
            ]
        )
    else:
        lines.append("\nУ повідомленні є й нові посилання, тому не видаляю.")

    await message.reply_html(
        "\n".join(lines),
        reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    warning = query.message

    if query.data.startswith("rm:"):
        dup_message_id = int(query.data.split(":", 1)[1])
        try:
            await context.bot.delete_message(warning.chat_id, dup_message_id)
        except Exception as exc:
            await query.answer(
                "Не зміг видалити 😔 Перевір, чи я адмін з правом видаляти повідомлення.",
                show_alert=True,
            )
            log.warning("Не вдалося видалити повідомлення: %s", exc)
            return
    try:
        await warning.delete()
    except Exception:
        pass


# ---------------------------------------------------------------- перевірка актуальності

DEAD_MARKERS = [
    # olx.ua
    "оголошення більше не доступне",
    "объявление больше не доступно",
    "no longer available",
    # dim.ria / dom.ria
    "оголошення знято з публікації",
    "публікацію завершено",
    "объявление снято с публикации",
    # rieltor.ua та інші
    "оголошення деактивовано",
    "оголошення видалено",
    "оголошення не знайдено",
    # загальні
    "сторінку не знайдено",
    "страница не найдена",
    "page not found",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.5",
}


async def check_one(client: httpx.AsyncClient, url: str) -> tuple[bool, str]:
    """Повертає (чи живе, пояснення)."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return True, f"не зміг перевірити ({type(exc).__name__})"

    if resp.status_code in (404, 410):
        return False, f"HTTP {resp.status_code}"
    if resp.status_code >= 400:
        return True, f"не зміг перевірити (HTTP {resp.status_code})"

    body = resp.text[:200_000].lower()
    for marker in DEAD_MARKERS:
        if marker in body:
            return False, f"на сторінці написано «{marker}»"

    # редірект на головну — оголошення, найімовірніше, зникло
    original_path = urlsplit(url).path.strip("/")
    final_path = resp.url.path.strip("/")
    if original_path and not final_path:
        return False, "редірект на головну сторінку сайту"

    return True, "ok"


async def run_check(chat_id: int) -> str | None:
    """Перевіряє всі активні посилання чату. Повертає текст звіту
    або None, якщо перевіряти нічого."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM links WHERE chat_id = ? AND is_active = 1", (chat_id,)
        ).fetchall()
    if not rows:
        return None

    semaphore = asyncio.Semaphore(5)
    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20
    ) as client:

        async def guarded(row):
            async with semaphore:
                return row, await check_one(client, row["url_orig"])

        results = await asyncio.gather(*(guarded(r) for r in rows))

    now = datetime.now(KYIV).isoformat(timespec="seconds")
    dead = []
    with db() as conn:
        for row, (alive, reason) in results:
            conn.execute(
                "UPDATE links SET is_active = ?, last_checked = ? WHERE id = ?",
                (1 if alive else 0, now, row["id"]),
            )
            if not alive:
                dead.append((row, reason))

    report = [f"🔍 Перевірено посилань: {len(rows)}"]
    if dead:
        report.append(f"❌ Неактуальних: {len(dead)}\n")
        for row, reason in dead:
            report.append(
                f'• {html.escape(row["url_orig"])}\n'
                f'  ↳ кидав(ла) {html.escape(row["posted_by"] or "?")} — {reason}'
            )
        report.append("\nЦі посилання я позначив неактуальними.")
    else:
        report.append("✅ Всі оголошення ще актуальні!")
    return "\n".join(report)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    progress = await update.effective_message.reply_text("🔍 Перевіряю посилання…")
    report = await run_check(update.effective_chat.id)
    if report is None:
        await progress.edit_text("Поки що я не зберіг жодного посилання в цій групі.")
        return
    await progress.edit_text(
        report,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def daily_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Щоденна автоперевірка: пише в групу, лише якщо знайшлося щось неактуальне."""
    with db() as conn:
        chat_ids = [
            r["chat_id"]
            for r in conn.execute("SELECT DISTINCT chat_id FROM links").fetchall()
        ]
    for chat_id in chat_ids:
        report = await run_check(chat_id)
        if report and "❌" in report:
            try:
                await context.bot.send_message(
                    chat_id,
                    report,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception as exc:
                log.warning("Не вдалося написати в чат %s: %s", chat_id, exc)


# ---------------------------------------------------------------- інші команди

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM links WHERE chat_id = ? ORDER BY id",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(
            "Поки що я не зберіг жодного посилання в цій групі."
        )
        return

    active = [r for r in rows if r["is_active"]]
    lines = [f"📋 Збережено посилань: {len(rows)} (актуальних: {len(active)})\n"]
    for row in rows:
        mark = "✅" if row["is_active"] else "❌"
        lines.append(
            f'{mark} {html.escape(row["url_orig"])} — {html.escape(row["posted_by"] or "?")}'
        )
    text = "\n".join(lines)
    # телеграм не любить повідомлення понад 4096 символів
    for chunk_start in range(0, len(text), 4000):
        await update.effective_message.reply_text(
            text[chunk_start : chunk_start + 4000],
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


GREETING = (
    "Привіт! Я слідкуватиму за посиланнями на квартири 🏠\n\n"
    "• нове посилання тихенько відмічаю 👌\n"
    "• якщо посилання вже кидали — попереджу і запропоную видалити повторку\n"
    "• /check — перевірю, чи оголошення ще актуальні (і сам роблю це щодня о 10:00)\n"
    "• /list — покажу все збережене\n\n"
    "Щоб я бачив усі повідомлення і міг видаляти повторки, зробіть мене "
    "адміністратором з правом «Видалення повідомлень» 🙏"
)


async def on_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вітається, коли бота додають у групу."""
    message = update.effective_message
    if message and message.new_chat_members:
        if any(m.id == context.bot.id for m in message.new_chat_members):
            await message.reply_text(GREETING)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привіт! Я слідкую, щоб у групі не було повторних посилань на квартири 🏠\n\n"
        "Додай мене в групу як адміністратора (з правом видаляти повідомлення) — "
        "і просто кидайте посилання, як звикли.\n\n"
        "Команди:\n"
        "/check — перевірити, чи оголошення ще актуальні\n"
        "/list — список збережених посилань"
    )


# ---------------------------------------------------------------- запуск

def main() -> None:
    load_env()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Не знайдено BOT_TOKEN. Скопіюй .env.example у .env і встав туди токен від @BotFather."
        )

    init_db()

    async def post_init(app: Application) -> None:
        # кнопка меню «/» зі списком команд біля поля вводу
        await app.bot.set_my_commands(
            [
                BotCommand("check", "перевірити, чи оголошення ще актуальні"),
                BotCommand("list", "показати всі збережені посилання"),
            ]
        )

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_added_to_group)
    )
    app.add_handler(CommandHandler("check", cmd_check, filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("list", cmd_list, filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            on_group_message,
        )
    )

    if app.job_queue:
        app.job_queue.run_daily(daily_check, time=dtime(hour=10, minute=0, tzinfo=KYIV))
    else:
        log.warning(
            "JobQueue недоступний — щоденна автоперевірка вимкнена. "
            "Встанови python-telegram-bot[job-queue]."
        )

    log.info("Бот запущений. Ctrl+C — щоб зупинити.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
