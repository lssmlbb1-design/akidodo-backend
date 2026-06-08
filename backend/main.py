from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DB_PATH = BASE_DIR / "akidodo_posts.db"

load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / ".env.local")

OWNER_BOT_TOKEN  = os.getenv("TELEGRAM_OWNER_BOT_TOKEN", "").strip()
PUBLIC_BOT_TOKEN = os.getenv("TELEGRAM_PUBLIC_BOT_TOKEN", "").strip()
PUBLIC_BOT_USERNAME = os.getenv("PUBLIC_BOT_USERNAME", "").strip()
ADMIN_SECRET     = os.getenv("ADMIN_SECRET", "").strip()
SITE_PUBLIC_URL  = os.getenv("SITE_PUBLIC_URL", "http://localhost:3000").strip()

# Поддержка нескольких владельцев: "8449696490,1480121001" → ['8449696490', '1480121001']
_raw_owner_ids = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
OWNER_CHAT_IDS: list[str] = [cid.strip() for cid in _raw_owner_ids.split(",") if cid.strip()]
OWNER_CHAT_ID = OWNER_CHAT_IDS[0] if OWNER_CHAT_IDS else ""  # основной ID (для отправки)

LINK_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


app = FastAPI(title="AkiDoDo Public Board API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                telegram_message_id INTEGER UNIQUE,
                title TEXT,
                content TEXT NOT NULL,
                links TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'owner_bot',
                is_published INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def get_setting(key: str, default: str = "0") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def extract_post_text(message: dict[str, Any]) -> str:
    return (message.get("text") or message.get("caption") or "").strip()

def extract_links(content: str) -> list[str]:
    return [link.rstrip(").,;") for link in LINK_RE.findall(content)]

def title_from(content: str) -> str:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if len(first_line) > 90:
        return f"{first_line[:87]}..."
    return first_line or "Новая заметка"

def post_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "telegram_message_id": row["telegram_message_id"],
        "title": row["title"],
        "content": row["content"],
        "links": json.loads(row["links"] or "[]"),
        "source": row["source"],
        "is_published": bool(row["is_published"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_post(content: str, telegram_message_id: int | None = None) -> dict[str, Any]:
    post_id = new_id()
    created = now()
    links = extract_links(content)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO posts
            (id, telegram_message_id, title, content, links, source, is_published, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id, telegram_message_id, title_from(content), content,
                json.dumps(links, ensure_ascii=False), "owner_bot", 1, created, created,
            ),
        )
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return post_from_row(row)


def update_post_by_message(telegram_message_id: int, content: str) -> dict[str, Any] | None:
    links = extract_links(content)
    with connect() as conn:
        conn.execute(
            """
            UPDATE posts
            SET title = ?, content = ?, links = ?, updated_at = ?
            WHERE telegram_message_id = ?
            """,
            (title_from(content), content, json.dumps(links, ensure_ascii=False), now(), telegram_message_id),
        )
        row = conn.execute(
            "SELECT * FROM posts WHERE telegram_message_id = ?", (telegram_message_id,)
        ).fetchone()
    return post_from_row(row) if row else None


def delete_post(post_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    return cursor.rowcount > 0


async def telegram_api(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
        )
        response.raise_for_status()
        return response.json()


async def send_owner_message(text: str) -> None:
    """Пересылает сообщение читателей ВСЕМ владельцам из списка."""
    if not OWNER_BOT_TOKEN or not OWNER_CHAT_IDS:
        return
    for chat_id in OWNER_CHAT_IDS:
        try:
            await telegram_api(
                OWNER_BOT_TOKEN,
                "sendMessage",
                {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        except Exception as exc:
            print(f"send_owner_message → {chat_id} failed: {exc}")


async def answer(token: str, chat_id: int | str, text: str) -> None:
    await telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def is_owner(message: dict[str, Any]) -> bool:
    """Проверяет что отправитель — один из владельцев (поддерживает несколько ID)."""
    sender = message.get("from") or {}
    sender_id = str(sender.get("id", ""))
    return bool(sender_id and sender_id in OWNER_CHAT_IDS)


async def handle_owner_message(message: dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    text = extract_post_text(message)

    if not is_owner(message):
        await answer(OWNER_BOT_TOKEN, chat_id, "Этот бот принимает публикации только от владельца.")
        return

    if text.startswith("/start"):
        await answer(
            OWNER_BOT_TOKEN, chat_id,
            "Отправь мне текст заметки, и я опубликую её на сайте.\n"
            "Команды:\n"
            "/list — последние посты\n"
            "/delete ID — удалить пост с сайта",
        )
        return

    if text.startswith("/list"):
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title FROM posts WHERE is_published = 1 ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        if not rows:
            await answer(OWNER_BOT_TOKEN, chat_id, "Пока нет опубликованных заметок.")
            return
        lines = [f"{row['id']}\n  {row['title']}" for row in rows]
        await answer(OWNER_BOT_TOKEN, chat_id, "\n\n".join(lines))
        return

    if text.startswith("/delete"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await answer(OWNER_BOT_TOKEN, chat_id, "Формат: /delete ID")
            return
        removed = delete_post(parts[1].strip())
        await answer(OWNER_BOT_TOKEN, chat_id, "Удалено с сайта." if removed else "Пост не найден.")
        return

    if not text:
        await answer(OWNER_BOT_TOKEN, chat_id, "Пока поддерживаются текстовые заметки и подписи к медиа.")
        return

    post = create_post(text, message.get("message_id"))
    await answer(
        OWNER_BOT_TOKEN, chat_id,
        f"Опубликовано на сайте.\nID: {post['id']}\n{SITE_PUBLIC_URL}",
    )


async def handle_owner_edit(message: dict[str, Any]) -> None:
    text = extract_post_text(message)
    if not text or not is_owner(message):
        return
    post = update_post_by_message(message.get("message_id"), text)
    if post:
        await answer(OWNER_BOT_TOKEN, message["chat"]["id"], f"Пост обновлён.\nID: {post['id']}")


async def handle_public_message(message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    text = extract_post_text(message)

    if text.startswith("/start"):
        await answer(
            PUBLIC_BOT_TOKEN, chat_id,
            "Напишите сообщение автору. Я передам его владельцу AkiDoDo.",
        )
        return

    username = sender.get("username")
    name = " ".join(part for part in [sender.get("first_name"), sender.get("last_name")] if part)
    from_line = f"@{username}" if username else name or f"id:{sender.get('id')}"
    await send_owner_message(f"Сообщение от читателя ({from_line}):\n\n{text or '[медиа/вложение]'}")
    await answer(PUBLIC_BOT_TOKEN, chat_id, "Сообщение передано автору.")


async def clear_pending_updates(token: str, setting_key: str) -> None:
    """Пропускает накопленные сообщения при холодном старте (свежая БД)."""
    if get_setting(setting_key, "0") != "0":
        return
    try:
        result = await telegram_api(token, "getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
        updates = result.get("result", [])
        if updates:
            latest_id = updates[-1]["update_id"]
            await telegram_api(token, "getUpdates", {"offset": latest_id + 1, "limit": 1, "timeout": 0})
            set_setting(setting_key, str(latest_id))
            print(f"[{setting_key}] Пропущены накопленные сообщения, старт с {latest_id + 1}")
        else:
            print(f"[{setting_key}] Нет накопленных сообщений")
    except Exception as exc:
        print(f"[{setting_key}] clear_pending_updates error: {exc}")


async def poll_bot(token: str, setting_key: str, handler, edit_handler=None) -> None:
    while True:
        try:
            offset = int(get_setting(setting_key, "0"))
            result = await telegram_api(
                token, "getUpdates",
                {"offset": offset + 1, "timeout": 25, "allowed_updates": ["message", "edited_message"]},
            )
            for update in result.get("result", []):
                set_setting(setting_key, str(update["update_id"]))
                if "message" in update:
                    await handler(update["message"])
                elif edit_handler and "edited_message" in update:
                    await edit_handler(update["edited_message"])
        except Exception as exc:
            err_str = str(exc)
            print(f"Telegram polling error for {setting_key}: {exc}")
            await asyncio.sleep(30 if "409" in err_str else 5)


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    if OWNER_BOT_TOKEN and OWNER_CHAT_IDS:
        await clear_pending_updates(OWNER_BOT_TOKEN, "owner_bot_offset")
        asyncio.create_task(
            poll_bot(OWNER_BOT_TOKEN, "owner_bot_offset", handle_owner_message, handle_owner_edit)
        )
    if PUBLIC_BOT_TOKEN and OWNER_CHAT_IDS:
        await clear_pending_updates(PUBLIC_BOT_TOKEN, "public_bot_offset")
        asyncio.create_task(
            poll_bot(PUBLIC_BOT_TOKEN, "public_bot_offset", handle_public_message)
        )


class ManualPost(BaseModel):
    content: str


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok", "service": "AkiDoDo Backend"}


@app.get("/posts")
def list_posts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE is_published = 1 ORDER BY created_at DESC"
        ).fetchall()
    return [post_from_row(row) for row in rows]


@app.post("/posts")
def add_manual_post(data: ManualPost, x_admin_secret: str | None = Header(default=None)) -> dict[str, Any]:
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Admin secret required")
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    return create_post(data.content.strip(), None)


@app.delete("/posts/{post_id}")
def delete_manual_post(post_id: str, x_admin_secret: str | None = Header(default=None)) -> dict[str, bool]:
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Admin secret required")
    return {"ok": delete_post(post_id)}


@app.get("/status")
def status() -> dict[str, Any]:
    return {
        "owner_bot_configured": bool(OWNER_BOT_TOKEN and OWNER_CHAT_IDS),
        "owner_ids_count": len(OWNER_CHAT_IDS),
        "public_bot_configured": bool(PUBLIC_BOT_TOKEN and OWNER_CHAT_IDS),
        "public_bot_username": PUBLIC_BOT_USERNAME,
    }
