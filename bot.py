
import os
import asyncio
import datetime
import json
import logging
import re
from typing import Dict, Any, Tuple, List

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI

# ╔═ ENV & LOG ═══════════════════════════════════╗
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("❌ .env not loaded или переменные окружения отсутствуют!")

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
client = OpenAI(api_key=OPENAI_API_KEY)

# ╔═ FILE PATHS ══════════════════════════════════╗
BASE = os.path.dirname(__file__)
USER_JSON = os.path.join(BASE, "user_data.json")
REM_JSON = os.path.join(BASE, "reminders.json")

user_data: Dict[str, Any] = json.load(open(USER_JSON, encoding="utf-8")) if os.path.exists(USER_JSON) else {}
reminders: List[Dict[str, Any]] = json.load(open(REM_JSON, encoding="utf-8")) if os.path.exists(REM_JSON) else []
user_ctx: Dict[int, list] = {}

# ╔═ I18N ════════════════════════════════════════╗
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

# ╔═ HELPERS ═════════════════════════════════════╗
def L(uid: int) -> str:
    return user_data.get(str(uid), {}).get("language", "RU")

def KB(l: str):
    t = T[l]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["lang"], callback_data="lang"), InlineKeyboardButton(t["style"], callback_data="style")],
        [InlineKeyboardButton(t["rem"], callback_data="rem"), InlineKeyboardButton(t["gen"], callback_data="gender")],
        [InlineKeyboardButton(t["prof"], callback_data="prof"), InlineKeyboardButton(t["clr"], callback_data="clear")],
    ])

# ╔═ PROMPT BUILDER ═══════════════════════════════╗
def build_prompt(uid: int) -> str:
    d = user_data.get(str(uid), {})
    style = d.get("style", "street")
    l = d.get("language", "RU")
    gender = d.get("gender")
    persona = d.get("persona", {})
    lines = [f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in persona.items()]
    if gender:
        lines.append("Пользователь — женщина." if gender == "female" and l == "RU" else ("User is female." if gender == "female" else "Пользователь — мужчина." if l == "RU" else "User is male."))
    extra = "\n".join(lines)
    mapping = {
        "street": "Ты дерзкий, но дружелюбный уличный бро. Мотивируй." if l == "RU" else "You are a street‑smart AI bro. Casual and motivating.",
        "psych": "Ты спокойный психолог‑ассистент. Эмпатия." if l == "RU" else "You are a calm psychological assistant. Empathic.",
        "coach": "Ты энергичный лайф‑коуч. Дай действия." if l == "RU" else "You are an energetic life coach. Action‑oriented.",
    }
    return mapping.get(style, mapping["street"]) + ("\n" + extra if extra else "")

# ╔═ PARSE DELAY ══════════════════════════════════╗
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
# ╔═ HANDLERS ═════════════════════════════════════╗
async def start(update: Update, _): await update.message.reply_text(T[L(update.effective_user.id)]["welcome"], reply_markup=KB(L(update.effective_user.id)))

async def on_buttons(update: Update, _):
    global user_data
    q = update.callback_query; await q.answer()
    uid, sid = q.from_user.id, str(q.from_user.id)
    l = L(uid); t = T[l]

    if q.data == "lang":
        await q.message.reply_text("🇷🇺 | 🇬🇧", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("RU", callback_data="lang_RU"), InlineKeyboardButton("EN", callback_data="lang_EN")]])); return
    if q.data.startswith("lang_"):
        user_data.setdefault(sid, {})["language"] = q.data.split("_")[1]
        json.dump(user_data, open(USER_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        await q.edit_message_text(T[L(uid)]["lang_set"], reply_markup=KB(L(uid))); return

    if q.data == "gender":
        k = [[InlineKeyboardButton("🚹 " + t["male"], callback_data="g_male"), InlineKeyboardButton("🚺 " + t["female"], callback_data="g_female"), InlineKeyboardButton("❌ " + t["skip"], callback_data="g_skip")]]
        await q.message.reply_text(t["q_gen"], reply_markup=InlineKeyboardMarkup(k)); return
    if q.data.startswith("g_"):
        g = q.data.split("_")[1]
        if g == "skip":
            user_data.setdefault(sid, {}).pop("gender", None)
            await q.message.reply_text(t["reset"])
        else:
            user_data.setdefault(sid, {})["gender"] = "female" if g == "female" else "male"
            await q.message.reply_text(t["saved"].format(t[g]))
        json.dump(user_data, open(USER_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2); return

    if q.data == "style":
        kb_s = [
            [InlineKeyboardButton(t["style_street"], callback_data="s_street")],
            [InlineKeyboardButton(t["style_psych"], callback_data="s_psych")],
            [InlineKeyboardButton(t["style_coach"], callback_data="s_coach")]
        ]
        await q.message.reply_text(t["choose_style"], reply_markup=InlineKeyboardMarkup(kb_s))
        return
    if q.data.startswith("s_"):
        user_data.setdefault(sid, {})["style"] = q.data.split("_")[1]
        json.dump(user_data, open(USER_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        await q.message.reply_text(t["style_ok"]); return

    if q.data == "rem":
        await q.message.reply_text(t["rem_fmt"]); return

    if q.data == "prof":
        d = user_data.get(sid, {})
        prof_lines = [f"{t['lang']}: {d.get('language', '-')}", f"{t['style']}: {d.get('style', '-')}", f"{t['gen']}: {t.get(d.get('gender'), '-') if d.get('gender') else '-'}"]
        await q.message.reply_text("\n".join(prof_lines)); return

    if q.data == "clear":
        user_data.pop(sid, None)
        json.dump(user_data, open(USER_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        await q.message.reply_text(t["cleared"]); return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, l = update.effective_user.id, L(update.effective_user.id)
    text = update.message.text

    delay = parse_delay(text, l)
    if delay:
        if isinstance(delay[0], datetime.datetime):
            dt, msg = delay
            reminders.append({"uid": uid, "at": dt.isoformat(), "msg": msg})
            json.dump(reminders, open(REM_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            await update.message.reply_text(T[l]["rem_save"].format(d=dt.strftime("%d.%m.%Y %H:%M"), m=msg))
            return
        reminders.append({"uid": uid, "at": (datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)).isoformat(), "msg": msg})
        json.dump(reminders, open(REM_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        formatted_time = f"{minutes} мин" if l == "RU" else f"{minutes} min"
        await update.message.reply_text(T[l]["rem_save"].format(d=formatted_time, m=msg))
        return

    prompt = build_prompt(uid)
    chat_history = user_ctx.setdefault(uid, [])
    chat_history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": prompt}] + chat_history[-10:]

    try:
        response = client.chat.completions.create(model="gpt-3.5-turbo", messages=messages)
        reply = response.choices[0].message.content
    except Exception as e:
        logging.error(f"OpenAI Error: {e}")
        reply = T[l]["err"]

    chat_history.append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

async def reminder_loop(app):
    while True:
        now = datetime.datetime.utcnow()
        due = [r for r in reminders if datetime.datetime.fromisoformat(r["at"]) <= now]
        for r in due:
            try:
                await app.bot.send_message(chat_id=r["uid"], text=f"⏰ {r['msg']}")
            except Exception as e:
                logging.error(f"Reminder send error: {e}")
        if due:
            reminders[:] = [r for r in reminders if r not in due]
            json.dump(reminders, open(REM_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        await asyncio.sleep(30)

def build_app():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

if __name__ == "__main__":
    app = build_app()
    asyncio.get_event_loop().create_task(reminder_loop(app))
    print("🤖 Bro 24/7 запущен …")
    app.run_polling(allowed_updates=Update.ALL_TYPES)