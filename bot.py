import psycopg2
import asyncio
import sqlite3
import logging
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from openai import AsyncOpenAI

# ================= НАСТРОЙКИ =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

FREE_MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "google/gemma-3-27b-it:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "openrouter/free"
]

MAX_HISTORY_MESSAGES = 20
DEFAULT_AFFECTION = 20
# =============================================

logging.basicConfig(level=logging.INFO)

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# --- БАЗА ДАННЫХ ---
DATABASE_URL = os.getenv("DATABASE_URL")

class DBConnection:
    def __init__(self):
        self.is_pg = bool(DATABASE_URL and DATABASE_URL.strip())
        if self.is_pg:
            self.conn = psycopg2.connect(DATABASE_URL.strip())
            self.conn.autocommit = True
        else:
            self.conn = sqlite3.connect("characters.db")

    def cursor(self):
        return self.conn.cursor()

    def commit(self):
        if not self.is_pg:
            self.conn.commit()

    def close(self):
        self.conn.close()


def init_db():
    try:
        conn = DBConnection()
        c = conn.cursor()
        
        if conn.is_pg:
            c.execute("""
                CREATE TABLE IF NOT EXISTS characters (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    name TEXT,
                    prompt TEXT,
                    greeting TEXT,
                    avatar_url TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    active_char_id INTEGER,
                    active_persona_id INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_personas (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    name TEXT,
                    profile_text TEXT,
                    avatar_url TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    char_id INTEGER,
                    role TEXT,
                    content TEXT,
                    delta INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    user_id BIGINT,
                    char_id INTEGER,
                    affection INTEGER DEFAULT 20,
                    PRIMARY KEY (user_id, char_id)
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    prompt TEXT,
                    greeting TEXT,
                    avatar_url TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    active_char_id INTEGER,
                    active_persona_id INTEGER
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_personas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    profile_text TEXT,
                    avatar_url TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    char_id INTEGER,
                    role TEXT,
                    content TEXT,
                    delta INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    user_id INTEGER,
                    char_id INTEGER,
                    affection INTEGER DEFAULT 20,
                    PRIMARY KEY (user_id, char_id)
                )
            """)

            # PRAGMA проверки нужны только для SQLite
            c.execute("PRAGMA table_info(characters)")
            columns = [col[1] for col in c.fetchall()]
            if "avatar_url" not in columns:
                c.execute("ALTER TABLE characters ADD COLUMN avatar_url TEXT DEFAULT ''")

            c.execute("PRAGMA table_info(user_personas)")
            p_columns = [col[1] for col in c.fetchall()]
            if "avatar_url" not in p_columns:
                c.execute("ALTER TABLE user_personas ADD COLUMN avatar_url TEXT DEFAULT ''")

            c.execute("PRAGMA table_info(users)")
            u_columns = [col[1] for col in c.fetchall()]
            if "active_persona_id" not in u_columns:
                c.execute("ALTER TABLE users ADD COLUMN active_persona_id INTEGER")

        # Проверка и добавление дефолтных персонажей, если их нет
        c.execute("SELECT COUNT(*) FROM characters WHERE user_id = 0")
        if c.fetchone()[0] == 0:
            default_chars = [
                (
                    0,
                    "🕵️‍♂️ Шерлок Холмс",
                    "Ты Шерлок Холмс, гениальный детектив с Бейкер-стрит. Наблюдателен, ироничен, ценишь острый ум, логику и преданных напарников.",
                    "*Откладывает скрипку в сторону и внимательно осматривает вас с ног до головы* Любопытно. Вы явно пришли с делом, которое не терпит отлагательств. Рассказывайте.",
                    "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=500"
                ),
                (
                    0,
                    "👾 Никс (Хакерша)",
                    "Ты Никс — дерзкая девушка-нетраннер из неонового мегаполиса. Общаешься живо, с киберпанк-сленгом и сарказмом.",
                    "*Поправляет светящиеся неоновые очки и отпивает энергетик* О, живой человек в моем бункере. Какими судьбами?",
                    "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=500"
                )
            ]
            if conn.is_pg:
                c.executemany(
                    "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (%s, %s, %s, %s, %s)",
                    default_chars
                )
            else:
                c.executemany(
                    "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (?, ?, ?, ?, ?)",
                    default_chars
                )

        conn.commit()
        conn.close()
        print("🟢 База данных успешно инициализирована!")
    except Exception as e:
        print(f"🔴 Ошибка при инициализации базы данных: {e}")


# --- СИСТЕМА УРОВНЯ ДОВЕРИЯ ---
def get_relationship_status(score: int) -> tuple[str, str]:
    if score <= 15:
        return ("❄️ Холод и недоверие", "Держишь жесткую дистанцию и относишься настороженно.")
    elif score <= 35:
        return ("🤝 Знакомые / Напарники", "Общаешься в своем обычном характере, присматриваешься к собеседнику.")
    elif score <= 55:
        return ("😊 Хорошие друзья", "Доверяешь собеседнику, охотно шутишь, проявляешь теплоту.")
    elif score <= 75:
        return ("🔥 Близкие союзники", "Между вами крепкая связь и доверие без утайки.")
    else:
        return ("💎 Полное доверие", "Самый надежный партнер, абсолютное взаимопонимание.")


def format_affection_footer(score: int, delta: int, mood: str) -> str:
    filled = round(score / 10)
    bar = "💚" * filled + "🤍" * (10 - filled)
    status_title, _ = get_relationship_status(score)

    if delta > 0:
        delta_str = f" (+{delta}%)"
    elif delta < 0:
        delta_str = f" ({delta}%)"
    else:
        delta_str = " (0%)"

    return (
        f"\n\n───────────────────\n"
        f"💭 **Настроение:** {mood}\n"
        f"📊 **Уровень связи:** {bar} **{score}%**{delta_str}\n"
        f"🤝 **Статус:** {status_title}"
    )


# --- СОСТОЯНИЯ ---
class CreateChar(StatesGroup):
    name = State()
    prompt = State()
    greeting = State()
    avatar = State()


class CreatePersona(StatesGroup):
    name = State()
    text = State()
    avatar = State()


class EditPersona(StatesGroup):
    choice = State()
    text = State()
    avatar = State()


# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎭 Выбрать персонажа"), KeyboardButton(text="➕ Создать персонажа")],
            [KeyboardButton(text="👥 Мои персоны"), KeyboardButton(text="➕ Создать персону")],
            [KeyboardButton(text="🔄 Сбросить диалог"), KeyboardButton(text="🗑 Удалить текущего")]
        ],
        resize_keyboard=True
    )


def regen_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regenerate")]
        ]
    )


def get_characters_kb(user_id: int):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT id, name, user_id FROM characters WHERE user_id IN (0, %s)", (user_id,))
    else:
        c.execute("SELECT id, name, user_id FROM characters WHERE user_id IN (0, ?)", (user_id,))
    chars = c.fetchall()
    conn.close()

    buttons = []
    for char_id, name, owner_id in chars:
        label = f"{name} (Мой)" if owner_id != 0 else name
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"selectchar_{char_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_personas_kb(user_id: int):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT id, name FROM user_personas WHERE user_id = %s", (user_id,))
    else:
        c.execute("SELECT id, name FROM user_personas WHERE user_id = ?", (user_id,))
    personas = c.fetchall()
    conn.close()

    buttons = []
    for persona_id, name in personas:
        buttons.append([InlineKeyboardButton(text=f"👤 {name}", callback_data=f"viewpersona_{persona_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- РАБОТА С БД ---
def get_active_persona_text(user_id: int) -> str:
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT active_persona_id FROM users WHERE user_id = %s", (user_id,))
    else:
        c.execute("SELECT active_persona_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row or not row[0]:
        conn.close()
        return "Игрок (без детального описания)"
    
    persona_id = row[0]
    if conn.is_pg:
        c.execute("SELECT name, profile_text FROM user_personas WHERE id = %s", (persona_id,))
    else:
        c.execute("SELECT name, profile_text FROM user_personas WHERE id = ?", (persona_id,))
    p_row = c.fetchone()
    conn.close()
    if p_row:
        return f"Имя/Роль: {p_row[0]}\nОписание: {p_row[1]}"
    return "Игрок (без детального описания)"


def set_active_character(user_id: int, char_id: int):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT active_persona_id FROM users WHERE user_id = %s", (user_id,))
    else:
        c.execute("SELECT active_persona_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    active_persona = row[0] if row else None

    if conn.is_pg:
        c.execute("INSERT INTO users (user_id, active_char_id, active_persona_id) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET active_char_id = EXCLUDED.active_char_id, active_persona_id = EXCLUDED.active_persona_id", (user_id, char_id, active_persona))
        c.execute("INSERT INTO relationships (user_id, char_id, affection) VALUES (%s, %s, %s) ON CONFLICT (user_id, char_id) DO NOTHING", (user_id, char_id, DEFAULT_AFFECTION))
    else:
        c.execute("INSERT OR REPLACE INTO users (user_id, active_char_id, active_persona_id) VALUES (?, ?, ?)", (user_id, char_id, active_persona))
        c.execute("INSERT OR IGNORE INTO relationships (user_id, char_id, affection) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


def get_active_character(user_id: int):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("""
            SELECT c.id, c.name, c.prompt, c.greeting, c.avatar_url, c.user_id
            FROM users u
            JOIN characters c ON u.active_char_id = c.id
            WHERE u.user_id = %s
        """, (user_id,))
    else:
        c.execute("""
            SELECT c.id, c.name, c.prompt, c.greeting, c.avatar_url, c.user_id
            FROM users u
            JOIN characters c ON u.active_char_id = c.id
            WHERE u.user_id = ?
        """, (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_affection(user_id: int, char_id: int) -> int:
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT affection FROM relationships WHERE user_id = %s AND char_id = %s", (user_id, char_id))
    else:
        c.execute("SELECT affection FROM relationships WHERE user_id = ? AND char_id = ?", (user_id, char_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else DEFAULT_AFFECTION


def update_affection(user_id: int, char_id: int, delta: int) -> int:
    current = get_affection(user_id, char_id)
    new_score = max(0, min(100, current + delta))
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("INSERT INTO relationships (user_id, char_id, affection) VALUES (%s, %s, %s) ON CONFLICT (user_id, char_id) DO UPDATE SET affection = EXCLUDED.affection", (user_id, char_id, new_score))
    else:
        c.execute("INSERT OR REPLACE INTO relationships (user_id, char_id, affection) VALUES (?, ?, ?)", (user_id, char_id, new_score))
    conn.commit()
    conn.close()
    return new_score


def save_message(user_id: int, char_id: int, role: str, content: str, delta: int = 0):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("INSERT INTO messages (user_id, char_id, role, content, delta) VALUES (%s, %s, %s, %s, %s)", (user_id, char_id, role, content, delta))
    else:
        c.execute("INSERT INTO messages (user_id, char_id, role, content, delta) VALUES (?, ?, ?, ?, ?)", (user_id, char_id, role, content, delta))
    conn.commit()
    conn.close()


def get_chat_history(user_id: int, char_id: int):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT role, content FROM messages WHERE user_id = %s AND char_id = %s ORDER BY id DESC LIMIT %s", (user_id, char_id, MAX_HISTORY_MESSAGES))
    else:
        c.execute("SELECT role, content FROM messages WHERE user_id = ? AND char_id = ? ORDER BY id DESC LIMIT ?", (user_id, char_id, MAX_HISTORY_MESSAGES))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def delete_last_assistant_message(user_id: int, char_id: int) -> bool:
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT id, role, delta FROM messages WHERE user_id = %s AND char_id = %s ORDER BY id DESC LIMIT 2", (user_id, char_id))
    else:
        c.execute("SELECT id, role, delta FROM messages WHERE user_id = ? AND char_id = ? ORDER BY id DESC LIMIT 2", (user_id, char_id))
    rows = c.fetchall()
    if len(rows) < 2 or rows[0][1] != "assistant":
        conn.close()
        return False
    msg_id, _, last_delta = rows[0]
    if conn.is_pg:
        c.execute("DELETE FROM messages WHERE id = %s", (msg_id,))
    else:
        c.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()
    if last_delta != 0:
        update_affection(user_id, char_id, -last_delta)
    return True


def clear_history(user_id: int, char_id: int):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("DELETE FROM messages WHERE user_id = %s AND char_id = %s", (user_id, char_id))
        c.execute("INSERT INTO relationships (user_id, char_id, affection) VALUES (%s, %s, %s) ON CONFLICT (user_id, char_id) DO UPDATE SET affection = EXCLUDED.affection", (user_id, char_id, DEFAULT_AFFECTION))
    else:
        c.execute("DELETE FROM messages WHERE user_id = ? AND char_id = ?", (user_id, char_id))
        c.execute("INSERT OR REPLACE INTO relationships (user_id, char_id, affection) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


# --- ГЕНЕРАЦИЯ ОТВЕТА ---
async def generate_ai_reply(system_prompt: str, history: list, current_score: int, user_profile: str) -> tuple[str, str, int]:
    status_title, behavior_guide = get_relationship_status(current_score)

    system_instruction = "Персонаж и его правила:\n" + system_prompt + "\n\nИнформация об игроке:\n" + user_profile + "\n\nУровень связи: " + str(current_score) + "/100 (" + status_title + "). " + behavior_guide + "\n\nПравила:\n1. Пиши на русском языке, без латиницы и иностранных слов.\n2. Отвечай от первого лица, действия пиши в *звездочках*.\n3. В конце ответа укажи настроение и дельту в формате:\n[MOOD: спокойный интерес | DELTA: +2]"
    
    messages = [{"role": "system", "content": system_instruction}] + history

    raw_reply = None
    last_error = None
    for model_name in FREE_MODELS:
        try:
            if "gemma" in model_name.lower() and history:
                req_messages = [{"role": "user", "content": "СИСТЕМНАЯ ИНСТРУКЦИЯ:\n" + system_instruction}] + history
            else:
                req_messages = messages

            response = await client.chat.completions.create(
                model=model_name,
                messages=req_messages,
                temperature=0.7,
                max_tokens=650,
            )
            if response.choices and response.choices[0].message.content:
                raw_reply = response.choices[0].message.content.strip()
                break
        except Exception as e:
            last_error = e
            continue

    if not raw_reply:
        return f"⚠️ Серверы перегружены. Повторите попытку. ({last_error})", "Ожидание", 0

    mood = "Нейтральное"
    delta = 0
    pattern = r"\[MOOD:\s*(.+?)\s*\|\s*DELTA:\s*([+-]?\d+)\s*\]"
    match = re.search(pattern, raw_reply, re.IGNORECASE)
    if match:
        mood = match.group(1).strip()
        try:
            delta = int(match.group(2))
            delta = max(-5, min(5, delta))
        except ValueError:
            delta = 0
        clean_reply = re.sub(pattern, "", raw_reply, flags=re.IGNORECASE).strip()
    else:
        clean_reply = raw_reply

    return clean_reply, mood, delta
