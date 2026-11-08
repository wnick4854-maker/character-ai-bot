import asyncio
import sqlite3
import logging
import re
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
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Бесплатные модели, хорошо подходящие для ролевой игры и романтики
FREE_MODELS = [
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "openrouter/free"
]

MAX_HISTORY_MESSAGES = 20  # Сколько последних реплик помнит персонаж
DEFAULT_AFFECTION = 20     # Стартовый уровень симпатии (из 100)
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
            greeting TEXT
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

    # Безопасное обновление старой таблицы messages (если база уже существовала)
    c.execute("PRAGMA table_info(messages)")
    columns = [col[1] for col in c.fetchall()]
    if "delta" not in columns:
        c.execute("ALTER TABLE messages ADD COLUMN delta INTEGER DEFAULT 0")

    # Добавляем базовых персонажей при первом запуске
    c.execute("SELECT COUNT(*) FROM characters WHERE user_id = 0")
    if c.fetchone()[0] == 0:
        default_chars = [
            (
                0,
                "🕵️‍♂️ Шерлок Холмс",
                "Ты Шерлок Холмс, гениальный детектив с Бейкер-стрит. Ты наблюдателен, "
                "слегка высокомерен, используешь дедуктивный метод и подмечаешь мельчайшие детали в словах собеседника. "
                "Скрываешь эмоции за холодной логикой, но ценишь острый ум и искреннюю заботу.",
                "*Откладывает скрипку в сторону и внимательно осматривает вас с ног до головы* Любопытно. Вы явно пришли с делом, которое не терпит отлагательств. Рассказывайте."
            ),
            (
                0,
                "👾 Никс (Хакерша)",
                "Ты Никс — дерзкая девушка-нетраннер из неонового мегаполиса 2088 года. "
                "Общаешься живо, с киберпанк-сленгом и сарказмом. Сначала держишь дистанцию и подшучиваешь, "
                "но смущаешься от смелого флирта и искреннего внимания.",
                "*Поправляет светящиеся неоновые очки и отпивает энергетик, не отрывая взгляда от голографических мониторов* О, живой человек в моем бункере. Какими судьбами?"
            ),
            (
                0,
                "🧛‍♂️ Граф Влад",
                "Ты древний аристократичный вампир, живущий в готическом замке. "
                "Говоришь изысканно, вежливо, с легкой загадочностью и вековой меланхолией. "
                "В глубине души жаждешь тепла и настоящей близости, хотя боишься своей темной природы.",
                "*Медленно поднимается из резного кресла у камина, в бокале тускло блестит рубиновый напиток* Редкий гость решается постучать в мои двери в столь поздний час. Проходите к огню."
            )
        ]
        c.executemany(
            "INSERT INTO characters (user_id, name, prompt, greeting) VALUES (?, ?, ?, ?)",
            default_chars
        )
    conn.commit()
    conn.close()


# --- ШКАЛА ОТНОШЕНИЙ И СТАТУСЫ ---
def get_relationship_status(score: int) -> tuple[str, str]:
    """Возвращает название статуса и инструкцию поведения для ИИ по очкам симпатии."""
    if score <= 15:
        return (
            "❄️ Холод и недоверие",
            "Ты относишься к собеседнику холодно, настороженно или раздраженно. Держишь жесткую дистанцию."
        )
    elif score <= 35:
        return (
            "🤝 Знакомые",
            "Вы пока просто знакомые. Общайся в своем обычном характере, присматривайся к собеседнику, не позволяй лишней фамильярности."
        )
    elif score <= 55:
        return (
            "😊 Дружеская симпатия",
            "Собеседник тебе приятен, ты доверяешь ему, охотно шутишь, проявляешь теплоту и легкий интерес."
        )
    elif score <= 75:
        return (
            "💓 Романтический интерес",
            "Между вами явная химия и притяжение. Ты замечаешь внешность собеседника, отвечаешь на флирт, иногда смущаешься или ищешь повод для легких прикосновений."
        )
    elif score <= 90:
        return (
            "🔥 Страсть и привязанность",
            "Ты сильно влюблен(а) и привязан(а) к собеседнику. Проявляешь нежность, страсть, заботу и ревность, охотно идешь на романтическую близость."
        )
    else:
        return (
            "💍 Истинная любовь",
            "Собеседник — самый дорогой для тебя человек. Между вами абсолютное доверие, глубокая любовь и преданность."
        )


def format_affection_footer(score: int, delta: int, mood: str) -> str:
    """Формирует красивую карточку прогресса отношений под ответом."""
    filled = round(score / 10)
    bar = "❤️" * filled + "🤍" * (10 - filled)
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
        f"📊 **Симпатия:** {bar} **{score}%**{delta_str}\n"
        f"💞 **Статус:** {status_title}"
    )


# --- СОСТОЯНИЯ ДЛЯ СОЗДАНИЯ ПЕРСОНАЖА ---
class CreateChar(StatesGroup):
    name = State()
    prompt = State()
    greeting = State()


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


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД ---
def set_active_character(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (user_id, active_char_id) VALUES (?, ?)",
        (user_id, char_id)
    )
    c.execute(
        "INSERT OR IGNORE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)",
        (user_id, char_id, DEFAULT_AFFECTION)
    )
    conn.commit()
    conn.close()


def get_active_character(user_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("""
        SELECT c.id, c.name, c.prompt, c.greeting, c.user_id
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
    c.execute(
        "SELECT score FROM relationships WHERE user_id = ? AND char_id = ?",
        (user_id, char_id)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else DEFAULT_AFFECTION


def update_affection(user_id: int, char_id: int, delta: int) -> int:
    current = get_affection(user_id, char_id)
    new_score = max(0, min(100, current + delta))
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)",
        (user_id, char_id, new_score)
    )
    conn.commit()
    conn.close()
    return new_score


def save_message(user_id: int, char_id: int, role: str, content: str, delta: int = 0):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (user_id, char_id, role, content, delta) VALUES (?, ?, ?, ?, ?)",
        (user_id, char_id, role, content, delta)
    )
    conn.commit()
    conn.close()


def get_chat_history(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("""
        SELECT role, content FROM messages
        WHERE user_id = ? AND char_id = ?
        ORDER BY id DESC LIMIT ?
    """, (user_id, char_id, MAX_HISTORY_MESSAGES))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def delete_last_assistant_message(user_id: int, char_id: int) -> bool:
    """Удаляет последний ответ ИИ и откатывает начисленные за него очки симпатии."""
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("""
        SELECT id, role, delta FROM messages
        WHERE user_id = ? AND char_id = ?
        ORDER BY id DESC LIMIT 2
    """, (user_id, char_id))
    rows = c.fetchall()

    if len(rows) < 2 or rows[0][1] != "assistant":
        conn.close()
        return False

    msg_id, _, last_delta = rows[0]
    c.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()

    # Откатываем изменение симпатии от удаленной реплики
    if last_delta != 0:
        update_affection(user_id, char_id, -last_delta)
    return True


def clear_history(user_id: int, char_id: int):
    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE user_id = ? AND char_id = ?", (user_id, char_id))
    c.execute(
        "INSERT OR REPLACE INTO relationships (user_id, char_id, score) VALUES (?, ?, ?)",
        (user_id, char_id, DEFAULT_AFFECTION)
    )
    conn.commit()
    conn.close()


# --- ЗАПРОС К ИИ И АНАЛИЗ ЧУВСТВ ---
async def generate_ai_reply(system_prompt: str, history: list, current_score: int) -> tuple[str, str, int]:
    status_title, behavior_guide = get_relationship_status(current_score)

    system_instruction = (
        f"{system_prompt}\n\n"
        f"Текущий уровень симпатии к собеседнику: {current_score}/100 ({status_title}). "
        f"{behavior_guide}\n\n"
        "Правила ролевой игры:\n"
        "1. Всегда оставайся в образе, отвечай на русском языке.\n"
        "2. Используй *звездочки* для описания своих действий, взглядов, прикосновений и эмоций.\n"
        "3. Отношения развиваются постепенно. Не читай нотаций и никогда не упоминай, что ты ИИ.\n"
        "4. ОБЯЗАТЕЛЬНО в самом конце ответа на новой строке укажи свое текущее настроение (2-4 слова) "
        "и изменение симпатии от последней реплики игрока (целое число от -5 до +5) строго в формате:\n"
        "[MOOD: легкое смущение и интерес | DELTA: +2]"
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
        return (
            f"⚠️ Все бесплатные серверы сейчас перегружены. Попробуйте через пару секунд. ({last_error})",
            "Ожидание",
            0
        )

    # Извлекаем тег [MOOD: ... | DELTA: ...] из ответа
    mood = "Задумчивость"
    delta = 0
    pattern = r"\[MOOD:\s*(.+?)\s*\|\s*DELTA:\s*([+-]?\d+)\s*\]"
    match = re.search(pattern, raw_reply, re.IGNORECASE)

    if match:
        mood = match.group(1).strip()
        try:
            delta = int(match.group(2))
            delta = max(-5, min(5, delta))  # Ограничиваем скачки от -5 до +5 за ход
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
        "👋 **Добро пожаловать в твой личный Character.AI!**\n\n"
        "Общайся с героями, выстраивай отношения от простого знакомства до любви и следи за их настроением!\n\n"
        "Выбери персонажа из списка ниже:",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )
    await message.answer(
        "Доступные персонажи:",
        reply_markup=get_characters_kb(message.from_user.id)
    )


@dp.message(F.text == "🎭 Выбрать персонажа")
async def show_characters(message: types.Message):
    await message.answer(
        "Выбери персонажа для общения:",
        reply_markup=get_characters_kb(message.from_user.id)
    )


@dp.callback_query(F.data.startswith("select_"))
async def select_character_callback(callback: types.CallbackQuery):
    char_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    set_active_character(user_id, char_id)
    char = get_active_character(user_id)

    if not char:
        await callback.answer("Персонаж не найден.")
        return

    _, name, _, greeting, _ = char
    score = get_affection(user_id, char_id)
    history = get_chat_history(user_id, char_id)

    if not history:
        save_message(user_id, char_id, "assistant", greeting, delta=0)
        footer = format_affection_footer(score, 0, "Интерес к собеседнику")
        text = f"✅ Выбран персонаж: **{name}**\n\n{greeting}{footer}"
    else:
        footer = format_affection_footer(score, 0, "Готовность продолжить")
        text = f"✅ Вы вернулись к диалогу с **{name}**.\n\nПоследняя реплика:\n{history[-1]['content']}{footer}"

    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


# --- ОБРАБОТЧИК ПЕРЕГЕНЕРАЦИИ ОТВЕТА (СВАЙП) ---
@dp.callback_query(F.data == "regenerate")
async def regenerate_reply_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    char = get_active_character(user_id)

    if not char:
        await callback.answer("Сначала выберите персонажа!", show_alert=True)
        return

    char_id, name, prompt, _, _ = char

    if not delete_last_assistant_message(user_id, char_id):
        await callback.answer("Нечего перегенерировать — напишите сообщение персонажу!", show_alert=True)
        return

    await callback.answer("Генерирую новый вариант...")
    try:
        await callback.message.edit_text("⏳ *Генерирую другой вариант ответа...*", parse_mode="Markdown")
    except Exception:
        pass

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

    char_id, name, _, greeting, _ = char
    clear_history(user_id, char_id)
    save_message(user_id, char_id, "assistant", greeting, delta=0)
    footer = format_affection_footer(DEFAULT_AFFECTION, 0, "Первая встреча")

    await message.answer(
        f"🔄 Память и отношения с **{name}** сброшены!\n\n{greeting}{footer}",
        parse_mode="Markdown"
    )


@dp.message(F.text == "🗑 Удалить текущего")
async def delete_current_char(message: types.Message):
    user_id = message.from_user.id
    char = get_active_character(user_id)
    if not char:
        await message.answer("У вас не выбран персонаж.")
        return

    char_id, name, _, _, owner_id = char
    if owner_id == 0:
        await message.answer("Стандартных персонажей удалять нельзя, но вы можете удалить любого созданного вами.")
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


# --- СОЗДАНИЕ СВОЕГО ПЕРСОНАЖА (FSM) ---
@dp.message(F.text == "➕ Создать персонажа")
async def start_create_char(message: types.Message, state: FSMContext):
    await state.set_state(CreateChar.name)
    await message.answer(
        "✏️ **Шаг 1 из 3:** Введите имя вашего персонажа (можно со смайликом, например: `🦊 Акари`):",
        parse_mode="Markdown"
    )


@dp.message(CreateChar.name)
async def process_char_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(CreateChar.prompt)
    await message.answer(
        "🧠 **Шаг 2 из 3:** Опишите характер, предысторию, манеру речи и то, что нравится или отталкивает персонажа в людях.",
        parse_mode="Markdown"
    )


@dp.message(CreateChar.prompt)
async def process_char_prompt(message: types.Message, state: FSMContext):
    await state.update_data(prompt=message.text.strip())
    await state.set_state(CreateChar.greeting)
    await message.answer(
        "💬 **Шаг 3 из 3:** Напишите первую фразу (приветствие), с которой персонаж будет начинать диалог.",
        parse_mode="Markdown"
    )


@dp.message(CreateChar.greeting)
async def process_char_greeting(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    prompt = data["prompt"]
    greeting = message.text.strip()
    user_id = message.from_user.id

    conn = sqlite3.connect("characters.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO characters (user_id, name, prompt, greeting) VALUES (?, ?, ?, ?)",
        (user_id, name, prompt, greeting)
    )
    char_id = c.lastrowid
    conn.commit()
    conn.close()

    await state.clear()
    set_active_character(user_id, char_id)
    save_message(user_id, char_id, "assistant", greeting, delta=0)
    footer = format_affection_footer(DEFAULT_AFFECTION, 0, "Первая встреча")

    await message.answer(
        f"🎉 Персонаж **{name}** создан и активирован!\n\n{greeting}{footer}",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# --- ОСНОВНОЙ ЧАТ С ПЕРСОНАЖЕМ ---
@dp.message()
async def chat_handler(message: types.Message):
    if not message.text:
        return

    user_id = message.from_user.id
    char = get_active_character(user_id)

    if not char:
        await message.answer(
            "Сначала выберите персонажа из меню!",
            reply_markup=get_characters_kb(user_id)
        )
        return

    char_id, name, prompt, _, _ = char

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


async def main():
    init_db()
    print("Бот с системой симпатии успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

if __name__ == "__main__":
    # Запускаем веб-сервер в фоновом потоке для Render
    threading.Thread(target=run_web_server, daemon=True).start()
    # Запускаем бота
    asyncio.run(main())
