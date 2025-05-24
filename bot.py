import os
import asyncio
import datetime
import json
import logging
import re
from typing import Dict, Any, Tuple, List

import aiofiles
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.ext._application import Application
from telegram.constants import UpdateType
from openai import OpenAI

# ╔═ ENV & LOG ═══════════════════════════════════╗
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # Например, https://your-bot.onrender.com
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not WEBHOOK_HOST:
    raise RuntimeError("❌ .env not loaded или переменные окружения отсутствуют!")

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
client = OpenAI(api_key=OPENAI_API_KEY)

BASE = os.path.dirname(__file__)
USER_JSON = os.path.join(BASE, "user_data.json")
REM_JSON = os.path.join(BASE, "reminders.json")
CTX_JSON = os.path.join(BASE, "user_ctx.json")

def safe_load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Ошибка загрузки {path}: {e}")
        return default

async def async_save_json(path: str, data):
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))

user_data: Dict[str, Any] = safe_load_json(USER_JSON, {})
reminders: List[Dict[str, Any]] = safe_load_json(REM_JSON, [])
user_ctx: Dict[int, list] = safe_load_json(CTX_JSON, {})

T = {
    "RU": {
        "lang": "🌐 Язык", "style": "🎭 Стиль", "rem": "⏰ Напоминание", "gen": "🧬 Пол", "prof": "🧠 Профиль", "clr": "🧹 Сброс",
        "q_gen": "Кто ты по полу?", "saved": "Запомнил! Ты {}.", "reset": "Пол сброшен.",
        "male": "мужчина", "female": "женщина", "skip": "Не указывать",
        "lang_set": "Язык установлен ✅", "welcome": "👋 Привет! Я Bro 24/7 — всегда на связи.",
        "rem_fmt": "Формат: 'через 10мин ...' / 'через 2 часа ...'", "rem_bad": "Не понял формат.",
        "rem_save": "⏰ Напомню через {d}: {m}", "style_ok": "Стиль сохранён ✅", "cleared": "🧹 Очищено.",
        "err": "Ошибка. Попробуй ещё или /start.",
        "choose_style": "Выбери стиль общения:",
        "style_street": "🔥 Уличный бро",
        "style_psych": "🧘 Психолог",
        "style_coach": "💼 Коуч",
    },
    "EN": {
        "lang": "🌐 Language", "style": "🎭 Style", "rem": "⏰ Reminder", "gen": "🧬 Gender", "prof": "🧠 Profile", "clr": "🧹 Clear",
        "q_gen": "Your gender?", "saved": "Got it! You're {}.", "reset": "Gender cleared.",
        "male": "male", "female": "female", "skip": "Skip",
        "lang_set": "Language set ✅", "welcome": "👋 Hey! I'm Bro 24/7 — always online.",
        "rem_fmt": "Format: 'in 10min ...' / 'in 2 hours ...'", "rem_bad": "Bad format.",
        "rem_save": "⏰ I'll remind you in {d}: {m}", "style_ok": "Style saved ✅", "cleared": "🧹 Cleared.",
        "err": "Error. Try again or /start.",
        "choose_style": "Choose your style:",
        "style_street": "🔥 Street bro",
        "style_psych": "🧘 Psychologist",
        "style_coach": "💼 Coach",
    },
}

def L(uid: int) -> str:
    return user_data.get(str(uid), {}).get("language", "RU")

def KB(l: str):
    t = T[l]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["lang"], callback_data="lang"), InlineKeyboardButton(t["style"], callback_data="style")],
        [InlineKeyboardButton(t["rem"], callback_data="rem"), InlineKeyboardButton(t["gen"], callback_data="gender")],
        [InlineKeyboardButton(t["prof"], callback_data="prof"), InlineKeyboardButton(t["clr"], callback_data="clear")],
    ])

async def send_reply(msg_func, text: str, **kwargs):
    try:
        await msg_func(text, **kwargs)
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {e}")

def build_prompt(uid: int) -> str:
    d = user_data.get(str(uid), {})
    style = d.get("style", "street")
    l = d.get("language", "RU")
    gender = d.get("gender", "")
    persona = d.get("persona", {})
    name = d.get("name", "пользователь" if l == "RU" else "user")

    if l == "RU":
        gender_line = "женщина" if gender == "female" else "мужчина" if gender == "male" else "человек"
    else:
        gender_line = "female" if gender == "female" else "male" if gender == "male" else "person"

    traits = [f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in persona.items()]
    traits.append(f"Имя: {name}" if l == "RU" else f"Name: {name}")
    traits.append(f"Пол: {gender_line}" if l == "RU" else f"Gender: {gender_line}")
    extra = "".join(traits)

    if l == "RU":
        style_prompt = {
            "street": "Ты уличный бро. Говори просто, с юмором, можешь вставлять лёгкий сленг, немного неформальности. Главное — поддержка и уверенность.",
            "coach": "Ты коуч и наставник. Говоришь уверенно, мотивируешь, даёшь советы чётко и по делу.",
            "psych": "Ты психолог. Говоришь мягко, внимательно, с эмпатией. Помогаешь разобраться в чувствах, задаёшь наводящие вопросы."
        }
    else:
        style_prompt = {
            "street": "You're a street-style AI bro. Speak casually, with slang and humor. Be confident and supportive.",
            "coach": "You're a motivational coach. Speak clearly, confidently, and give concrete, action-oriented advice.",
            "psych": "You're an empathetic psychologist. Speak gently and attentively, help the user understand their emotions and thoughts."
        }

    return style_prompt.get(style, "") + "" + extra

async def ask_openai(uid: int, text: str) -> str:
    prompt = build_prompt(uid)
    chat_history = user_ctx.setdefault(uid, [])
    chat_history.append({"role": "user", "content": text})
    user_ctx[uid] = chat_history[-12:]
    messages = [{"role": "system", "content": prompt}] + user_ctx[uid]
    try:
        response = client.chat.completions.create(model="gpt-3.5-turbo", messages=messages)
        reply = response.choices[0].message.content
    except Exception as e:
        logging.error(f"OpenAI Error: {e}")
        reply = T[L(uid)]["err"]
    chat_history.append({"role": "assistant", "content": reply})
    user_ctx[uid] = chat_history[-12:]
    await async_save_json(CTX_JSON, user_ctx)
    return reply

R_MIN_RU = re.compile(r"через\s+(\d+)\s*мин(?:ут[ыу]?)?\s+(.*)", re.I)
R_HR_RU = re.compile(r"через\s+(\d+)\s*час(?:а|ов)?\s+(.*)", re.I)
R_MIN_EN = re.compile(r"in\s+(\d+)\s*min(?:s|utes)?\s+(.*)", re.I)
R_HR_EN = re.compile(r"in\s+(\d+)\s*hour(?:s)?\s+(.*)", re.I)
R_DATE_TIME = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})\s+(\d{2}):(\d{2})\s+(.*)")

def parse_delay(text: str, l: str) -> Tuple[int | datetime.datetime, str] | None:
    if (m := R_DATE_TIME.search(text)):
        try:
            dt = datetime.datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]))
            msg = m[6].strip()
            return dt, msg or ("Без текста" if l == "RU" else "No text")
        except: return None

    m = (R_MIN_RU.search(text) or R_HR_RU.search(text)) if l == "RU" else (R_MIN_EN.search(text) or R_HR_EN.search(text))
    if not m: return None
    num = int(m.group(1))
    minutes = num * 60 if 'hour' in m.re.pattern or 'час' in m.re.pattern else num
    return minutes, m.group(2).strip() or ("Без текста" if l == "RU" else "No text")

async def start(update: Update, _):
    await send_reply(update.message.reply_text, T[L(update.effective_user.id)]["welcome"], reply_markup=KB(L(update.effective_user.id)))

async def on_buttons(update: Update, _):
    q = update.callback_query
    await q.answer()
    uid, sid = q.from_user.id, str(q.from_user.id)
    l = L(uid)
    t = T[l]

    if q.data == "lang":
        await send_reply(q.message.reply_text, "🇷🇺 | 🇬🇧", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("RU", callback_data="lang_RU"), InlineKeyboardButton("EN", callback_data="lang_EN")]]))
    elif q.data.startswith("lang_"):
        user_data.setdefault(sid, {})["language"] = q.data.split("_")[1]
        await async_save_json(USER_JSON, user_data)
        await q.edit_message_text(T[L(uid)]["lang_set"], reply_markup=KB(L(uid)))

    elif q.data == "gender":
        k = [[InlineKeyboardButton("🚹 " + t["male"], callback_data="g_male"), InlineKeyboardButton("🚺 " + t["female"], callback_data="g_female"), InlineKeyboardButton("❌ " + t["skip"], callback_data="g_skip")]]
        await send_reply(q.message.reply_text, t["q_gen"], reply_markup=InlineKeyboardMarkup(k))
    elif q.data.startswith("g_"):
        g = q.data.split("_")[1]
        if g == "skip":
            user_data.setdefault(sid, {}).pop("gender", None)
            await send_reply(q.message.reply_text, t["reset"])
        else:
            user_data.setdefault(sid, {})["gender"] = "female" if g == "female" else "male"
            await send_reply(q.message.reply_text, t["saved"].format(t[g]))
        await async_save_json(USER_JSON, user_data)

    elif q.data == "style":
        kb_s = [
            [InlineKeyboardButton(t["style_street"], callback_data="s_street")],
            [InlineKeyboardButton(t["style_psych"], callback_data="s_psych")],
            [InlineKeyboardButton(t["style_coach"], callback_data="s_coach")]
        ]
        await send_reply(q.message.reply_text, t["choose_style"], reply_markup=InlineKeyboardMarkup(kb_s))
    elif q.data.startswith("s_"):
        user_data.setdefault(sid, {})["style"] = q.data.split("_")[1]
        await async_save_json(USER_JSON, user_data)
        await send_reply(q.message.reply_text, t["style_ok"])

    elif q.data == "rem":
        await send_reply(q.message.reply_text, t["rem_fmt"])

    elif q.data == "prof":
        d = user_data.get(sid, {})
        prof_lines = [f"{t['lang']}: {d.get('language', '-')}", f"{t['style']}: {d.get('style', '-')}", f"{t['gen']}: {t.get(d.get('gender'), '-') if d.get('gender') else '-'}"]
        await send_reply(q.message.reply_text, "\n".join(prof_lines))

    elif q.data == "clear":
        user_data.pop(sid, None)
        await async_save_json(USER_JSON, user_data)
        await send_reply(q.message.reply_text, t["cleared"])

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, l = update.effective_user.id, L(update.effective_user.id)
    text = update.message.text

    delay = parse_delay(text, l)
    if delay:
        if isinstance(delay[0], datetime.datetime):
            dt, msg = delay
            reminders.append({"uid": uid, "at": dt.isoformat(), "msg": msg})
            await async_save_json(REM_JSON, reminders)
            await send_reply(update.message.reply_text, T[l]["rem_save"].format(d=dt.strftime("%d.%m.%Y %H:%M"), m=msg))
            return
        elif isinstance(delay, tuple) and len(delay) == 2:
            minutes, msg = delay
            at_time = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=minutes)).isoformat()
            reminders.append({"uid": uid, "at": at_time, "msg": msg})
            await async_save_json(REM_JSON, reminders)
            formatted_time = f"{minutes} МИН" if l == "RU" else f"{minutes} MIN"
            await send_reply(update.message.reply_text, T[l]["rem_save"].format(d=formatted_time, m=msg))
            return

    reply = await ask_openai(uid, text)
    await send_reply(update.message.reply_text, reply)

async def reminder_loop(app):
    while True:
        now = datetime.datetime.now(datetime.UTC)
        due = [r for r in reminders if datetime.datetime.fromisoformat(r["at"]) <= now]
        for r in due:
            try:
                await app.bot.send_message(chat_id=r["uid"], text=f"⏰ {r['msg']}")
            except Exception as e:
                logging.error(f"Reminder send error: {e}")
        if due:
            for r in due:
                reminders.remove(r)
            await async_save_json(REM_JSON, reminders)
        await asyncio.sleep(30)

async def post_start(app):
    await asyncio.sleep(5)
    asyncio.create_task(reminder_loop(app))
    print("🤖 Bro 24/7 запущен …")

def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_start)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

# ---- FLASK WEBHOOK ----
app_flask = Flask(__name__)
tg_app: Application = build_app()

@app_flask.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    update = Update.de_json(request.get_json(force=True), tg_app.bot)
    tg_app.update_queue.put_nowait(update)
    return "ok", 200

@app_flask.route("/", methods=["GET"])
def root():
    return "Bro 24/7 is alive!", 200

def setup_webhook():
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    webhook_url = WEBHOOK_URL
    r = requests.post(url, json={"url": webhook_url})
    print(f"Webhook setup response: {r.text}")

if __name__ == "__main__":
    import sys

    # Установка webhook один раз (или при каждом старте)
    setup_webhook()
    # Запуск Flask сервера
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))



# ╔═ ДОПОЛНЕНИЯ И УЛУЧШЕНИЯ ════════════════════════════════════════════════════════════════════════════╗

# Закрытие соединений (если будут использоваться базы SQLite в будущем)
def close_connections():
    logging.info("Закрытие соединений и сохранение данных...")
    import atexit
    atexit.register(lambda: asyncio.run(async_save_json(USER_JSON, user_data)))
    atexit.register(lambda: asyncio.run(async_save_json(REM_JSON, reminders)))
    atexit.register(lambda: asyncio.run(async_save_json(CTX_JSON, user_ctx)))

close_connections()

# Дополнительная проверка переменных окружения с отдельными ошибками
required_env_vars = ["TELEGRAM_TOKEN", "OPENAI_API_KEY", "WEBHOOK_HOST"]
for var in required_env_vars:
    if not os.getenv(var):
        raise RuntimeError(f"❌ Переменная окружения {var} не установлена!")

# Безопасность: защита от SQL-инъекций в потенциальных динамических полях (если будут)
SAFE_FIELDS = {"style", "language", "gender", "persona", "name"}
def is_safe_field(field: str) -> bool:
    return field in SAFE_FIELDS

# Кэширование (пример — можно развить в будущем)
user_cache = {}
def get_user_cached(uid: str):
    if uid in user_cache:
        return user_cache[uid]
    user = user_data.get(uid, {})
    user_cache[uid] = user
    return user

# Пример заготовки под миграции (в будущем — через Alembic)
def run_db_migrations():
    logging.info("Проверка и применение миграций… (в будущем подключить Alembic)")

run_db_migrations()
