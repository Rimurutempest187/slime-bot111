#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import random
import sqlite3
import html
from functools import wraps
from dotenv import load_dotenv
from pathlib import Path
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters, CallbackQueryHandler
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()]
DB_PATH = os.getenv("DATABASE","bot_data.db")
CARD_DIR = os.getenv("CARD_IMAGES_DIR","card_images")

Path(CARD_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Rarity emojis
RARITY_EMOJI = {
    "Common":"🟤",
    "Rare":"🟡",
    "Epic":"🔮",
    "Legendary":"⚡",
    "Mythic":"👑"
}

# --- Database helpers ---
def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

conn = db_connect()
cur = conn.cursor()

def init_db():
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT,
        coins INTEGER DEFAULT 10000,
        daily_claimed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        movie TEXT,
        rarity TEXT,
        image TEXT
    );
    CREATE TABLE IF NOT EXISTS user_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        card_id INTEGER,
        is_fav INTEGER DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(card_id) REFERENCES cards(id)
    );
    CREATE TABLE IF NOT EXISTS drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        card_id INTEGER,
        claimed INTEGER DEFAULT 0,
        answer TEXT
    );
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY,
        drop_every INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS evotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        name TEXT,
        votes INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sudo_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE
    );
    """)
    conn.commit()

init_db()

# --- Utilities ---
def is_admin(user_id: int):
    return user_id in ADMIN_IDS

def is_sudo(user_id: int):
    cur.execute("SELECT 1 FROM sudo_list WHERE tg_id=?", (user_id,))
    return cur.fetchone() is not None or is_admin(user_id)

def ensure_user(tg_user):
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_user.id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (tg_id, username, coins) VALUES (?, ?, ?)",
            (tg_user.id, tg_user.username or "", 10000)
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_user.id,))
        row = cur.fetchone()
    return row

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            await update.message.reply_text("Admin commands only.")
            return
        return await func(update, context)
    return wrapper

# --- Commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    text = (
        f"မင်္ဂလာပါ {user.first_name} 👋\n\n"
        "Character Collection Game Bot မှကြိုဆိုပါတယ်။\n\n"
        "Commands များကို /helps မှာကြည့်ပါ။\n\n"
        "Create by : @Enoch_777"
    )
    await update.message.reply_text(text)

async def helps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "**User Commands**\n"
        "/start - စတင်အသုံးပြုခြင်း\n"
        "/helps - ဒီစာမျက်နှာ\n"
        "/slime <name> - Drop ကဒ်ကိုယူရန် (group drop မှာ)\n"
        "/harem - ကိုယ့် collection ကြည့်ရန်\n"
        "/set <card id> - harem မှာ fav သတ်မှတ်ရန်\n        \n"
        "/slots <amount> - Slot bet game\n"
        "/basket <amount> - Basketball bet game\n"
        "/givecoin <reply or id> <amount> - coin ပေးရန်\n"
        "/balance - coin balance\n"
        "/daily - နေ့စဉ် bonus\n"
        "/shop - ကဒ်ဝယ်ရန်\n"
        "/tops - top players\n\n"
        "**Admin Commands**\n"
        "/upload - ကဒ်တင်ရန် (reply photo with caption: Name | Movie | Rarity)\n"
        "/setdrop <number> - group drop frequency\n"
        "/gift coin <amount> <reply or id> - coin ပေးရန်\n"
        "/gift card <amount> <reply or id> - random card ပေးရန်\n"
        "/edit - admin commands list\n        \n"
        "/broadcast - group များသို့ပို့ရန် (reply to photo/text)\n"
        "/stats - users and groups\n"
        "/backup - export DB\n"
        "/restore - restore DB (admin only, manual file replace)\n"
        "/allclear - data အားလုံးဖျက်ရန်\n"
        "/delete <card id> - card ဖျက်ရန်\n"
        "/addsudo <reply or id> - sudo add\n"
        "/sudolist - sudo list\n"
        "/evote - create election (admin in group)\n"
        "/vote - vote via buttons\n\n"
        "Rarity: 🟤Common | 🟡Rare | 🔮Epic | ⚡Legendary | 👑Mythic"
    )
    await update.message.reply_text(text)

# --- Upload card (admin) ---
@admin_only
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # Expect reply with photo and caption "Name | Movie | Rarity"
    if not msg.reply_to_message or not msg.reply_to_message.photo:
        await msg.reply_text("Upload လုပ်ရန်: reply to a photo with caption: Name | Movie | Rarity")
        return
    caption = msg.reply_to_message.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    if len(parts) < 3:
        await msg.reply_text("Caption format: Name | Movie | Rarity")
        return
    name, movie, rarity = parts[0], parts[1], parts[2]
    rarity = rarity.capitalize()
    if rarity not in RARITY_EMOJI:
        await msg.reply_text(f"Invalid rarity. Use one of: {', '.join(RARITY_EMOJI.keys())}")
        return
    photo = msg.reply_to_message.photo[-1]
    file = await photo.get_file()
    filename = f"{CARD_DIR}/{random.randint(100000,999999)}_{photo.file_id}.jpg"
    await file.download_to_drive(filename)
    cur.execute("INSERT INTO cards (name, movie, rarity, image) VALUES (?, ?, ?, ?)",
                (name, movie, rarity, filename))
    conn.commit()
    await msg.reply_text(f"Card uploaded: {name} | {movie} | {rarity} {RARITY_EMOJI[rarity]}")

# --- setdrop ---
@admin_only
async def setdrop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg.chat or msg.chat.type == "private":
        await msg.reply_text("This command must be used in a group.")
        return
    try:
        n = int(context.args[0])
    except:
        await msg.reply_text("Usage: /setdrop <number>")
        return
    cur.execute("INSERT OR REPLACE INTO groups (id, drop_every, msg_count) VALUES (?, ?, COALESCE((SELECT msg_count FROM groups WHERE id=?),0))",
                (msg.chat.id, n, msg.chat.id))
    conn.commit()
    await msg.reply_text(f"Set drop every {n} messages in this group.")

# --- message counter for drops ---
async def group_message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.chat:
        return
    chat = update.message.chat
    if chat.type == "private":
        return
    cur.execute("SELECT * FROM groups WHERE id=?", (chat.id,))
    g = cur.fetchone()
    if not g:
        return
    drop_every = g["drop_every"]
    if drop_every <= 0:
        return
    msg_count = g["msg_count"] + 1
    if msg_count >= drop_every:
        # trigger drop
        # pick random card
        cur.execute("SELECT * FROM cards ORDER BY RANDOM() LIMIT 1")
        card = cur.fetchone()
        if card:
            # create drop with hidden answer
            cur.execute("INSERT INTO drops (group_id, card_id, claimed, answer) VALUES (?, ?, 0, ?)",
                        (chat.id, card["id"], card["name"].lower()))
            conn.commit()
            await context.bot.send_message(chat.id, "📢 A mysterious card has dropped! Guess its character name and claim with /slime <name>")
        msg_count = 0
    cur.execute("UPDATE groups SET msg_count=? WHERE id=?", (msg_count, chat.id))
    conn.commit()

# --- slime (claim) ---
async def slime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /slime <character name>")
        return
    guess = " ".join(args).strip().lower()
    # find latest unclaimed drop in this chat (or global if private)
    chat_id = update.message.chat.id if update.message.chat else None
    cur.execute("SELECT * FROM drops WHERE group_id=? AND claimed=0 ORDER BY id DESC LIMIT 1", (chat_id,))
    drop = cur.fetchone()
    if not drop:
        await update.message.reply_text("No active drop to claim.")
        return
    if guess == drop["answer"]:
        # give card to user
        cur.execute("SELECT * FROM cards WHERE id=?", (drop["card_id"],))
        card = cur.fetchone()
        # add to user_cards
        cur.execute("SELECT id FROM users WHERE tg_id=?", (user.id,))
        u = cur.fetchone()
        cur.execute("INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)", (u["id"], card["id"]))
        cur.execute("UPDATE drops SET claimed=1 WHERE id=?", (drop["id"],))
        conn.commit()
        await update.message.reply_text(f"Congratulations! You claimed **{card['name']}** {RARITY_EMOJI.get(card['rarity'],'')} and added to your /harem", parse_mode="Markdown")
    else:
        await update.message.reply_text("Wrong name. Try again!")

# --- harem (collection) with pagination ---
CARDS_PER_PAGE = 5

async def harem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = ensure_user(user)
    cur.execute("""
        SELECT uc.id as ucid, c.id as cid, c.name, c.movie, c.rarity, c.image, uc.is_fav
        FROM user_cards uc
        JOIN cards c ON uc.card_id=c.id
        JOIN users u ON uc.user_id=u.id
        WHERE u.tg_id=?
        ORDER BY uc.id DESC
    """, (user.id,))
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Your harem is empty. Try /shop or wait for drops.")
        return
    # pagination
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    total = len(rows)
    pages = (total + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE
    start = (page-1)*CARDS_PER_PAGE
    end = start + CARDS_PER_PAGE
    chunk = rows[start:end]
    text_lines = []
    for r in chunk:
        # count owned duplicates
        cur.execute("SELECT COUNT(*) as cnt FROM user_cards WHERE user_id=(SELECT id FROM users WHERE tg_id=?) AND card_id=?", (user.id, r["cid"]))
        cnt = cur.fetchone()["cnt"]
        text_lines.append(f"**{r['movie']}** — {r['name']} {RARITY_EMOJI.get(r['rarity'],'')}\nown:[{cnt}/25]  CardID:{r['cid']}  fav:{'⭐' if r['is_fav'] else ''}")
    text = "\n\n".join(text_lines)
    kb = []
    if page > 1:
        kb.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"harem_page_{page-1}"))
    if page < pages:
        kb.append(InlineKeyboardButton("Next ➡️", callback_data=f"harem_page_{page+1}"))
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([kb]) if kb else None)

async def harem_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if not data.startswith("harem_page_"):
        return
    page = int(data.split("_")[-1])
    # emulate /harem page
    user = q.from_user
    cur.execute("""
        SELECT uc.id as ucid, c.id as cid, c.name, c.movie, c.rarity, c.image, uc.is_fav
        FROM user_cards uc
        JOIN cards c ON uc.card_id=c.id
        JOIN users u ON uc.user_id=u.id
        WHERE u.tg_id=?
        ORDER BY uc.id DESC
    """, (user.id,))
    rows = cur.fetchall()
    total = len(rows)
    pages = (total + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE
    start = (page-1)*CARDS_PER_PAGE
    end = start + CARDS_PER_PAGE
    chunk = rows[start:end]
    text_lines = []
    for r in chunk:
        cur.execute("SELECT COUNT(*) as cnt FROM user_cards WHERE user_id=(SELECT id FROM users WHERE tg_id=?) AND card_id=?", (user.id, r["cid"]))
        cnt = cur.fetchone()["cnt"]
        text_lines.append(f"**{r['movie']}** — {r['name']} {RARITY_EMOJI.get(r['rarity'],'')}\nown:[{cnt}/25]  CardID:{r['cid']}  fav:{'⭐' if r['is_fav'] else ''}")
    text = "\n\n".join(text_lines)
    kb = []
    if page > 1:
        kb.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"harem_page_{page-1}"))
    if page < pages:
        kb.append(InlineKeyboardButton("Next ➡️", callback_data=f"harem_page_{page+1}"))
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([kb]) if kb else None)

# --- set favorite ---
async def setfav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    if not context.args:
        await update.message.reply_text("Usage: /set <card id>")
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text("Invalid card id.")
        return
    cur.execute("SELECT id FROM users WHERE tg_id=?", (user.id,))
    u = cur.fetchone()
    # unset previous fav
    cur.execute("UPDATE user_cards SET is_fav=0 WHERE user_id=?", (u["id"],))
    # set fav if user has that card
    cur.execute("SELECT uc.id FROM user_cards uc JOIN cards c ON uc.card_id=c.id WHERE uc.user_id=? AND c.id=?", (u["id"], cid))
    row = cur.fetchone()
    if not row:
        await update.message.reply_text("You don't own that card.")
        return
    cur.execute("UPDATE user_cards SET is_fav=1 WHERE id=?", (row["id"],))
    conn.commit()
    await update.message.reply_text("Favorite set.")

# --- slots game ---
async def slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = ensure_user(user)
    if not context.args:
        await update.message.reply_text("Usage: /slots <amount>")
        return
    try:
        amt = int(context.args[0])
    except:
        await update.message.reply_text("Invalid amount.")
        return
    if amt <= 0:
        await update.message.reply_text("Amount must be positive.")
        return
    if u["coins"] < amt:
        await update.message.reply_text("Not enough coins.")
        return
    # play
    symbols = ["🍒","🍋","🔔","⭐","7️⃣"]
    res = [random.choice(symbols) for _ in range(3)]
    text = " | ".join(res)
    # determine win
    if res[0]==res[1]==res[2]:
        multiplier = 3
        win = amt * multiplier
        new_coins = u["coins"] + win
        cur.execute("UPDATE users SET coins=? WHERE tg_id=?", (new_coins, user.id))
        conn.commit()
        await update.message.reply_text(f"{text}\nJackpot! You won {win} coins. New balance: {new_coins}")
    elif res[0]==res[1] or res[1]==res[2] or res[0]==res[2]:
        multiplier = 2
        win = amt * multiplier
        new_coins = u["coins"] + win
        cur.execute("UPDATE users SET coins=? WHERE tg_id=?", (new_coins, user.id))
        conn.commit()
        await update.message.reply_text(f"{text}\nNice! You won {win} coins. New balance: {new_coins}")
    else:
        new_coins = u["coins"] - amt
        cur.execute("UPDATE users SET coins=? WHERE tg_id=?", (new_coins, user.id))
        conn.commit()
        await update.message.reply_text(f"{text}\nYou lost {amt} coins. New balance: {new_coins}")

# --- basket game ---
async def basket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = ensure_user(user)
    if not context.args:
        await update.message.reply_text("Usage: /basket <amount>")
        return
    try:
        amt = int(context.args[0])
    except:
        await update.message.reply_text("Invalid amount.")
        return
    if amt <= 0:
        await update.message.reply_text("Amount must be positive.")
        return
    if u["coins"] < amt:
        await update.message.reply_text("Not enough coins.")
        return
    # 50% chance to score; if score -> 2x or 3x randomly
    scored = random.random() < 0.5
    if scored:
        mult = random.choice([2,3])
        win = amt * mult
        new_coins = u["coins"] + win
        cur.execute("UPDATE users SET coins=? WHERE tg_id=?", (new_coins, user.id))
        conn.commit()
        await update.message.reply_text(f"🏀 You scored! Multiplier {mult}x. You won {win} coins. New balance: {new_coins}")
    else:
        new_coins = u["coins"] - amt
        cur.execute("UPDATE users SET coins=? WHERE tg_id=?", (new_coins, user.id))
        conn.commit()
        await update.message.reply_text(f"Missed! You lost {amt} coins. New balance: {new_coins}")

# --- givecoin ---
async def givecoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = ensure_user(user)
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /givecoin <reply or id> <amount>")
        return
    target = context.args[0]
    try:
        amt = int(context.args[1])
    except:
        await update.message.reply_text("Invalid amount.")
        return
    if amt <= 0:
        await update.message.reply_text("Amount must be positive.")
        return
    # determine target id
    if update.message.reply_to_message:
        tgt = update.message.reply_to_message.from_user
        tgt_id = tgt.id
    else:
        try:
            tgt_id = int(target)
        except:
            await update.message.reply_text("Invalid target id.")
            return
    if u["coins"] < amt:
        await update.message.reply_text("Not enough coins.")
        return
    # transfer
    cur.execute("UPDATE users SET coins=coins-? WHERE tg_id=?", (amt, user.id))
    cur.execute("INSERT OR IGNORE INTO users (tg_id, username, coins) VALUES (?, ?, ?)", (tgt_id, "", 10000))
    cur.execute("UPDATE users SET coins=coins+? WHERE tg_id=?", (amt, tgt_id))
    conn.commit()
    await update.message.reply_text(f"Sent {amt} coins to {tgt_id}.")

# --- balance ---
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = ensure_user(user)
    await update.message.reply_text(f"Your balance: {u['coins']} coins")

# --- daily ---
async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = ensure_user(user)
    # simple daily cooldown by day number
    from datetime import datetime
    today = int(datetime.utcnow().strftime("%Y%m%d"))
    if u["daily_claimed"] == today:
        await update.message.reply_text("You've already claimed today's bonus.")
        return
    bonus = random.randint(5000,50000)
    cur.execute("UPDATE users SET coins=coins+?, daily_claimed=? WHERE tg_id=?", (bonus, today, user.id))
    conn.commit()
    await update.message.reply_text(f"You received daily bonus: {bonus} coins. Enjoy!")

# --- shop (browse cards and buy) ---
async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show one card at a time with price
    cur.execute("SELECT * FROM cards ORDER BY id ASC")
    cards = cur.fetchall()
    if not cards:
        await update.message.reply_text("Shop is empty.")
        return
    # store index in user context
    context.user_data['shop_index'] = 0
    await show_shop_card(update, context)

async def show_shop_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get('shop_index',0)
    cur.execute("SELECT * FROM cards ORDER BY id ASC")
    cards = cur.fetchall()
    if not cards:
        await update.message.reply_text("Shop empty.")
        return
    card = cards[idx % len(cards)]
    # price based on rarity
    base_price = {"Common":1000,"Rare":5000,"Epic":15000,"Legendary":50000,"Mythic":150000}
    price = base_price.get(card["rarity"],2000)
    text = f"**{card['name']}**\nMovie: {card['movie']}\nRarity: {card['rarity']} {RARITY_EMOJI.get(card['rarity'],'')}\nPrice: {price} coins\nCardID: {card['id']}"
    kb = [
        [InlineKeyboardButton("Buy", callback_data=f"shop_buy_{card['id']}_{price}"),
         InlineKeyboardButton("Next", callback_data="shop_next")]
    ]
    # send photo if exists
    if card["image"] and os.path.exists(card["image"]):
        await update.message.reply_photo(open(card["image"],"rb"), caption=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def shop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user = q.from_user
    ensure_user(user)
    if data == "shop_next":
        context.user_data['shop_index'] = context.user_data.get('shop_index',0) + 1
        # edit message to next card
        # simply call show_shop_card by sending new message
        await show_shop_card(q, context)
        try:
            await q.message.delete()
        except:
            pass
        return
    if data.startswith("shop_buy_"):
        parts = data.split("_")
        cid = int(parts[2])
        price = int(parts[3])
        cur.execute("SELECT coins FROM users WHERE tg_id=?", (user.id,))
        coins = cur.fetchone()["coins"]
        if coins < price:
            await q.message.reply_text("Not enough coins.")
            return
        # give card
        cur.execute("INSERT INTO user_cards (user_id, card_id) VALUES ((SELECT id FROM users WHERE tg_id=?), ?)", (user.id, cid))
        cur.execute("UPDATE users SET coins=coins-? WHERE tg_id=?", (price, user.id))
        conn.commit()
        await q.message.reply_text("Purchased and added to your /harem!")

# --- tops ---
async def tops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("Top Coins", callback_data="tops_coins"),
         InlineKeyboardButton("Top Cards", callback_data="tops_cards")]
    ]
    await update.message.reply_text("Choose leaderboard:", reply_markup=InlineKeyboardMarkup(kb))

async def tops_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "tops_coins":
        cur.execute("SELECT tg_id, username, coins FROM users ORDER BY coins DESC LIMIT 10")
        rows = cur.fetchall()
        text = "Top Coins:\n"
        for i,r in enumerate(rows,1):
            text += f"{i}. {r['username'] or r['tg_id']} — {r['coins']}\n"
        await q.edit_message_text(text)
    else:
        cur.execute("""
            SELECT u.tg_id, u.username, COUNT(uc.id) as cnt
            FROM users u
            LEFT JOIN user_cards uc ON uc.user_id=u.id
            GROUP BY u.id
            ORDER BY cnt DESC LIMIT 10
        """)
        rows = cur.fetchall()
        text = "Top Collections:\n"
        for i,r in enumerate(rows,1):
            text += f"{i}. {r['username'] or r['tg_id']} — {r['cnt']} cards\n"
        await q.edit_message_text(text)

# --- admin gift coin/card ---
@admin_only
async def gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /gift coin <amount> <reply or id>
    msg = update.message
    args = context.args
    if len(args) < 3:
        await msg.reply_text("Usage: /gift coin|card <amount> <reply or id>")
        return
    typ = args[0].lower()
    try:
        amt = int(args[1])
    except:
        await msg.reply_text("Invalid amount.")
        return
    # target
    if msg.reply_to_message:
        tgt = msg.reply_to_message.from_user
        tgt_id = tgt.id
    else:
        try:
            tgt_id = int(args[2])
        except:
            await msg.reply_text("Invalid target id.")
            return
    cur.execute("INSERT OR IGNORE INTO users (tg_id, username, coins) VALUES (?, ?, ?)", (tgt_id, "", 10000))
    if typ == "coin":
        cur.execute("UPDATE users SET coins=coins+? WHERE tg_id=?", (amt, tgt_id))
        conn.commit()
        await msg.reply_text(f"Gave {amt} coins to {tgt_id}.")
    elif typ == "card":
        # give random cards
        cur.execute("SELECT id FROM cards ORDER BY RANDOM() LIMIT ?", (amt,))
        picks = cur.fetchall()
        for p in picks:
            cur.execute("INSERT INTO user_cards (user_id, card_id) VALUES ((SELECT id FROM users WHERE tg_id=?), ?)", (tgt_id, p["id"]))
        conn.commit()
        await msg.reply_text(f"Gave {amt} random cards to {tgt_id}.")
    else:
        await msg.reply_text("Invalid type. Use coin or card.")

# --- edit (admin commands list) ---
@admin_only
async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Admin commands: /upload, /setdrop, /gift, /broadcast, /stats, /backup, /restore, /allclear, /delete, /addsudo, /sudolist, /evote"
    await update.message.reply_text(text)

# --- broadcast ---
@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # send to all groups in groups table
    cur.execute("SELECT id FROM groups")
    groups = cur.fetchall()
    if not groups:
        await msg.reply_text("No groups registered.")
        return
    # message to send: if reply, use that content
    if msg.reply_to_message:
        for g in groups:
            try:
                if msg.reply_to_message.photo:
                    await context.bot.send_photo(g["id"], msg.reply_to_message.photo[-1].file_id, caption=msg.reply_to_message.caption or "")
                else:
                    await context.bot.send_message(g["id"], msg.reply_to_message.text or "")
            except Exception as e:
                logger.warning(f"Broadcast to {g['id']} failed: {e}")
        await msg.reply_text("Broadcast sent.")
    else:
        await msg.reply_text("Reply to a message (text or photo) to broadcast.")

# --- stats ---
@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT COUNT(*) as c FROM users")
    users = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM groups")
    groups = cur.fetchone()["c"]
    await update.message.reply_text(f"Users: {users}\nGroups: {groups}")

# --- backup (export DB file) ---
@admin_only
async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_document(open(DB_PATH,"rb"))
    except Exception as e:
        await update.message.reply_text("Backup failed.")

# --- allclear ---
@admin_only
async def allclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.executescript("""
    DELETE FROM user_cards;
    DELETE FROM users;
    DELETE FROM drops;
    """)
    conn.commit()
    await update.message.reply_text("All user data cleared.")

# --- delete card ---
@admin_only
async def delete_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <card id>")
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text("Invalid id.")
        return
    cur.execute("DELETE FROM cards WHERE id=?", (cid,))
    cur.execute("DELETE FROM user_cards WHERE card_id=?", (cid,))
    conn.commit()
    await update.message.reply_text("Card deleted.")

# --- addsudo / sudolist ---
@admin_only
async def addsudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        tgt = update.message.reply_to_message.from_user
        tg_id = tgt.id
    elif context.args:
        try:
            tg_id = int(context.args[0])
        except:
            await update.message.reply_text("Invalid id.")
            return
    else:
        await update.message.reply_text("Reply to user or provide id.")
        return
    cur.execute("INSERT OR IGNORE INTO sudo_list (tg_id) VALUES (?)", (tg_id,))
    conn.commit()
    await update.message.reply_text(f"Added sudo: {tg_id}")

@admin_only
async def sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT tg_id FROM sudo_list")
    rows = cur.fetchall()
    text = "Sudo list:\n" + "\n".join(str(r["tg_id"]) for r in rows) if rows else "Empty"
    await update.message.reply_text(text)

# --- evote / vote ---
@admin_only
async def evote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /evote name1 | name2 | name3 (in group)
    if not update.message.chat or update.message.chat.type == "private":
        await update.message.reply_text("Use in group.")
        return
    caption = " ".join(context.args)
    if not caption:
        await update.message.reply_text("Usage: /evote name1 | name2 | ...")
        return
    names = [n.strip() for n in " ".join(context.args).split("|") if n.strip()]
    if not names:
        await update.message.reply_text("No names provided.")
        return
    # clear previous evotes for group
    cur.execute("DELETE FROM evotes WHERE group_id=?", (update.message.chat.id,))
    for n in names:
        cur.execute("INSERT INTO evotes (group_id, name, votes) VALUES (?, ?, 0)", (update.message.chat.id, n))
    conn.commit()
    # build buttons
    cur.execute("SELECT id, name FROM evotes WHERE group_id=?", (update.message.chat.id,))
    rows = cur.fetchall()
    kb = [[InlineKeyboardButton(r["name"], callback_data=f"vote_{r['id']}")] for r in rows]
    await update.message.reply_text("Vote now:", reply_markup=InlineKeyboardMarkup(kb))

async def vote_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if not data.startswith("vote_"):
        return
    vid = int(data.split("_")[1])
    # increment vote
    cur.execute("UPDATE evotes SET votes=votes+1 WHERE id=?", (vid,))
    conn.commit()
    cur.execute("SELECT name, votes FROM evotes WHERE id=?", (vid,))
    r = cur.fetchone()
    await q.edit_message_text(f"Voted for {r['name']} — total votes: {r['votes']}")

# --- message handler for registering groups ---
async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat and update.message.chat.type != "private":
        cur.execute("INSERT OR IGNORE INTO groups (id, drop_every, msg_count) VALUES (?, 0, 0)", (update.message.chat.id,))
        conn.commit()

# --- main ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("helps", helps))
    app.add_handler(CommandHandler("slime", slime))
    app.add_handler(CommandHandler("harem", harem))
    app.add_handler(CallbackQueryHandler(harem_page_cb, pattern=r"^harem_page_"))
    app.add_handler(CommandHandler("set", setfav))
    app.add_handler(CommandHandler("slots", slots))
    app.add_handler(CommandHandler("basket", basket))
    app.add_handler(CommandHandler("givecoin", givecoin))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("shop", shop))
    app.add_handler(CallbackQueryHandler(shop_cb, pattern=r"^shop_"))
    app.add_handler(CommandHandler("tops", tops))
    app.add_handler(CallbackQueryHandler(tops_cb, pattern=r"^tops_"))
    app.add_handler(CommandHandler("upload", upload))
    app.add_handler(CommandHandler("setdrop", setdrop))
    app.add_handler(CommandHandler("gift", gift))
    app.add_handler(CommandHandler("edit", edit))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("allclear", allclear))
    app.add_handler(CommandHandler("delete", delete_card))
    app.add_handler(CommandHandler("addsudo", addsudo))
    app.add_handler(CommandHandler("sudolist", sudolist))
    app.add_handler(CommandHandler("evote", evote))
    app.add_handler(CallbackQueryHandler(vote_cb, pattern=r"^vote_"))

    # group message counter and register
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), group_message_counter))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), register_group))

    # shop navigation callback already added
    # start
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
