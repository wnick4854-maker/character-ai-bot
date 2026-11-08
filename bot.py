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
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
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


# --- БАЗА ДАННЫХ (SQLite) ---
def init_db():
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
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
            score INTEGER DEFAULT 20,
            PRIMARY KEY (user_id, char_id)
        )
    """)

    # Проверяем колонку аватара у персон (на случай обновления старой базы)
    c.execute("PRAGMA table_info(user_personas)")
    p_columns = [col[1] for col in c.fetchall()]
    if "avatar_url" not in p_columns:
        c.execute("ALTER TABLE user_personas ADD COLUMN avatar_url TEXT DEFAULT ''")

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
        c.executemany(
            "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (?, ?, ?, ?, ?)",
            default_chars
        )
    conn.commit()
    conn.close()


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
    choice = State() # выбор: текст или фото
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
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT id, name, user_id FROM characters WHERE user_id IN (0, ?)", (user_id,))
    chars = c.fetchall()
    conn.close()

    buttons = []
    for char_id, name, owner_id in chars:
        label = f"{name} (Мой)" if owner_id != 0 else name
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"selectchar_{char_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_personas_kb(user_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT id, name FROM user_personas WHERE user_id = ?", (user_id,))
    personas = c.fetchall()
    conn.close()

    buttons = []
    for persona_id, name in personas:
        buttons.append([InlineKeyboardButton(text=f"👤 {name}", callback_data=f"viewpersona_{persona_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- РАБОТА С БД ---
def get_active_persona_text(user_id: int) -> str:
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT active_persona_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row or not row[0]:
        conn.close()
        return "Игрок (без детального описания)"
    
    persona_id = row[0]
    c.execute("SELECT name, profile_text FROM user_personas WHERE id = ?", (persona_id,))
    p_row = c.fetchone()
    conn.close()
    if p_row:
        return f"Имя/Роль: {p_row[0]}\nОписание: {p_row[1]}"
    return "Игрок (без детального описания)"


def set_active_character(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT active_persona_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    active_persona = row[0] if row else None

    c.execute("INSERT OR REPLACE INTO users (user_id, active_char_id, active_persona_id) VALUES (?, ?, ?)", (user_id, char_id, active_persona))
    c.execute("INSERT OR IGNORE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


def get_active_character(user_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
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
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT score FROM relationships WHERE user_id = ? AND char_id = ?", (user_id, char_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else DEFAULT_AFFECTION


def update_affection(user_id: int, char_id: int, delta: int) -> int:
    current = get_affection(user_id, char_id)
    new_score = max(0, min(100, current + delta))
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)", (user_id, char_id, new_score))
    conn.commit()
    conn.close()
    return new_score


def save_message(user_id: int, char_id: int, role: str, content: str, delta: int = 0):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("INSERT INTO messages (user_id, char_id, role, content, delta) VALUES (?, ?, ?, ?, ?)", (user_id, char_id, role, content, delta))
    conn.commit()
    conn.close()


def get_chat_history(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT role, content FROM messages WHERE user_id = ? AND char_id = ? ORDER BY id DESC LIMIT ?", (user_id, char_id, MAX_HISTORY_MESSAGES))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def delete_last_assistant_message(user_id: int, char_id: int) -> bool:
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT id, role, delta FROM messages WHERE user_id = ? AND char_id = ? ORDER BY id DESC LIMIT 2", (user_id, char_id))
    rows = c.fetchall()
    if len(rows) < 2 or rows[0][1] != "assistant":
        conn.close()
        return False
    msg_id, _, last_delta = rows[0]
    c.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()
    if last_delta != 0:
        update_affection(user_id, char_id, -last_delta)
    return True


def clear_history(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE user_id = ? AND char_id = ?", (user_id, char_id))
    c.execute("INSERT OR REPLACE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


# --- ГЕНЕРАЦИЯ ОТВЕТА С УЧЕТОМ ПЕРСОНЫ ---
async def generate_ai_reply(system_prompt: str, history: list, current_score: int, user_profile: str) -> tuple[str, str, int]:
    status_title, behavior_guide = get_relationship_status(current_score)

    system_instruction = (
        f"{system_prompt}\n\n"
        f"Информация о собеседнике (Игроке):\n{user_profile}\n\n"
        f"Текущий уровень доверия/связи с собеседником: {current_score}/100 ({status_title}). "
        f"{behavior_guide}\n\n"
        "Правила ролевой игры:\n"
        "1. Всегда оставайся в образе, отвечай на русском языке.\n"
        "2. Используй *звездочки* для описания действий, мимики и окружения.\n"
        "3. Никогда не упоминай, что ты ИИ.\n"
        "4. ОБЯЗАТЕЛЬНО в самом конце ответа на новой строке укажи настроение (2-4 слова) "
        "и изменение связи от последней реплики игрока (целое число от -5 до +5) в формате:\n"
        "[MOOD: спокойный интерес | DELTA: +2]"
    )
    messages = [{"role": "system", "content": system_instruction}] + history

    raw_reply = None
    last_error = None
    for model_name in FREE_MODELS:
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.85,
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
    pattern = r"\[MOOD:\s*(.+?)\s*\Vert{}\s*DELTA:\s*([+-]?\d+)\s*\]"
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


# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Добро пожаловать в личный Character.AI!**\n\n"
        "Выбирай персонажа, создавай и кастомизируй свои **персоны** с загрузкой фото прямо в чат!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )
    await message.answer("Доступные персонажи:", reply_markup=get_characters_kb(message.from_user.id))


@dp.message(F.text == "🎭 Выбрать персонажа")
async def show_characters(message: types.Message):
    await message.answer("Выбери персонажа для общения:", reply_markup=get_characters_kb(message.from_user.id))


# --- УПРАВЛЕНИЕ ПЕРСОНАМИ С АВАТАРКАМИ ---
@dp.message(F.text == "👥 Мои персоны")
async def show_personas(message: types.Message):
    user_id = message.from_user.id
    kb = get_personas_kb(user_id)
    if not kb.inline_keyboard:
        await message.answer("У вас пока нет созданных персон. Нажмите «➕ Создать персону» в меню!", reply_markup=main_menu_kb())
        return
    await message.answer("Выберите персону для просмотра, выбора, редактирования или удаления:", reply_markup=kb)


@dp.callback_query(F.data.startswith("viewpersona_"))
async def view_persona_callback(callback: types.CallbackQuery):
    persona_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT name, profile_text, avatar_url FROM user_personas WHERE id = ?", (persona_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        await callback.answer("Персона не найдена.")
        return

    name, text, avatar = row
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выбрать эту", callback_data=f"selectpersona_{persona_id}")],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"edittext_{persona_id}"),
             InlineKeyboardButton(text="🖼 Изменить фото", callback_data=f"editphoto_{persona_id}")],
            [InlineKeyboardButton(text="🗑 Удалить персону", callback_data=f"delpersona_{persona_id}")]
        ]
    )
    
    caption = f"👤 **Персона: {name}**\n\n{text}"
    if avatar:
        try:
            await callback.message.answer_photo(photo=avatar, caption=caption, reply_markup=kb, parse_mode="Markdown")
            await callback.message.delete()
            await callback.answer()
            return
        except Exception:
            pass
    
    await callback.message.edit_text(caption, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("selectpersona_"))
async def select_persona_callback(callback: types.CallbackQuery):
    persona_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("SELECT name FROM user_personas WHERE id = ?", (persona_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        await callback.answer("Персона не найдена.")
        return
    persona_name = row[0]

    c.execute("UPDATE users SET active_persona_id = ? WHERE user_id = ?", (persona_id, user_id))
    conn.commit()
    conn.close()

    await callback.message.answer(f"✅ Активная персона изменена на: **{persona_name}**", parse_mode="Markdown")
    await callback.answer()


# --- СОЗДАНИЕ ПЕРСОНЫ (ИМЯ -> ТЕКСТ -> ФОТО) ---
@dp.message(F.text == "➕ Создать персону")
async def start_create_persona(message: types.Message, state: FSMContext):
    await state.set_state(CreatePersona.name)
    await message.answer("✏️ **Шаг 1 из 3:** Введите имя или название для вашей персоны (например, `Детектив Джон`):", parse_mode="Markdown")


@dp.message(CreatePersona.name)
async def process_persona_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(CreatePersona.text)
    await message.answer("🧠 **Шаг 2 из 3:** Опишите вашу персону (возраст, внешность, характер или предысторию):", parse_mode="Markdown")


@dp.message(CreatePersona.text)
async def process_persona_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text.strip())
    await state.set_state(CreatePersona.avatar)
    await message.answer("🖼 **Шаг 3 из 3:** Отправьте картинку (аватарку) для этой персоны **напрямую файлом** (или отправьте текст `-`, если без картинки):", parse_mode="Markdown")


@dp.message(CreatePersona.avatar, F.photo | F.text)
async def process_persona_avatar(message: types.Message, state: FSMContext):
    data = await state.get_data()
    persona_name = data["name"]
    profile_text = data["text"]
    
    avatar_url = ""
    if message.photo:
        avatar_url = message.photo[-1].file_id
    elif message.text and message.text.strip() not in ["-", "нет", "no"]:
        avatar_url = message.text.strip() # если вдруг скинули ссылку текстом

    user_id = message.from_user.id
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("INSERT INTO user_personas (user_id, name, profile_text, avatar_url) VALUES (?, ?, ?, ?)", (user_id, persona_name, profile_text, avatar_url))
    persona_id = c.lastrowid
    c.execute("INSERT OR IGNORE INTO users (user_id, active_persona_id) VALUES (?, ?)", (user_id, persona_id))
    c.execute("UPDATE users SET active_persona_id = ? WHERE user_id = ?", (persona_id, user_id))
    conn.commit()
    conn.close()

    await state.clear()
    text = f"🎉 Персона **{persona_name}** создана и установлена как активная!"
    if avatar_url:
        try:
            await message.answer_photo(photo=avatar_url, caption=text, reply_markup=main_menu_kb(), parse_mode="Markdown")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


# --- РЕДАКТИРОВАНИЕ ПЕРСОНЫ ---
@dp.callback_query(F.data.startswith("edittext_"))
async def edit_persona_text_start(callback: types.CallbackQuery, state: FSMContext):
    persona_id = int(callback.data.split("_")[1])
    await state.update_data(edit_persona_id=persona_id)
    await state.set_state(EditPersona.text)
    await callback.message.answer("✏️ Отправьте новый текст описания для этой персоны:", parse_mode="Markdown")
    await callback.answer()


@dp.message(EditPersona.text)
async def edit_persona_text_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    persona_id = data["edit_persona_id"]
    new_text = message.text.strip()

    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("UPDATE user_personas SET profile_text = ? WHERE id = ?", (new_text, persona_id))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ **Текст персоны успешно обновлен!**", reply_markup=main_menu_kb(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("editphoto_"))
async def edit_persona_photo_start(callback: types.CallbackQuery, state: FSMContext):
    persona_id = int(callback.data.split("_")[1])
    await state.update_data(edit_persona_id=persona_id)
    await state.set_state(EditPersona.avatar)
    await callback.message.answer("🖼 Отправьте новую картинку **файлом** (или отправьте `-`, чтобы удалить фото):", parse_mode="Markdown")
    await callback.answer()


@dp.message(EditPersona.avatar, F.photo | F.text)
async def edit_persona_photo_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    persona_id = data["edit_persona_id"]
    
    avatar_url = ""
    if message.photo:
        avatar_url = message.photo[-1].file_id
    elif message.text and message.text.strip() not in ["-", "нет", "no"]:
        avatar_url = message.text.strip()

    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("UPDATE user_personas SET avatar_url = ? WHERE id = ?", (avatar_url, persona_id))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("✅ **Фото персоны успешно обновлено!**", reply_markup=main_menu_kb(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("delpersona_"))
async def delete_persona_callback(callback: types.CallbackQuery):
    persona_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("DELETE FROM user_personas WHERE id = ? AND user_id = ?", (persona_id, user_id))
    c.execute("UPDATE users SET active_persona_id = NULL WHERE user_id = ? AND active_persona_id = ?", (user_id, persona_id))
    conn.commit()
    conn.close()

    await callback.message.edit_text("🗑 Персона успешно удалена.", parse_mode="Markdown")
    await callback.answer()


# --- СОЗДАНИЕ ПЕРСОНАЖА ИИ (С ПОДДЕРЖКОЙ ЗАГРУЗКИ ФОТО ФАЙЛОМ) ---
@dp.message(F.text == "➕ Создать персонажа")
async def start_create_char(message: types.Message, state: FSMContext):
    await state.set_state(CreateChar.name)
    await message.answer("✏️ **Шаг 1 из 4:** Введите имя персонажа:", parse_mode="Markdown")


@dp.message(CreateChar.name)
async def process_char_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(CreateChar.prompt)
    await message.answer("🧠 **Шаг 2 из 4:** Опишите характер и предысторию персонажа:", parse_mode="Markdown")


@dp.message(CreateChar.prompt)
async def process_char_prompt(message: types.Message, state: FSMContext):
    await state.update_data(prompt=message.text.strip())
    await state.set_state(CreateChar.greeting)
    await message.answer("💬 **Шаг 3 из 4:** Напишите приветствие персонажа:", parse_mode="Markdown")


@dp.message(CreateChar.greeting)
async def process_char_greeting(message: types.Message, state: FSMContext):
    await state.update_data(greeting=message.text.strip())
    await state.set_state(CreateChar.avatar)
    await message.answer("🖼 **Шаг 4 из 4:** Отправьте картинку-аватарку для персонажа **напрямую файлом** (или отправьте `-`, если без картинки):", parse_mode="Markdown")


@dp.message(CreateChar.avatar, F.photo | F.text)
async def process_char_avatar(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    prompt = data["prompt"]
    greeting = data["greeting"]
    
    avatar_url = ""
    if message.photo:
        avatar_url = message.photo[-1].file_id
    elif message.text and message.text.strip() not in ["-", "нет", "no"]:
        avatar_url = message.text.strip()

    user_id = message.from_user.id
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, prompt, greeting, avatar_url)
    )
    char_id = c.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    set_active_character(user_id, char_id)
    save_message(user_id, char_id, "assistant", greeting, delta=0)
    footer = format_affection_footer(DEFAULT_AFFECTION, 0, "Первая встреча")
    text = f"🎉 Персонаж **{name}** создан!\n\n{greeting}{footer}"

    if avatar_url:
        try:
            await message.answer_photo(photo=avatar_url, caption=text, reply_markup=main_menu_kb(), parse_mode="Markdown")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("selectchar_"))
async def select_character_callback(callback: types.CallbackQuery):
    char_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    set_active_character(user_id, char_id)
    char = get_active_character(user_id)
    if not char:
        await callback.answer("Персонаж не найден.")
        return

    _, name, _, greeting, avatar_url, _ = char
    score = get_affection(user_id, char_id)
    history = get_chat_history(user_id, char_id)

    if not history:
        save_message(user_id, char_id, "assistant", greeting, delta=0)
        footer = format_affection_footer(score, 0, "Начало общения")
        text = f"✅ Выбран персонаж: **{name}**\n\n{greeting}{footer}"
    else:
        footer = format_affection_footer(score, 0, "Возврат к диалогу")
        text = f"✅ Вы вернулись к **{name}**.\n\nПоследняя реплика:\n{history[-1]['content']}{footer}"

    if avatar_url:
        try:
            await callback.message.answer_photo(photo=avatar_url, caption=text, reply_markup=regen_kb(), parse_mode="Markdown")
            await callback.message.delete()
        except Exception:
            await callback.message.answer(text, reply_markup=regen_kb(), parse_mode="Markdown")
    else:
        await callback.message.answer(text, reply_markup=regen_kb(), parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "regenerate")
async def regenerate_reply_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    char = get_active_character(user_id)
    if not char:
        await callback.answer("Сначала выберите персонажа!", show_alert=True)
        return

    char_id, name, prompt, _, _, _ = char
    if not delete_last_assistant_message(user_id, char_id):
        await callback.answer("Нечего перегенерировать!", show_alert=True)
        return

    await callback.answer("Генерирую новый вариант...")
    current_score = get_affection(user_id, char_id)
    history = get_chat_history(user_id, char_id)
    user_profile = get_active_persona_text(user_id)

    clean_reply, mood, delta = await generate_ai_reply(prompt, history, current_score, user_profile)
    new_score = update_affection(user_id, char_id, delta)
    save_message(user_id, char_id, "assistant", clean_reply, delta=delta)

    full_text = f"{clean_reply}{format_affection_footer(new_score, delta, mood)}"
    try:
        await callback.message.edit_text(full_text, reply_markup=regen_kb(), parse_mode="Markdown")
    except Exception:
        await callback.message.edit_text(full_text, reply_markup=regen_kb())


@dp.message(F.text == "🔄 Сбросить диалог")
async def reset_chat(message: types.Message):
    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.
