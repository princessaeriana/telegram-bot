# ================================================
# Baddietootall — Telegram Bot (Sexting prototype)
# Render-ready • Python 3 • Long polling
# Storage: local JSON files (state.json, admins.json)
# ================================================
#
# TEXT FLOW (no image)
# /start → age check (18+?) → main menu (only Sexting enabled for test)
#   Sexting:
#     → pick tier: premade ($40) | live ($50) | custom ($60)
#     → add-ons loop (parse text; updates running total)
#        - user says "no" → summary → forward to admin → send content link
#
# Commands:
#   /start      — start fresh
#   /help       — quick help
#   /cancel     — cancel current flow
#   /iamadmin   — run in your admin group (or DM) to capture chat_id for forwarding
#
# Smart filters:
#   - scam talk → gentle warning
#   - explicit words → only warn if off-topic (not in sexting steps)
#
# Timeouts:
#   - warn after 10 minutes silence
#   - stop after 30 minutes (can /start again)
#
# Env Vars (set on Render):
#   BOT_TOKEN
#   ADMIN_USERNAME=baddietootall
#   ADMIN_CHAT_LINK=https://t.me/+caNaUBh7idhlNmNh
#   CONTENT_CHANNEL_LINK=https://t.me/+I7tI60WNJb8yZGM8
#
# After deploy:
#   1) Add the bot to your admin group on Telegram.
#   2) In that group, send /iamadmin  → bot stores chat_id for forwards.

import os
import re
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

from cachetools import TTLCache
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------
# Settings / Constants
# -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "baddietootall").lstrip("@")
ADMIN_CHAT_LINK = os.getenv("ADMIN_CHAT_LINK", "").strip()
CONTENT_CHANNEL_LINK = os.getenv("CONTENT_CHANNEL_LINK", "").strip()

DATA_FILE = "state.json"
ADMINS_FILE = "admins.json"

WARN_AFTER_MIN = 10    # send “you still there?” after 10m
STOP_AFTER_MIN = 30    # mark idle after 30m

STATE_AGE = "age"
STATE_MENU = "menu"
STATE_SEXTING_TIER = "sexting_tier"
STATE_SEXTING_ADDONS = "sexting_addons"
STATE_IDLE = "idle"

SEX_BASE = {"premade": 40.0, "live": 50.0, "custom": 60.0}

SCAM_PHRASES = [
    "pay after", "send first", "prove you’re real", "prove you are real",
    "been scammed", "always get scammed", "not paying", "won't pay",
    "cannot pay", "cant pay", "can’t pay"
]
EXPLICIT_WORDS = [
    "dick","cock","penis","pussy","vagina","tits","boobs","nude","naked",
    "fuck","sex","horny","masturbate","cum","orgasm","blowjob","anal"
]

# throttle repeated warnings
warn_cache = TTLCache(maxsize=10000, ttl=60)  # 60s per user per warning-key

# -------------------
# Tiny persistence
# -------------------
def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

if not os.path.exists(DATA_FILE):
    _save_json(DATA_FILE, {})
if not os.path.exists(ADMINS_FILE):
    _save_json(ADMINS_FILE, {})

def get_admin_chat_id() -> Optional[int]:
    data = _load_json(ADMINS_FILE, {})
    cid = data.get("admin_chat_id")
    return int(cid) if cid else None

def set_admin_chat_id(chat_id: int):
    data = _load_json(ADMINS_FILE, {})
    data["admin_chat_id"] = chat_id
    _save_json(ADMINS_FILE, data)

def get_state(user_id: int) -> Dict[str, Any]:
    data = _load_json(DATA_FILE, {})
    return data.get(str(user_id), {
        "step": STATE_IDLE,
        "age_ok": None,
        "service": None,
        "sexting": {"tier": None, "addons": [], "notes": [], "total": 0.0},
        "last_activity": _now_iso(),
        "warned": False,
    })

def set_state(user_id: int, new_state: Dict[str, Any]):
    data = _load_json(DATA_FILE, {})
    data[str(user_id)] = new_state
    _save_json(DATA_FILE, data)

def touch(user_id: int):
    st = get_state(user_id)
    st["last_activity"] = _now_iso()
    set_state(user_id, st)

# -------------------
# Helpers
# -------------------
def contains_any(text: str, words: List[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)

def money(n: float) -> str:
    return f"${int(n)}" if n.is_integer() else f"${n:.2f}"

def parse_addons(text: str) -> Tuple[float, List[str]]:
    """Return (amount_added, notes). Detects known add-ons in free text."""
    t = text.lower()
    added = 0.0
    notes = []

    # outfit
    if "outfit" in t:
        if "buy" in t or "purchase" in t:
            notes.append("outfit (buy needed): cost varies")
        else:
            added += 5.0
            notes.append("outfit request (+$5)")

    # toys
    if "toy" in t:
        if "bluetooth" in t or "lovense" in t:
            added += 30.0
            notes.append("bluetooth toy (+$30)")
        else:
            added += 15.0
            notes.append("regular toy (+$15)")

    # name per use
    # rough: count "name", "nickname", "call me", "use my name"
    name_uses = 0
    name_uses += len(re.findall(r"\bname\b", t))
    name_uses += len(re.findall(r"\bnickname\b", t))
    if "call me" in t or "use my name" in t:
        name_uses = max(name_uses, 1)
    # dirty talk + name per use
    if "dirty talk" in t:
        # if they also mention name, treat as name+dirty
        if name_uses == 0:
            name_uses = 1
        added += 10.0 * name_uses
        notes.append(f"name + dirty talk (+$10/use x{name_uses})")
    elif name_uses > 0:
        added += 5.0 * name_uses
        notes.append(f"name/nickname use (+$5/use x{name_uses})")

    # squirt
    if "squirt" in t:
        added += 15.0
        notes.append("squirt (+$15)")

    # drugs
    if "drug" in t or "weed" in t or "coke" in t or "pill" in t:
        notes.append("drugs (varies $30–$300)")

    return added, notes

# -------------------
# Copy (short + natural)
# -------------------
WELCOME = "hey — quick check: are you **18 or older**? (yes/no)"
UNDERAGE = "i can’t help if you’re under 18. take care."
MENU = (
    "pick what you’re here for:\n"
    "1) sexting (test build)\n\n"
    "reply with a number."
)
SEXTING_TIER = (
    "you picked **sexting**. pick a tier so i can price it:\n"
    "- premade ($40)\n- live ($50)\n- custom ($60)\n\n"
    "just say: premade / live / custom"
)
ADDONS_PROMPT = (
    "any add-ons? you can list more than one:\n"
    "- outfit (+$5 if i have it; say 'outfit have' or 'outfit buy')\n"
    "- toy: regular (+$15) or bluetooth/lovense (+$30)\n"
    "- name use (+$5 per use)\n"
    "- name + dirty talk (+$10 per use)\n"
    "- squirt (+$15)\n"
    "- drugs (varies $30–$300)\n\n"
    "if none, say **no**."
)
SCAM_WARN = "payment comes first — standard for creators. if that doesn’t work for you, i might not be a fit."
EXPLICIT_WARN = "let’s keep it relevant. i’ll ask for details when it matters — off-topic explicit stuff isn’t cool."
CONFIRM_MORE = "cool. anything else? (say **no** to finish)"
HELP_MSG = "need help? you can say /cancel to reset. or just keep going — i’ll guide you."
CANCELLED = "all good — cancelled. send /start to begin again."

# -------------------
# Command handlers
# -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_state(uid)
    st["step"] = STATE_AGE
    st["service"] = None
    st["sexting"] = {"tier": None, "addons": [], "notes": [], "total": 0.0}
    st["warned"] = False
    set_state(uid, st)
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MSG)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_state(uid)
    st["step"] = STATE_IDLE
    set_state(uid, st)
    await update.message.reply_text(CANCELLED)

async def iamadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # run this in your admin group (or DM) to store chat id
    set_admin_chat_id(update.effective_chat.id)
    await update.message.reply_text("admin chat captured. i’ll forward completions here.")

# -------------------
# Message router
# -------------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    uid = update.effective_user.id
    msg = update.message.text.strip()
    st = get_state(uid)
    st["last_activity"] = _now_iso()
    set_state(uid, st)

    # soft scam warning
    if contains_any(msg, SCAM_PHRASES) and f"scam:{uid}" not in warn_cache:
        warn_cache[f"scam:{uid}"] = True
        await update.message.reply_text(SCAM_WARN)

    # smart explicit filter (warn only when off-topic)
    if contains_any(msg, EXPLICIT_WORDS):
        if st["step"] not in (STATE_SEXTING_TIER, STATE_SEXTING_ADDONS):
            if f"exp:{uid}" not in warn_cache:
                warn_cache[f"exp:{uid}"] = True
                await update.message.reply_text(EXPLICIT_WARN)

    step = st.get("step", STATE_IDLE)

    # AGE
    if step == STATE_AGE:
        low = msg.lower()
        if "yes" in low or "18" in low or "adult" in low or "older" in low:
            st["age_ok"] = True
            st["step"] = STATE_MENU
            set_state(uid, st)
            await update.message.reply_text(MENU, parse_mode=ParseMode.MARKDOWN)
        else:
            st["age_ok"] = False
            st["step"] = STATE_IDLE
            set_state(uid, st)
            await update.message.reply_text(UNDERAGE)
        return

    # MENU (test build: only sexting)
    if step == STATE_MENU:
        if msg.isdigit() and int(msg) == 1:
            st["service"] = "sexting"
            st["step"] = STATE_SEXTING_TIER
            st["sexting"] = {"tier": None, "addons": [], "notes": [], "total": 0.0}
            set_state(uid, st)
            await update.message.reply_text(SEXTING_TIER, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("for this test, send **1** for sexting.", parse_mode=ParseMode.MARKDOWN)
        return

    # SEXTING TIER
    if step == STATE_SEXTING_TIER:
        low = msg.lower()
        if "premade" in low:
            tier = "premade"
        elif "live" in low:
            tier = "live"
        elif "custom" in low:
            tier = "custom"
        else:
            await update.message.reply_text("say: premade / live / custom")
            return

        total = SEX_BASE[tier]
        st["sexting"]["tier"] = tier
        st["sexting"]["total"] = total
        set_state(uid, st)

        await update.message.reply_text(
            f"ok — **{tier}** selected. current total: {money(total)}.\n\n{ADDONS_PROMPT}",
            parse_mode=ParseMode.MARKDOWN,
        )
        st["step"] = STATE_SEXTING_ADDONS
        set_state(uid, st)
        return

    # SEXTING ADDONS LOOP
    if step == STATE_SEXTING_ADDONS:
        low = msg.lower()
        if low in ("no", "none", "nope", "nah", "done", "finish", "finished"):
            # finish → send summary, forward to admin, send channel link
            await finalize_and_forward(update, context, uid)
            return

        add_amount, notes = parse_addons(msg)
        # merge notes uniquely but preserve order
        for n in notes:
            if n not in st["sexting"]["notes"]:
                st["sexting"]["notes"].append(n)
        if add_amount > 0:
            st["sexting"]["addons"].append({"raw": msg, "amount": add_amount})
            st["sexting"]["total"] += add_amount
        set_state(uid, st)

        await update.message.reply_text(
            f"got it. current total: {money(st['sexting']['total'])}. {CONFIRM_MORE}"
        )
        return

    # default fallback
    if step == STATE_IDLE:
        await update.message.reply_text("send /start to begin.")

# -------------------
# Finalize + Forward
# -------------------
def build_summary(user: Any, st: Dict[str, Any]) -> str:
    tier = st["sexting"]["tier"] or "not set"
    total = money(st["sexting"]["total"])
    addons_lines = st["sexting"]["notes"][:]  # copy
    if not addons_lines:
        addons_lines = ["(no add-ons)"]
    addons = "\n• " + "\n• ".join(addons_lines)

    uname = f"@{user.username}" if user.username else user.first_name or "user"
    return (
        "📋 **Order Summary**\n"
        f"User: {uname} (ID: {user.id})\n"
        "Service: Sexting\n"
        f"Tier: {tier}\n"
        f"Add-ons:{addons}\n"
        f"Total: **{total}**"
    )

async def finalize_and_forward(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    st = get_state(uid)
    summary = build_summary(update.effective_user, st)

    # send summary to user
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)

    # forward to admin chat if captured
    admin_cid = get_admin_chat_id()
    if admin_cid:
        mention = f"@{ADMIN_USERNAME}" if ADMIN_USERNAME else ""
        try:
            await context.bot.send_message(
                chat_id=admin_cid,
                text=f"🔔 **New Sexting Request** {mention}\n\n{summary}",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            # drop the admin group link for quick context if you want
            if ADMIN_CHAT_LINK:
                await context.bot.send_message(chat_id=admin_cid, text=f"admin link: {ADMIN_CHAT_LINK}")
        except Exception as e:
            # ignore failures; user still gets summary + channel link
            pass

    # send the content channel link to the user
    if CONTENT_CHANNEL_LINK:
        await update.message.reply_text(
            f"here’s my channel while you wait:\n{CONTENT_CHANNEL_LINK}"
        )

    # reset user state to idle
    st["step"] = STATE_IDLE
    set_state(uid, st)

# -------------------
# Timeout job
# -------------------
async def timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = _load_json(DATA_FILE, {})
    if not data:
        return
    now = datetime.utcnow()
    for k, st in list(data.items()):
        uid = int(k)
        last = st.get("last_activity")
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except Exception:
            last_dt = now
        diff = now - last_dt

        # warn
        if diff >= timedelta(minutes=WARN_AFTER_MIN) and not st.get("warned") and st.get("step") != STATE_IDLE:
            try:
                await context.bot.send_message(chat_id=uid, text="you still there?")
                st["warned"] = True
                data[k] = st
                _save_json(DATA_FILE, data)
            except Exception:
                pass

        # stop
        if diff >= timedelta(minutes=STOP_AFTER_MIN) and st.get("step") != STATE_IDLE:
            st["step"] = STATE_IDLE
            data[k] = st
            _save_json(DATA_FILE, data)
            try:
                await context.bot.send_message(chat_id=uid, text="i’ll stop here. send /start to continue anytime.")
            except Exception:
                pass

# -------------------
# Main
# -------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN env var.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("iamadmin", iamadmin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # run timeout checker every 60s
    app.job_queue.run_repeating(timeout_job, interval=60, first=60)

    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
