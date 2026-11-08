import psycopg2
import asyncio
import sqlite3
import logging
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from openai import AsyncOpenAI

# ================= НАСТРОЙКИ =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

FREE_MODELS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "deepseek/deepseek-r1:free",
    "google/gemma-3-27b-it:free",
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
class DBConnection:
    def __init__(self):
        clean_url = DATABASE_URL.strip().strip('"').strip("'")
        self.is_pg = bool(clean_url)
        if self.is_pg:
            self.conn = psycopg2.connect(clean_url, connect_timeout=10)
            self.conn.autocommit = True
        else:
            self.conn = sqlite3.connect("characters.db", timeout=10)

    def cursor(self):
        return self.conn.cursor()

    def commit(self):
        if not self.is_pg:
            self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


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
            # Безопасная миграция для PostgreSQL (Neon Tech)
            c.execute("ALTER TABLE characters ADD COLUMN IF NOT EXISTS avatar_url TEXT DEFAULT ''")
            c.execute("ALTER TABLE user_personas ADD COLUMN IF NOT EXISTS avatar_url TEXT DEFAULT ''")
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_persona_id INTEGER")
            c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS delta INTEGER DEFAULT 0")
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
        logging.info("🟢 База данных успешно инициализирована!")
    except Exception as e:
        logging.error(f"🔴 Ошибка при инициализации базы данных: {e}")


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


def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )


def skip_or_cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭ Пропустить")],
            [KeyboardButton(text="❌ Отмена")]
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
        c.execute("SELECT id, name, user_id FROM characters WHERE user_id IN (0, %s) ORDER BY id ASC", (user_id,))
    else:
        c.execute("SELECT id, name, user_id FROM characters WHERE user_id IN (0, ?) ORDER BY id ASC", (user_id,))
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
        c.execute("SELECT active_persona_id FROM users WHERE user_id = %s", (user_id,))
    else:
        c.execute("SELECT active_persona_id FROM users WHERE user_id = ?", (user_id,))
    u_row = c.fetchone()
    active_id = u_row[0] if u_row else None

    if conn.is_pg:
        c.execute("SELECT id, name FROM user_personas WHERE user_id = %s ORDER BY id ASC", (user_id,))
    else:
        c.execute("SELECT id, name FROM user_personas WHERE user_id = ? ORDER BY id ASC", (user_id,))
    personas = c.fetchall()
    conn.close()

    buttons = []
    for persona_id, name in personas:
        prefix = "✅ " if persona_id == active_id else "👤 "
        buttons.append([InlineKeyboardButton(text=f"{prefix}{name}", callback_data=f"viewpersona_{persona_id}")])
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
        c.execute(
            "INSERT INTO users (user_id, active_char_id, active_persona_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET active_char_id = EXCLUDED.active_char_id, active_persona_id = EXCLUDED.active_persona_id",
            (user_id, char_id, active_persona)
        )
        c.execute(
            "INSERT INTO relationships (user_id, char_id, affection) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, char_id) DO NOTHING",
            (user_id, char_id, DEFAULT_AFFECTION)
        )
    else:
        c.execute("INSERT OR REPLACE INTO users (user_id, active_char_id, active_persona_id) VALUES (?, ?, ?)", (user_id, char_id, active_persona))
        c.execute("INSERT OR IGNORE INTO relationships (user_id, char_id, affection) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


def set_active_persona(user_id: int, persona_id: int | None):
    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT active_char_id FROM users WHERE user_id = %s", (user_id,))
    else:
        c.execute("SELECT active_char_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    active_char = row[0] if row else None

    if conn.is_pg:
        c.execute(
            "INSERT INTO users (user_id, active_char_id, active_persona_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET active_persona_id = EXCLUDED.active_persona_id",
            (user_id, active_char, persona_id)
        )
    else:
        c.execute("INSERT OR REPLACE INTO users (user_id, active_char_id, active_persona_id) VALUES (?, ?, ?)", (user_id, active_char, persona_id))
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
        c.execute(
            "INSERT INTO relationships (user_id, char_id, affection) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, char_id) DO UPDATE SET affection = EXCLUDED.affection",
            (user_id, char_id, new_score)
        )
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
        c.execute(
            "INSERT INTO relationships (user_id, char_id, affection) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, char_id) DO UPDATE SET affection = EXCLUDED.affection",
            (user_id, char_id, DEFAULT_AFFECTION)
        )
    else:
        c.execute("DELETE FROM messages WHERE user_id = ? AND char_id = ?", (user_id, char_id))
        c.execute("INSERT OR REPLACE INTO relationships (user_id, char_id, affection) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


# --- ВСПОМОГАТЕЛЬНАЯ ОТПРАВКА СООБЩЕНИЙ ---
async def safe_send_text(message: types.Message, text: str, reply_markup=None):
    try:
        return await message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception:
        return await message.answer(text, reply_markup=reply_markup)


async def send_with_avatar(message: types.Message, text: str, avatar_url: str, reply_markup=None):
    if avatar_url and len(text) <= 1000:
        try:
            return await message.answer_photo(photo=avatar_url, caption=text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            try:
                return await message.answer_photo(photo=avatar_url, caption=text, reply_markup=reply_markup)
            except Exception:
                pass
    return await safe_send_text(message, text, reply_markup=reply_markup)


# --- ГЕНЕРАЦИЯ ОТВЕТА ---
async def generate_ai_reply(system_prompt: str, history: list, current_score: int, user_profile: str) -> tuple[str, str, int]:
    status_title, behavior_guide = get_relationship_status(current_score)

    system_instruction = (
        f"Персонаж и его правила:\n{system_prompt}\n\n"
        f"Информация об игроке:\n{user_profile}\n\n"
        f"Уровень связи: {current_score}/100 ({status_title}). {behavior_guide}\n\n"
        "Правила:\n"
        "1. Пиши на русском языке, без латиницы и иностранных слов.\n"
        "2. Отвечай от первого лица, действия пиши в *звездочках*.\n"
        "3. В конце ответа укажи настроение и дельту в формате:\n"
        "[MOOD: спокойный интерес | DELTA: +2]"
    )

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
                max_tokens=1500,
            )
            if response.choices and response.choices[0].message.content:
                content = response.choices[0].message.content.strip()
                # Убираем блок <think>...</think>, если попалась рассуждающая модель
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                if content:
                    raw_reply = content
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


# ================= ОБРАБОТЧИКИ КОМАНД И МЕНЮ =================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **Добро пожаловать в мир ролевых ИИ-персонажей!**\n\n"
        "Используйте меню ниже, чтобы выбрать персонажа, создать своего героя или настроить свою персону (анкету игрока).",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )


@dp.message(F.text == "❌ Отмена", StateFilter("*"))
async def cancel_state(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_kb())


# --- ВЫБОР ПЕРСОНАЖА ---
@dp.message(F.text == "🎭 Выбрать персонажа")
async def menu_choose_char(message: types.Message, state: FSMContext):
    await state.clear()
    kb = get_characters_kb(message.from_user.id)
    await message.answer("🎭 **Выберите персонажа для общения:**", parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("selectchar_"))
async def cb_select_char(call: types.CallbackQuery):
    char_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    set_active_character(user_id, char_id)
    char = get_active_character(user_id)
    if not char:
        await call.answer("Персонаж не найден", show_alert=True)
        return

    _, name, _, greeting, avatar_url, _ = char
    score = get_affection(user_id, char_id)
    history = get_chat_history(user_id, char_id)

    await call.answer()

    if history:
        last_msg = history[-1]["content"]
        footer = format_affection_footer(score, 0, "Возврат к диалогу")
        text = f"✅ **Вы вернулись к {name}.**\n\nПоследняя реплика:\n{last_msg}{footer}"
        await send_with_avatar(call.message, text, avatar_url, reply_markup=regen_kb())
    else:
        save_message(user_id, char_id, "assistant", greeting, 0)
        footer = format_affection_footer(score, 0, "Начало истории")
        text = f"✅ **Выбран персонаж:** {name}\n\n{greeting}{footer}"
        await send_with_avatar(call.message, text, avatar_url)


# --- СОЗДАНИЕ ПЕРСОНАЖА ---
@dp.message(F.text == "➕ Создать персонажа")
async def start_create_char(message: types.Message, state: FSMContext):
    await state.set_state(CreateChar.name)
    await message.answer("Введите **имя** нового персонажа (можно со смайликом):", parse_mode="Markdown", reply_markup=cancel_kb())


@dp.message(CreateChar.name)
async def process_char_name(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(CreateChar.prompt)
    await message.answer(
        "Опишите **характер, лор и манеру речи** персонажа (системный промпт):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@dp.message(CreateChar.prompt)
async def process_char_prompt(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(prompt=message.text.strip())
    await state.set_state(CreateChar.greeting)
    await message.answer(
        "Напишите **первое сообщение (приветствие)**, с которого персонаж начнет диалог (действия в *звездочках*):",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@dp.message(CreateChar.greeting)
async def process_char_greeting(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(greeting=message.text.strip())
    await state.set_state(CreateChar.avatar)
    await message.answer(
        "Отправьте **картинку (фото)** или прямую **ссылку на изображение** для аватара персонажа (или нажмите «⏭ Пропустить»):",
        parse_mode="Markdown",
        reply_markup=skip_or_cancel_kb()
    )


@dp.message(CreateChar.avatar)
async def process_char_avatar(message: types.Message, state: FSMContext):
    avatar_url = ""
    if message.photo:
        avatar_url = message.photo[-1].file_id
    elif message.text and message.text.strip() != "⏭ Пропустить":
        avatar_url = message.text.strip()

    data = await state.get_data()
    user_id = message.from_user.id

    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute(
            "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user_id, data["name"], data["prompt"], data["greeting"], avatar_url)
        )
        char_id = c.fetchone()[0]
    else:
        c.execute(
            "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (?, ?, ?, ?, ?)",
            (user_id, data["name"], data["prompt"], data["greeting"], avatar_url)
        )
        char_id = c.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    set_active_character(user_id, char_id)
    clear_history(user_id, char_id)
    save_message(user_id, char_id, "assistant", data["greeting"], 0)

    footer = format_affection_footer(DEFAULT_AFFECTION, 0, "Начало истории")
    await message.answer(f"🎉 Персонаж **{data['name']}** успешно создан и выбран!", parse_mode="Markdown", reply_markup=main_menu_kb())
    await send_with_avatar(message, f"{data['greeting']}{footer}", avatar_url)


# --- МОИ ПЕРСОНЫ ---
@dp.message(F.text == "👥 Мои персоны")
async def menu_personas(message: types.Message, state: FSMContext):
    await state.clear()
    kb = get_personas_kb(message.from_user.id)
    if not kb.inline_keyboard:
        await message.answer(
            "У вас пока нет созданных персон.\nНажмите **«➕ Создать персону»**, чтобы персонажи знали, кто вы!",
            parse_mode="Markdown"
        )
        return
    await message.answer("👥 **Ваши персоны** (нажмите для просмотра и выбора):", parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("viewpersona_"))
async def cb_view_persona(call: types.CallbackQuery):
    persona_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("SELECT name, profile_text, avatar_url FROM user_personas WHERE id = %s AND user_id = %s", (persona_id, user_id))
    else:
        c.execute("SELECT name, profile_text, avatar_url FROM user_personas WHERE id = ? AND user_id = ?", (persona_id, user_id))
    row = c.fetchall()
    conn.close()

    if not row:
        await call.answer("Персона не найдена.", show_alert=True)
        return

    name, profile_text, avatar_url = row[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделать активной", callback_data=f"setpersona_{persona_id}")],
        [InlineKeyboardButton(text="✏️ Редактировать описание", callback_data=f"editpersona_{persona_id}")],
        [InlineKeyboardButton(text="🗑 Удалить персону", callback_data=f"delpersona_{persona_id}")]
    ])

    await call.answer()
    text = f"👤 **Персона:** {name}\n\n📝 **Описание:**\n{profile_text}"
    await send_with_avatar(call.message, text, avatar_url, reply_markup=kb)


@dp.callback_query(F.data.startswith("setpersona_"))
async def cb_set_persona(call: types.CallbackQuery):
    persona_id = int(call.data.split("_")[1])
    set_active_persona(call.from_user.id, persona_id)
    await call.answer("Персона выбрана активной!")
    await call.message.answer("✅ Активная персона обновлена! Теперь персонажи будут учитывать эту анкету.")


@dp.callback_query(F.data.startswith("delpersona_"))
async def cb_del_persona(call: types.CallbackQuery):
    persona_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("DELETE FROM user_personas WHERE id = %s AND user_id = %s", (persona_id, user_id))
        c.execute("UPDATE users SET active_persona_id = NULL WHERE user_id = %s AND active_persona_id = %s", (user_id, persona_id))
    else:
        c.execute("DELETE FROM user_personas WHERE id = ? AND user_id = ?", (persona_id, user_id))
        c.execute("UPDATE users SET active_persona_id = NULL WHERE user_id = ? AND active_persona_id = ?", (user_id, persona_id))
    conn.commit()
    conn.close()

    await call.answer("Персона удалена")
    await call.message.answer("🗑 Персона была удалена.")


@dp.callback_query(F.data.startswith("editpersona_"))
async def cb_edit_persona(call: types.CallbackQuery, state: FSMContext):
    persona_id = int(call.data.split("_")[1])
    await state.update_data(persona_id=persona_id)
    await state.set_state(EditPersona.text)
    await call.answer()
    await call.message.answer("Введите **новое описание** для вашей персоны:", parse_mode="Markdown", reply_markup=cancel_kb())


@dp.message(EditPersona.text)
async def process_edit_persona_text(message: types.Message, state: FSMContext):
    if not message.text:
        return
    data = await state.get_data()
    persona_id = data["persona_id"]

    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("UPDATE user_personas SET profile_text = %s WHERE id = %s AND user_id = %s", (message.text.strip(), persona_id, message.from_user.id))
    else:
        c.execute("UPDATE user_personas SET profile_text = ? WHERE id = ? AND user_id = ?", (message.text.strip(), persona_id, message.from_user.id))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ Описание персоны успешно обновлено!", reply_markup=main_menu_kb())


# --- СОЗДАНИЕ ПЕРСОНЫ ---
@dp.message(F.text == "➕ Создать персону")
async def start_create_persona(message: types.Message, state: FSMContext):
    await state.set_state(CreatePersona.name)
    await message.answer("Введите **имя или прозвище** вашей персоны:", parse_mode="Markdown", reply_markup=cancel_kb())


@dp.message(CreatePersona.name)
async def process_persona_name(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(CreatePersona.text)
    await message.answer(
        "Опишите **себя (внешность, характер, род занятий)**, чтобы персонажи учитывали это в диалоге:",
        parse_mode="Markdown",
        reply_markup=cancel_kb()
    )


@dp.message(CreatePersona.text)
async def process_persona_text(message: types.Message, state: FSMContext):
    if not message.text:
        return
    await state.update_data(text=message.text.strip())
    await state.set_state(CreatePersona.avatar)
    await message.answer(
        "Отправьте **фото** или **ссылку на картинку** для вашей персоны (или нажмите «⏭ Пропустить»):",
        parse_mode="Markdown",
        reply_markup=skip_or_cancel_kb()
    )


@dp.message(CreatePersona.avatar)
async def process_persona_avatar(message: types.Message, state: FSMContext):
    avatar_url = ""
    if message.photo:
        avatar_url = message.photo[-1].file_id
    elif message.text and message.text.strip() != "⏭ Пропустить":
        avatar_url = message.text.strip()

    data = await state.get_data()
    user_id = message.from_user.id

    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute(
            "INSERT INTO user_personas (user_id, name, profile_text, avatar_url) VALUES (%s, %s, %s, %s) RETURNING id",
            (user_id, data["name"], data["text"], avatar_url)
        )
        persona_id = c.fetchone()[0]
    else:
        c.execute(
            "INSERT INTO user_personas (user_id, name, profile_text, avatar_url) VALUES (?, ?, ?, ?)",
            (user_id, data["name"], data["text"], avatar_url)
        )
        persona_id = c.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    set_active_persona(user_id, persona_id)
    await message.answer(
        f"✅ Персона **{data['name']}** создана и установлена как активная!",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )


# --- СБРОС ДИАЛОГА И УДАЛЕНИЕ ПЕРСОНАЖА ---
@dp.message(F.text == "🔄 Сбросить диалог")
async def menu_reset_dialog(message: types.Message):
    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.answer("Сначала выберите персонажа через «🎭 Выбрать персонажа».")
        return

    char_id, name, _, greeting, avatar_url, _ = char
    clear_history(user_id, char_id)
    save_message(user_id, char_id, "assistant", greeting, 0)

    footer = format_affection_footer(DEFAULT_AFFECTION, 0, "Начало истории")
    text = f"🔄 **Диалог с {name} сброшен!**\n\n{greeting}{footer}"
    await send_with_avatar(message, text, avatar_url)


@dp.message(F.text == "🗑 Удалить текущего")
async def menu_delete_char(message: types.Message):
    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.answer("У вас не выбран активный персонаж.")
        return

    char_id, name, _, _, _, owner_id = char
    if owner_id == 0:
        await message.answer("⚠️ Базовых персонажей удалить нельзя. Вы можете удалять только созданных вами персонажей.")
        return

    conn = DBConnection()
    c = conn.cursor()
    if conn.is_pg:
        c.execute("DELETE FROM characters WHERE id = %s AND user_id = %s", (char_id, user_id))
        c.execute("DELETE FROM messages WHERE char_id = %s AND user_id = %s", (char_id, user_id))
        c.execute("DELETE FROM relationships WHERE char_id = %s AND user_id = %s", (char_id, user_id))
        c.execute("UPDATE users SET active_char_id = NULL WHERE user_id = %s", (user_id,))
    else:
        c.execute("DELETE FROM characters WHERE id = ? AND user_id = ?", (char_id, user_id))
        c.execute("DELETE FROM messages WHERE char_id = ? AND user_id = ?", (char_id, user_id))
        c.execute("DELETE FROM relationships WHERE char_id = ? AND user_id = ?", (char_id, user_id))
        c.execute("UPDATE users SET active_char_id = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    await message.answer(f"🗑 Персонаж **{name}** был удален.", parse_mode="Markdown")


# --- ПЕРЕГЕНЕРАЦИЯ ОТВЕТА ---
@dp.callback_query(F.data == "regenerate")
async def cb_regenerate(call: types.CallbackQuery):
    user_id = call.from_user.id
    char = get_active_character(user_id)
    if not char:
        await call.answer("Персонаж не выбран", show_alert=True)
        return

    char_id, _, system_prompt, _, _, _ = char
    if not delete_last_assistant_message(user_id, char_id):
        await call.answer("Нечего перегенерировать", show_alert=True)
        return

    await call.answer("Генерирую новый вариант...")
    await bot.send_chat_action(call.message.chat.id, "typing")

    history = get_chat_history(user_id, char_id)
    current_score = get_affection(user_id, char_id)
    user_profile = get_active_persona_text(user_id)

    reply_text, mood, delta = await generate_ai_reply(system_prompt, history, current_score, user_profile)
    new_score = update_affection(user_id, char_id, delta)
    save_message(user_id, char_id, "assistant", reply_text, delta)

    full_text = reply_text + format_affection_footer(new_score, delta, mood)
    await safe_send_text(call.message, full_text, reply_markup=regen_kb())


# --- ОСНОВНОЙ ДИАЛОГ С ИИ ---
@dp.message(F.text)
async def handle_chat_message(message: types.Message):
    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.answer(
            "🎭 Сначала выберите персонажа, нажав кнопку **«🎭 Выбрать персонажа»** в меню!",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )
        return

    char_id, _, system_prompt, _, _, _ = char
    user_text = message.text.strip()

    await bot.send_chat_action(message.chat.id, "typing")
    save_message(user_id, char_id, "user", user_text, 0)

    history = get_chat_history(user_id, char_id)
    current_score = get_affection(user_id, char_id)
    user_profile = get_active_persona_text(user_id)

    reply_text, mood, delta = await generate_ai_reply(system_prompt, history, current_score, user_profile)
    new_score = update_affection(user_id, char_id, delta)
    save_message(user_id, char_id, "assistant", reply_text, delta)

    full_text = reply_text + format_affection_footer(new_score, delta, mood)
    await safe_send_text(message, full_text, reply_markup=regen_kb())


# ================= ВЕБ-СЕРВЕР ДЛЯ RENDER И ЗАПУСК =================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        return  # Отключаем спам HTTP-запросов в логах


def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logging.info(f"🌐 Фоновый HTTP-сервер запущен на порту {port}")
    server.serve_forever()


import urllib.request

async def keep_alive():
    """Пингует базу Neon и веб-сервер каждые 4 минуты, чтобы они не засыпали."""
    url = os.getenv("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.getenv('PORT', 8080)}"
    while True:
        await asyncio.sleep(240)
        # 1. Пинг базы данных Neon
        try:
            conn = DBConnection()
            c = conn.cursor()
            c.execute("SELECT 1")
            conn.close()
        except Exception as e:
            logging.warning(f"Ошибка пинга БД: {e}")

        # 2. Пинг HTTP-сервера Render
        try:
            await asyncio.to_thread(urllib.request.urlopen, url, timeout=10)
        except Exception as e:
            logging.warning(f"Ошибка пинга HTTP: {e}")


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(keep_alive())
    logging.info("🚀 Бот запущен и готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    asyncio.run(main())
