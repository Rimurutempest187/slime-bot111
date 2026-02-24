## bot.py

#!/usr/bin/env python3
# coding: utf-8
"""
Character Collection Game — Telegram bot
Features implemented:
 - User system with coins and cards
 - Card upload (admin) with photo/file_id and metadata
 - Auto drop in groups after /setdrop threshold reached
 - /slime <name> to claim hidden dropped card
 - /harem with pagination (5 cards per page)
 - /set <card id> to set favorite
 - /shop with buy/next flow
 - /slots and /basket bet minigames
 - /daily reward
 - /balance, /givecoin
 - basic admin commands: /upload, /setdrop, /gift, /broadcast, /stats, /delete, /addsudo, /sudolist
 - sqlite3 persistence

This single-file implementation focuses on clarity and practical completeness while remaining easy to extend.
"""

import logging
import os
import sqlite3
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

from dotenv import load_dotenv
from telegram import (
    __version__ as TGVER,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# --- Config & Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(',') if x.strip()]
DB_PATH = os.getenv("DB_PATH", "data.db")
DAILY_MIN = 5000
DAILY_MAX = 50000

# Rarity emoji mapping
RARITY_EMOJI = {
    'common': '🟤',
    'rare': '🟡',
    'epic': '🔮',
    'legendary': '⚡',
    'mythic': '👑',
}

# --- Database helpers ---
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # users: id, telegram_id, username, coins, last_daily, favorite_card
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        coins INTEGER DEFAULT 10000,
        last_daily TIMESTAMP,
        favorite_card INTEGER
    )
    ''')

    # cards: id, name, movie, rarity, price, file_id, uploader_id, created_at
    cur.execute('''
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY,
        name TEXT,
        movie TEXT,
        rarity TEXT,
        price INTEGER DEFAULT 0,
        file_id TEXT,
        uploader_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # user_cards: id, user_id, card_id, qty
    cur.execute('''
    CREATE TABLE IF NOT EXISTS user_cards (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        card_id INTEGER,
        qty INTEGER DEFAULT 1,
        UNIQUE(user_id, card_id)
    )
    ''')

    # groups: id, chat_id, drop_threshold, drop_counter, waiting_card_id
    cur.execute('''
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER UNIQUE,
        drop_threshold INTEGER DEFAULT 0,
        drop_counter INTEGER DEFAULT 0,
        waiting_card_id INTEGER
    )
    ''')

    # sudoers: id, telegram_id
    cur.execute('''
    CREATE TABLE IF NOT EXISTS sudoers (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE
    )
    ''')

    # votes: vote_id, title, options(json/text simplified), counts
    cur.execute('''
    CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY,
        title TEXT,
        options TEXT,
        counts TEXT
    )
    ''')

    # settings for misc key-values
    cur.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')

    conn.commit()
    conn.close()

# --- Utility functions ---

def ensure_user(telegram_id: int, username: Optional[str] = None):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
    row = cur.fetchone()
    if not row:
        cur.execute('INSERT INTO users (telegram_id, username, coins) VALUES (?, ?, ?)', (telegram_id, username, 10000))
        conn.commit()
    else:
        if username and row['username'] != username:
            cur.execute('UPDATE users SET username = ? WHERE telegram_id = ?', (username, telegram_id))
            conn.commit()
    conn.close()


def get_user_by_tid(telegram_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row


def add_coins(telegram_id: int, amount: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('UPDATE users SET coins = coins + ? WHERE telegram_id = ?', (amount, telegram_id))
    conn.commit(); conn.close()


def take_coins(telegram_id: int, amount: int) -> bool:
    user = get_user_by_tid(telegram_id)
    if not user:
        return False
    if user['coins'] < amount:
        return False
    conn = get_conn(); cur = conn.cursor()
    cur.execute('UPDATE users SET coins = coins - ? WHERE telegram_id = ?', (amount, telegram_id))
    conn.commit(); conn.close()
    return True


def insert_card(name: str, movie: str, rarity: str, price: int, file_id: str, uploader_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO cards (name, movie, rarity, price, file_id, uploader_id) VALUES (?, ?, ?, ?, ?, ?)',
                (name, movie, rarity, price, file_id, uploader_id))
    conn.commit(); cid = cur.lastrowid
    conn.close()
    return cid


def add_card_to_user(telegram_id: int, card_id: int, qty: int = 1):
    user = get_user_by_tid(telegram_id)
    if not user:
        return False
    uid = user['id']
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM user_cards WHERE user_id = ? AND card_id = ?', (uid, card_id))
    row = cur.fetchone()
    if row:
        cur.execute('UPDATE user_cards SET qty = qty + ? WHERE id = ?', (qty, row['id']))
    else:
        cur.execute('INSERT INTO user_cards (user_id, card_id, qty) VALUES (?, ?, ?)', (uid, card_id, qty))
    conn.commit(); conn.close()
    return True


def get_user_cards(telegram_id: int) -> List[sqlite3.Row]:
    user = get_user_by_tid(telegram_id)
    if not user:
        return []
    uid = user['id']
    conn = get_conn(); cur = conn.cursor()
    cur.execute('''
    SELECT uc.qty, c.* FROM user_cards uc
    JOIN cards c ON c.id = uc.card_id
    WHERE uc.user_id = ? ORDER BY c.rarity DESC, c.name
    ''', (uid,))
    rows = cur.fetchall(); conn.close(); return rows


def get_card_by_id(card_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM cards WHERE id = ?', (card_id,))
    row = cur.fetchone(); conn.close(); return row

# --- Decorators / permission checks ---

def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM sudoers WHERE telegram_id = ?', (user_id,))
    r = cur.fetchone(); conn.close()
    return bool(r)

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    text = (
        f"Hello, {user.first_name}!\n"
        "Welcome to the Character Collection Game — collect characters, play minigames, and climb the leaderboards!\n\n"
        "Use /helps to see available commands.\n\n"
        "Create by : @Enoch_777"
    )
    await update.message.reply_text(text)


async def helps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "User commands:\n"
        "/start — Start and welcome\n"
        "/helps — Show this help\n"
        "/slime <name> — Claim a hidden dropped card when available\n"
        "/harem — Show your card collection (5 per page)\n"
        "/set <card id> — Set favorite card from your collection\n"
        "/slots <amount> — Play slot machine\n"
        "/basket <amount> — Shoot a basketball bet\n"
        "/givecoin <reply or id> <amount> — Transfer coins to a user\n"
        "/balance — Check your coins\n"
        "/daily — Claim daily bonus (24h)\n"
        "/shop — Buy random characters from the shop\n"
        "/tops — Show top 10 by coins or cards\n"
        "\nAdmin commands: /upload /setdrop /gift /broadcast /stats /delete /addsudo /sudolist\n"
    )
    await update.message.reply_text(text)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    row = get_user_by_tid(user.id)
    await update.message.reply_text(f"{user.first_name}, your balance: {row['coins']} coins")


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    row = get_user_by_tid(user.id)
    last = row['last_daily']
    now = datetime.utcnow()
    if last:
        last_dt = datetime.fromisoformat(last)
        if now - last_dt < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - last_dt)
            await update.message.reply_text(f"Daily already claimed. Try again in {str(remaining).split('.',1)[0]}")
            return
    amount = random.randint(DAILY_MIN, DAILY_MAX)
    add_coins(user.id, amount)
    conn = get_conn(); cur = conn.cursor()
    cur.execute('UPDATE users SET last_daily = ? WHERE telegram_id = ?', (now.isoformat(), user.id))
    conn.commit(); conn.close()
    await update.message.reply_text(f"You received {amount} coins as daily reward!")


# --- Shop flow ---
async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Present a random card (not owned) with Buy / Next buttons
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM cards ORDER BY RANDOM() LIMIT 1')
    card = cur.fetchone(); conn.close()
    if not card:
        await update.message.reply_text("Shop is empty — admin needs to upload cards.")
        return
    caption = format_card_caption(card)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Buy', callback_data=f'shop_buy:{card[0]}'), InlineKeyboardButton('Next', callback_data='shop_next')]])
    if card['file_id']:
        await update.message.reply_photo(photo=card['file_id'], caption=caption, reply_markup=keyboard)
    else:
        await update.message.reply_text(caption, reply_markup=keyboard)


def format_card_caption(card_row) -> str:
    rarity = card_row['rarity'].lower() if card_row['rarity'] else 'common'
    emoji = RARITY_EMOJI.get(rarity, '')
    return (f"{emoji} <b>{card_row['name']}</b> — {card_row['movie']}\n"
            f"Card ID: {card_row['id']} | Price: {card_row['price']} coins\n"
            f"Rarity: {card_row['rarity'].title()}")


async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    ensure_user(user.id, user.username)
    if data == 'shop_next':
        await shop(update, context)
        return
    if data.startswith('shop_buy:'):
        card_id = int(data.split(':', 1)[1])
        card = get_card_by_id(card_id)
        if not card:
            await query.edit_message_text('Card not found')
            return
        if not take_coins(user.id, card['price']):
            await query.answer('Not enough coins', show_alert=True)
            return
        add_card_to_user(user.id, card_id, 1)
        await query.edit_message_caption(f"You bought {card['name']} for {card['price']} coins!", reply_markup=None)

# --- Harem (collection) with pagination ---
async def harem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    page = 0
    if context.args:
        try:
            page = int(context.args[0])
        except:
            page = 0
    await send_harem_page(update.effective_chat.id, user.id, page, context)


async def send_harem_page(chat_id: int, telegram_id: int, page: int, context: ContextTypes.DEFAULT_TYPE):
    cards = get_user_cards(telegram_id)
    per_page = 5
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page; stop = start + per_page
    slice_cards = cards[start:stop]
    if not slice_cards:
        await context.bot.send_message(chat_id, 'No cards in your harem yet — try /shop or wait for drops!')
        return
    lines = []
    for r in slice_cards:
        emoji = RARITY_EMOJI.get(r['rarity'].lower() if r['rarity'] else 'common', '')
        lines.append(f"{emoji} <b>{r['name']}</b> — {r['movie']} | own:[{r['qty']}] | Card ID: {r['id']}")
    text = '\n'.join(lines)
    kb = []
    if page > 0:
        kb.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'harem_page:{page-1}'))
    if page < total_pages-1:
        kb.append(InlineKeyboardButton('Next ➡️', callback_data=f'harem_page:{page+1}'))
    await context.bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([kb]) if kb else None)


async def harem_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith('harem_page:'):
        page = int(data.split(':', 1)[1])
        user = query.from_user
        await query.edit_message_text('Loading...')
        await send_harem_page(query.message.chat.id, user.id, page, context)


# --- Set favorite card ---
async def setfav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('Usage: /set <card id>')
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text('Card id must be a number')
        return
    user = update.effective_user
    row = get_card_by_id(cid)
    if not row:
        await update.message.reply_text('Card not found')
        return
    # check ownership
    ucards = get_user_cards(user.id)
    if not any(c['id'] == cid for c in ucards):
        await update.message.reply_text('You do not own that card')
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('UPDATE users SET favorite_card = ? WHERE telegram_id = ?', (cid, user.id))
    conn.commit(); conn.close()
    await update.message.reply_text(f'Set card {cid} as favorite')

# --- Slots minigame ---
async def slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    if not context.args:
        await update.message.reply_text('Usage: /slots <amount>')
        return
    try:
        amt = int(context.args[0])
    except:
        await update.message.reply_text('Amount must be a number')
        return
    if amt <= 0:
        await update.message.reply_text('Bet must be positive')
        return
    if not take_coins(user.id, amt):
        await update.message.reply_text('Not enough coins')
        return
    # simulate slots: 30% lose, 50% x2, 20% x3
    r = random.random()
    if r < 0.3:
        await update.message.reply_text(f'You lost {amt} coins 🎰')
    elif r < 0.8:
        win = amt * 2
        add_coins(user.id, win)
        await update.message.reply_text(f'You win 2× = {win} coins! 🎉')
    else:
        win = amt * 3
        add_coins(user.id, win)
        await update.message.reply_text(f'JACKPOT 3× = {win} coins! 🚀')

# --- Basket minigame ---
async def basket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    if not context.args:
        await update.message.reply_text('Usage: /basket <amount>')
        return
    try:
        amt = int(context.args[0])
    except:
        await update.message.reply_text('Amount must be a number')
        return
    if amt <= 0:
        await update.message.reply_text('Bet must be positive')
        return
    if not take_coins(user.id, amt):
        await update.message.reply_text('Not enough coins')
        return
    # 45% miss, 40% 2x, 15% 3x
    r = random.random()
    if r < 0.45:
        await update.message.reply_text(f'Missed! You lost {amt} coins 🏀')
    elif r < 0.85:
        win = amt * 2
        add_coins(user.id, win)
        await update.message.reply_text(f'Nice shot! You win 2× = {win} coins 🏀🎉')
    else:
        win = amt * 3
        add_coins(user.id, win)
        await update.message.reply_text(f'Pure swish! You win 3× = {win} coins 🏀🔥')

# --- Give coin to others ---
async def givecoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text('Usage: /givecoin <reply or id> <amount>')
        return
    target = context.args[0]
    try:
        amt = int(context.args[1])
    except:
        await update.message.reply_text('Amount must be a number')
        return
    if update.message.reply_to_message:
        target_tid = update.message.reply_to_message.from_user.id
    else:
        try:
            target_tid = int(target)
        except:
            await update.message.reply_text('Target must be a reply or user id')
            return
    sender = update.effective_user
    ensure_user(sender.id, sender.username)
    if not take_coins(sender.id, amt):
        await update.message.reply_text('Not enough coins')
        return
    ensure_user(target_tid)
    add_coins(target_tid, amt)
    await update.message.reply_text(f'Transferred {amt} coins to {target_tid}')

# --- Admin: upload card ---
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('You are not authorized to upload cards')
        return
    # Expect a photo with caption: name | movie | rarity | price
    msg = update.message
    if not msg.photo:
        await msg.reply_text('Send a photo with caption: Name | Movie | rarity | price')
        return
    caption = msg.caption or ''
    parts = [p.strip() for p in caption.split('|')]
    if len(parts) < 3:
        await msg.reply_text('Caption must be: Name | Movie | rarity | price')
        return
    name, movie, rarity = parts[0], parts[1], parts[2].lower()
    price = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    file_id = msg.photo[-1].file_id
    cid = insert_card(name, movie, rarity, price, file_id, user.id)
    await msg.reply_text(f'Card uploaded: {name} ({rarity}) — id {cid}')

# --- Group message counter to trigger drops ---
async def group_message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # increment counter for group if setdrop was used
    chat = update.effective_chat
    if not chat or chat.type == 'private':
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM groups WHERE chat_id = ?', (chat.id,))
    row = cur.fetchone()
    if not row:
        # create group entry
        cur.execute('INSERT OR IGNORE INTO groups (chat_id, drop_threshold, drop_counter) VALUES (?, ?, ?)', (chat.id, 0, 0))
        conn.commit(); conn.close(); return
    threshold = row['drop_threshold'] or 0
    if threshold <= 0:
        conn.close(); return
    counter = (row['drop_counter'] or 0) + 1
    if counter >= threshold:
        # trigger drop: pick random card
        cur.execute('SELECT * FROM cards ORDER BY RANDOM() LIMIT 1')
        card = cur.fetchone()
        if card:
            # set waiting_card_id and reset counter
            cur.execute('UPDATE groups SET waiting_card_id = ?, drop_counter = 0 WHERE chat_id = ?', (card['id'], chat.id))
            conn.commit()
            # send a hidden drop: show photo but hide name, allow claiming via /slime <name>
            try:
                await context.bot.send_photo(chat.id, photo=card['file_id'], caption='A mysterious card dropped! Guess its character name and claim with /slime <name>')
            except Exception as e:
                logger.exception('Failed to send drop photo')
        else:
            cur.execute('UPDATE groups SET drop_counter = 0 WHERE chat_id = ?', (chat.id,))
            conn.commit()
    else:
        cur.execute('UPDATE groups SET drop_counter = ? WHERE chat_id = ?', (counter, chat.id))
        conn.commit()
    conn.close()

# --- Slime claim command ---
async def slime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    chat = update.effective_chat
    if chat.type == 'private':
        # allow claiming from private: check if user has waiting personal drop? For now: fail
        await update.message.reply_text('Use this in the group where the card dropped.')
        return
    if not context.args:
        await update.message.reply_text('Usage: /slime <character name>')
        return
    guess = ' '.join(context.args).strip().lower()
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT * FROM groups WHERE chat_id = ?', (chat.id,))
    g = cur.fetchone()
    if not g or not g['waiting_card_id']:
        await update.message.reply_text('No hidden card is waiting right now in this group.')
        conn.close(); return
    card = get_card_by_id(g['waiting_card_id'])
    if not card:
        await update.message.reply_text('No hidden card info, contact admin.')
        cur.execute('UPDATE groups SET waiting_card_id = NULL WHERE chat_id = ?', (chat.id,))
        conn.commit(); conn.close(); return
    if card['name'].strip().lower() == guess:
        add_card_to_user(user.id, card['id'], 1)
        cur.execute('UPDATE groups SET waiting_card_id = NULL WHERE chat_id = ?', (chat.id,))
        conn.commit(); conn.close()
        await update.message.reply_text(f'Congrats {user.first_name}! You claimed {card["name"]} and it was added to your harem!')
    else:
        await update.message.reply_text('Wrong guess — try again!')
        conn.close()

# --- Admin: setdrop ---
async def setdrop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    if not context.args:
        await update.message.reply_text('Usage: /setdrop <number> (set number of messages needed to trigger a drop in this group)')
        return
    try:
        n = int(context.args[0])
    except:
        await update.message.reply_text('Number must be integer')
        return
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text('Use this in the group where you want drops')
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO groups (chat_id, drop_threshold, drop_counter) VALUES (?, ?, ?)', (chat.id, n, 0))
    cur.execute('UPDATE groups SET drop_threshold = ? WHERE chat_id = ?', (n, chat.id))
    conn.commit(); conn.close()
    await update.message.reply_text(f'Drop threshold for this group set to {n} messages')

# --- Admin: gift coin/card ---
async def gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    # Usage: /gift coin <amount> <reply or id>
    if len(context.args) < 3:
        await update.message.reply_text('Usage: /gift coin|card <amount> <reply or id>')
        return
    kind = context.args[0].lower()
    try:
        amt = int(context.args[1])
    except:
        await update.message.reply_text('Amount must be integer')
        return
    target = context.args[2]
    if update.message.reply_to_message:
        tid = update.message.reply_to_message.from_user.id
    else:
        try:
            tid = int(target)
        except:
            await update.message.reply_text('Target must be reply or user id')
            return
    ensure_user(tid)
    if kind == 'coin':
        add_coins(tid, amt)
        await update.message.reply_text(f'Gave {amt} coins to {tid}')
    elif kind == 'card':
        # give random 'amt' cards
        conn = get_conn(); cur = conn.cursor()
        cur.execute('SELECT id FROM cards')
        rows = cur.fetchall()
        if not rows:
            await update.message.reply_text('No cards in database')
            conn.close(); return
        for _ in range(amt):
            cid = random.choice(rows)['id']
            add_card_to_user(tid, cid, 1)
        await update.message.reply_text(f'Gave {amt} random cards to {tid}')
    else:
        await update.message.reply_text('Unknown gift type. Use coin or card')

# --- misc admin utilities ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT COUNT(*) as c FROM users')
    users = cur.fetchone()['c']
    cur.execute('SELECT COUNT(*) as c FROM cards')
    cards = cur.fetchone()['c']
    cur.execute('SELECT COUNT(*) as c FROM groups')
    groups = cur.fetchone()['c']
    conn.close()
    await update.message.reply_text(f'Users: {users}\nCards: {cards}\nGroups with settings: {groups}')

async def delete_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    if not context.args:
        await update.message.reply_text('Usage: /delete <card id>')
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text('Card id must be integer')
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('DELETE FROM cards WHERE id = ?', (cid,))
    cur.execute('DELETE FROM user_cards WHERE card_id = ?', (cid,))
    conn.commit(); conn.close()
    await update.message.reply_text(f'Deleted card {cid} and removed from user collections')

async def addsudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    if update.message.reply_to_message:
        tid = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            tid = int(context.args[0])
        except:
            await update.message.reply_text('Usage: reply to a user or give user id')
            return
    else:
        await update.message.reply_text('Usage: reply to a user or give user id')
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO sudoers (telegram_id) VALUES (?)', (tid,))
    conn.commit(); conn.close()
    await update.message.reply_text(f'Added sudo: {tid}')

async def sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT telegram_id FROM sudoers')
    rows = cur.fetchall(); conn.close()
    text = 'Sudo list:\n' + '\n'.join(str(r['telegram_id']) for r in rows)
    await update.message.reply_text(text)

# --- Top leaderboard ---
async def tops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('Top by coins', callback_data='tops_coins'), InlineKeyboardButton('Top by cards', callback_data='tops_cards')]])
    await update.message.reply_text('Choose leaderboard:', reply_markup=kb)

async def tops_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == 'tops_coins':
        conn = get_conn(); cur = conn.cursor()
        cur.execute('SELECT username, coins FROM users ORDER BY coins DESC LIMIT 10')
        rows = cur.fetchall(); conn.close()
        text = '\n'.join([f"{i+1}. {r['username'] or r['telegram_id']}: {r['coins']}" for i, r in enumerate(rows)])
        await query.edit_message_text(text)
    else:
        conn = get_conn(); cur = conn.cursor()
        cur.execute('SELECT u.username, COUNT(uc.card_id) as c FROM users u LEFT JOIN user_cards uc ON uc.user_id = u.id GROUP BY u.id ORDER BY c DESC LIMIT 10')
        rows = cur.fetchall(); conn.close()
        text = '\n'.join([f"{i+1}. {r['username'] or 'id:'+str(i)}: {r['c']} cards" for i, r in enumerate(rows)])
        await query.edit_message_text(text)

# --- Broadcast ---
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text('Unauthorized')
        return
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text('Usage: reply to a message to broadcast it to groups or use /broadcast <text>')
        return
    # send to all groups in DB
    conn = get_conn(); cur = conn.cursor()
    cur.execute('SELECT chat_id FROM groups')
    rows = cur.fetchall()
    conn.close()
    sent = 0
    for r in rows:
        cid = r['chat_id']
        try:
            if update.message.reply_to_message:
                # forward the replied message
                await context.bot.forward_message(cid, update.message.chat_id, update.message.reply_to_message.message_id)
            else:
                await context.bot.send_message(cid, ' '.join(context.args))
            sent += 1
        except Exception:
            continue
    await update.message.reply_text(f'Broadcast sent to {sent} groups')

# --- Main entrypoint ---

def main():
    if not BOT_TOKEN:
        logger.error('BOT_TOKEN not set in environment')
        return
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # user commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('helps', helps))
    app.add_handler(CommandHandler('balance', balance))
    app.add_handler(CommandHandler('daily', daily))
    app.add_handler(CommandHandler('shop', shop))
    app.add_handler(CallbackQueryHandler(shop_callback, pattern='^shop_'))

    app.add_handler(CommandHandler('harem', harem))
    app.add_handler(CallbackQueryHandler(harem_page_cb, pattern='^harem_page:'))
    app.add_handler(CommandHandler('set', setfav))
    app.add_handler(CommandHandler('slots', slots))
    app.add_handler(CommandHandler('basket', basket))
    app.add_handler(CommandHandler('givecoin', givecoin))

    app.add_handler(CommandHandler('slime', slime))

    # admin
    app.add_handler(CommandHandler('upload', upload))
    app.add_handler(CommandHandler('setdrop', setdrop))
    app.add_handler(CommandHandler('gift', gift))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('delete', delete_card))
    app.add_handler(CommandHandler('addsudo', addsudo))
    app.add_handler(CommandHandler('sudolist', sudolist))
    app.add_handler(CommandHandler('broadcast', broadcast))

    # misc
    app.add_handler(CommandHandler('helps', helps))
    app.add_handler(CommandHandler('tops', tops))
    app.add_handler(CallbackQueryHandler(tops_cb, pattern='^tops_'))

    # message handlers
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), group_message_counter))

    logger.info('Bot starting...')
    app.run_polling()


if __name__ == '__main__':
    main()
