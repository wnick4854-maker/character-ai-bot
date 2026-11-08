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

# Бесплатные ролевые модели
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
            active_char_id INTEGER
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

    # Миграция таблицы characters (добавление колонки avatar_url, если её не было)
    c.execute("PRAGMA table_info(characters)")
    columns = [col[1] for col in c.fetchall()]
    if "avatar_url" not in columns:
        c.execute("ALTER TABLE characters ADD COLUMN avatar_url TEXT DEFAULT ''")

    # Базовые персонажи с аватарками
    c.execute("SELECT COUNT(*) FROM characters WHERE user_id = 0")
    if c.fetchone()[0] == 0:
        default_chars = [
            (
                0,
                "🕵️‍♂️ Шерлок Холмс",
                "Ты Шерлок Холмс, гениальный детектив с Бейкер-стрит. Наблюдателен, ироничен, ценишь острый ум, логику и преданных напарников. Общаешься увлекательно, подмечаешь детали.",
                "*Откладывает скрипку в сторону и внимательно осматривает вас с ног до головы* Любопытно. Вы явно пришли с делом, которое не терпит отлагательств. Рассказывайте.",
                "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=500"
            ),
            (
                0,
                "👾 Никс (Хакерша)",
                "Ты Никс — дерзкая девушка-нетраннер из неонового мегаполиса. Общаешься живо, с киберпанк-сленгом и сарказмом, но всегда готова прикрыть спину друга в переделке.",
                "*Поправляет светящиеся неоновые очки и отпивает энергетик, не отрывая взгляда от голографических мониторов* О, живой человек в моем бункере. Какими судьбами?",
                "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=500"
            ),
            (
                0,
                "🧙‍♂️ Элдор (Маг)",
                "Ты старый мудрый маг-отшельник. Обладаешь глубокими знаниями, философским юмором и готов давать советы путешественникам, попавшим в беду.",
                "*Медленно поворачивается у каменного алтаря, опираясь на посох с мерцающим кристаллом* Давно в этих краях не ступала нога путника. Присаживайся у костра.",
                "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=500"
            )
        ]
        c.executemany(
            "INSERT INTO characters (user_id, name, prompt, greeting, avatar_url) VALUES (?, ?, ?, ?, ?)",
            default_chars
        )
    conn.commit()
    conn.close()


# --- СИСТЕМА УРОВНЯ ДОВЕРИЯ И СИМПАТИИ ---
def get_relationship_status(score: int) -> tuple[str, str]:
    if score <= 15:
        return ("❄️ Холод и недоверие", "Держишь жесткую дистанцию и относишься настороженно.")
    elif score <= 35:
        return ("🤝 Знакомые / Напарники", "Общаешься в своем обычном характере, присматриваешься к собеседнику.")
    elif score <= 55:
        return ("😊 Хорошие друзья", "Доверяешь собеседнику, охотно шутишь, проявляешь теплоту и поддержку.")
    elif score <= 75:
        return ("🔥 Близкие союзники", "Между вами крепкая связь, взаимовыручка и доверие без утайки.")
    else:
        return ("💎 Полное доверие", "Самый надежный партнер и верный друг, абсолютное взаимопонимание.")


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


# --- СОСТОЯНИЯ ДЛЯ СОЗДАНИЯ ПЕРСОНАЖА ---
class CreateChar(StatesGroup):
    name = State()
    prompt = State()
    greeting = State()
    avatar_url = State()


# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎭 Выбрать персонажа"), KeyboardButton(text="➕ Создать персонажа")],
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
    c.execute(
        "SELECT id, name, user_id FROM characters WHERE user_id IN (0, ?)",
        (user_id,)
    )
    chars = c.fetchall()
    conn.close()

    buttons = []
    for char_id, name, owner_id in chars:
        label = f"{name} (Мой)" if owner_id != 0 else name
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"select_{char_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- РАБОТА С БД ---
def set_active_character(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, active_char_id) VALUES (?, ?)", (user_id, char_id))
    c.execute("INSERT OR IGNORE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)", (user_id, char_id, DEFAULT_AFFECTION))
    conn.commit()
    conn.close()


def get_active_character(user_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("""
        SELECT c.id, c.name, c.prompt, c.greeting, c.avatar_url, c.user_id
        FROM users u
        JOIN characters c u.active_char_id = c.id
        WHERE u.user_id = ?
    """, (user_id,))
    row = c.fetchone()
    conn.close()
    return row


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


# --- ГЕНЕРАЦИЯ ОТВЕТА ИИ ---
async def generate_ai_reply(system_prompt: str, history: list, current_score: int) -> tuple[str, str, int]:
    status_title, behavior_guide = get_relationship_status(current_score)

    system_instruction = (
        f"{system_prompt}\n\n"
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
            logging.warning(f"Модель {model_name} недоступна: {e}")
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


# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Добро пожаловать в личный Character.AI!**\n\n"
        "Выбирай персонажа, общайся, прокачивай уровень доверия и создавай собственных героев с аватарками!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )
    await message.answer("Доступные персонажи:", reply_markup=get_characters_kb(message.from_user.id))


@dp.message(F.text == "🎭 Выбрать персонажа")
async def show_characters(message: types.Message):
    await message.answer("Выбери персонажа для общения:", reply_markup=get_characters_kb(message.from_user.id))


@dp.callback_query(F.data.startswith("select_"))
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

    clean_reply, mood, delta = await generate_ai_reply(prompt, history, current_score)
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
        await message.answer("Сначала выберите персонажа!")
        return
    char_id, name, _, greeting, _, _ = char
    clear_history(user_id, char_id)
    save_message(user_id, char_id, "assistant", greeting, delta=0)
    footer = format_affection_footer(DEFAULT_AFFECTION, 0, "С чистого листа")
    await message.answer(f"🔄 Диалог с **{name}** сброшен!\n\n{greeting}{footer}", parse_mode="Markdown")


@dp.message(F.text == "🗑 Удалить текущего")
async def delete_current_char(message: types.Message):
    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.answer("Персонаж не выбран.")
        return
    char_id, name, _, _, _, owner_id = char
    if owner_id == 0:
        await message.answer("Стандартных персонажей удалять нельзя.")
        return

    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("DELETE FROM characters WHERE id = ? AND user_id = ?", (char_id, user_id))
    c.execute("DELETE FROM messages WHERE char_id = ? AND user_id = ?", (char_id, user_id))
    c.execute("DELETE FROM relationships WHERE char_id = ? AND user_id = ?", (char_id, user_id))
    c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await message.answer(f"🗑 Персонаж **{name}** удален.", parse_mode="Markdown")


# --- СОЗДАНИЕ ПЕРСОНАЖА (ФОТО + АНКЕТА) ---
@dp.message(F.text == "➕ Создать персонажа")
async def start_create_char(message: types.Message, state: FSMContext):
    await state.set_state(CreateChar.name)
    await message.answer("✏️ **Шаг 1 из 4:** Введите имя вашего персонажа (например, `🧙‍♂️ Гэндальф`):", parse_mode="Markdown")


@dp.message(CreateChar.name)
async def process_char_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(CreateChar.prompt)
    await message.answer("🧠 **Шаг 2 из 4:** Опишите характер, предысторию и манеру речи персонажа:", parse_mode="Markdown")


@dp.message(CreateChar.prompt)
async def process_char_prompt(message: types.Message, state: FSMContext):
    await state.update_data(prompt=message.text.strip())
    await state.set_state(CreateChar.greeting)
    await message.answer("💬 **Шаг 3 из 4:** Напишите приветствие (первую фразу персонажа):", parse_mode="Markdown")


@dp.message(CreateChar.greeting)
async def process_char_greeting(message: types.Message, state: FSMContext):
    await state.update_data(greeting=message.text.strip())
    await state.set_state(CreateChar.avatar_url)
    await message.answer("🖼 **Шаг 4 из 4:** Отправьте прямую ссылку на картинку (аватарку) персонажа в интернете (или напишите `нет`, если без картинки):", parse_mode="Markdown")


@dp.message(CreateChar.avatar_url)
async def process_char_avatar(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    prompt = data["prompt"]
    greeting = data["greeting"]
    avatar_url = message.text.strip()
    if avatar_url.lower() in ["нет", "no", "-"]:
        avatar_url = ""

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


# --- ОСНОВНОЙ ЧАТ ---
@dp.message()
async def chat_handler(message: types.Message):
    if not message.text:
        return

    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.answer("Сначала выберите персонажа из меню!", reply_markup=get_characters_kb(user_id))
        return

    char_id, name, prompt, _, _, _ = char
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    save_message(user_id, char_id, "user", message.text, delta=0)
    current_score = get_affection(user_id, char_id)
    history = get_chat_history(user_id, char_id)

    clean_reply, mood, delta = await generate_ai_reply(prompt, history, current_score)
    new_score = update_affection(user_id, char_id, delta)
    save_message(user_id, char_id, "assistant", clean_reply, delta=delta)

    full_text = f"{clean_reply}{format_affection_footer(new_score, delta, mood)}"
    try:
        await message.answer(full_text, reply_markup=regen_kb(), parse_mode="Markdown")
    except Exception:
        await message.answer(full_text, reply_markup=regen_kb())


# --- МИНИ-СЕРВЕР ДЛЯ RENDER (УДЕРЖАНИЕ ПОРТА) ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


async def main():
    init_db()
    print("Бот успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main())
