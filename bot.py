import os
import re
import asyncio
import shutil
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from db import Database, default_price_for_rarity

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "./data/bot.sqlite3").strip()
START_COINS = int(os.getenv("START_COINS", "200"))
DAILY_MIN = int(os.getenv("DAILY_MIN", "80"))
DAILY_MAX = int(os.getenv("DAILY_MAX", "160"))
DEFAULT_DROP_EVERY = int(os.getenv("DEFAULT_DROP_EVERY", "0"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing in .env")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("card-bot")

db = Database(DB_PATH)

SHOP_STATE = {}  # user_id -> card_id


# ---------------- Helpers ----------------
def mention_html(user) -> str:
    name = (user.full_name or "User").replace("<", "").replace(">", "")
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

def clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def is_sudo_or_admin(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    return await db.is_sudo(user_id)

def require_group(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

def parse_target_user_and_amount(update: Update, args: List[str]) -> Tuple[Optional[int], Optional[int]]:
    """
    Accept patterns:
      - reply + amount: /givecoin 100
      - explicit id + amount: /givecoin 123456 100
    """
    target_id = None
    amount = None

    if update.message and update.message.reply_to_message and len(args) == 1:
        target_id = update.message.reply_to_message.from_user.id
        amount = int(args[0])
        return target_id, amount

    if len(args) >= 2:
        target_id = int(args[0])
        amount = int(args[1])
        return target_id, amount

    return None, None

async def ensure_user(update: Update):
    u = update.effective_user
    await db.upsert_user(u.id, u.username or "", u.first_name or "", START_COINS)

async def ensure_group_known(update: Update):
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await db.upsert_group(chat.id, chat.title or "Group")

def fmt_card_line(card: dict, own_idx: int, total_cards: int) -> str:
    # [Movie name + (own:[2/25]) + Card ID + rarity emoji + character name]
    return f"🎬 <b>{card['movie']}</b>  (own:[{own_idx}/{total_cards}])\n🆔 <code>{card['id']}</code>  {card['rarity_emoji']} <b>{card['name']}</b>"

def harem_keyboard(page: int, pages: int):
    btns = []
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"harem:{page-1}"))
    row.append(InlineKeyboardButton(f"📄 {page}/{pages}", callback_data="noop"))
    if page < pages:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"harem:{page+1}"))
    btns.append(row)
    btns.append([InlineKeyboardButton("⭐ Set Fav (use /set <id>)", callback_data="noop")])
    return InlineKeyboardMarkup(btns)

def tops_keyboard(active: str = "coins"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪙 Top Coins", callback_data="tops:coins"),
            InlineKeyboardButton("🃏 Top Cards", callback_data="tops:cards"),
        ],
        [InlineKeyboardButton(f"✅ Showing: {active}", callback_data="noop")]
    ])

def shop_keyboard(card_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛒 Buy", callback_data=f"shop:buy:{card_id}"),
            InlineKeyboardButton("⏭️ Next", callback_data="shop:next"),
        ]
    ])

def vote_keyboard(options: List[dict]):
    rows = []
    for opt in options:
        rows.append([InlineKeyboardButton(f"🗳️ {opt['name']}", callback_data=f"vote:{opt['option_id']}")])
    rows.append([InlineKeyboardButton("📊 Results", callback_data="vote:results")])
    return InlineKeyboardMarkup(rows)

async def render_vote_status(chat_id: int, user_id: int) -> str:
    results = await db.vote_results(chat_id)
    total = sum(r["votes"] for r in results) if results else 0
    uv = await db.user_vote(chat_id, user_id)

    lines = ["<b>📊 Vote Results</b>"]
    for r in results:
        mark = "✅" if uv == r["option_id"] else "▫️"
        lines.append(f"{mark} <b>{r['name']}</b> — <code>{r['votes']}</code>")
    lines.append(f"\n👥 Total Votes: <b>{total}</b>")
    if uv:
        chosen = next((x["name"] for x in results if x["option_id"] == uv), None)
        if chosen:
            lines.append(f"🧾 You voted: <b>{chosen}</b>")
    return "\n".join(lines)


# ---------------- User Commands ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    text = (
        "🎴 <b>Character Collection Game</b>\n"
        "စုဆောင်း၊ ကစား၊ ဝယ်ယူ၊ Top အောင်မြင်သူဖြစ်လာပါ!\n\n"
        "Rarity System:\n"
        "🟤Common | 🟡Rare | 🔮Epic | ⚡Legendary | 👑Mythic\n\n"
        "👉 Commands ကြည့်ရန်: /helps\n\n"
        "<i>Create by : @Enoch_777</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def helps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)
    text = (
        "📌 <b>User Commands</b>\n\n"
        "🚀 /start - စတင်အသုံးပြုရန်\n"
        "🧾 /helps - commands များ\n"
        "🗂️ /harem - သင့် cards collection (5 cards/page)\n"
        "⭐ /set &lt;card id&gt; - fav character သတ်မှတ်\n\n"
        "🟩 /slime &lt;character name&gt; - drop ကဒ်ကို နာမည်မှန်ရေးပြီးယူ\n\n"
        "🎰 /slots &lt;amount&gt; - bet game (2×/3×)\n"
        "🏀 /basket &lt;amount&gt; - bet game (2×/3×)\n\n"
        "🪙 /balance - coin လက်ကျန်\n"
        "🎁 /daily - နေ့စဉ် bonus\n"
        "🛍️ /shop - character ဝယ်ယူ\n"
        "🏆 /tops - Top 10 (coins/cards)\n\n"
        "🤝 /givecoin &lt;reply or id&gt; &lt;amount&gt; - coin လွဲပေး\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return

    text = (
        "🛠️ <b>Admin / Sudo Commands</b>\n\n"
        "📤 /upload (reply photo) rarity | movie | name | price(optional)\n"
        "⏱️ /setdrop &lt;number&gt; - group drop message count\n"
        "🎁 /gift coin &lt;amount&gt; &lt;reply or id&gt;\n"
        "🎁 /gift card &lt;amount&gt; &lt;reply or id&gt;  (random)\n\n"
        "📣 /broadcast (reply text/photo) OR /broadcast your text\n"
        "📊 /stats - users & groups list summary\n"
        "💾 /backup - db backup\n"
        "♻️ /restore (reply db file) - restore db\n"
        "🧨 /allclear - delete all data\n"
        "🗑️ /delete &lt;card id&gt; - delete one card\n\n"
        "👑 /addsudo &lt;reply or id&gt; - add sudo\n"
        "📜 /sudolist - list sudo\n\n"
        "🗳️ /evote name1 | name2 | name3 - set vote options (group)\n"
        "✅ /vote - vote buttons\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    user = await db.get_user(update.effective_user.id)
    await update.message.reply_text(f"🪙 <b>Your Balance</b>\nCoins: <code>{user['coins']}</code>", parse_mode=ParseMode.HTML)

async def daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    u = await db.get_user(update.effective_user.id)
    last = u.get("last_daily")

    now = datetime.now(timezone.utc)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            last_dt = None
        if last_dt and (now - last_dt) < timedelta(hours=24):
            remain = timedelta(hours=24) - (now - last_dt)
            hh = int(remain.total_seconds() // 3600)
            mm = int((remain.total_seconds() % 3600) // 60)
            await update.message.reply_text(f"⏳ Daily bonus ကို နောက်ထပ် <b>{hh}h {mm}m</b> နောက်မှ ယူနိုင်ပါမယ်။", parse_mode=ParseMode.HTML)
            return

    bonus = random.randint(DAILY_MIN, DAILY_MAX)
    await db.add_coins(update.effective_user.id, bonus)
    await db.set_last_daily(update.effective_user.id, now.isoformat())
    await update.message.reply_text(f"🎁 <b>Daily Bonus</b>\nYou got: <code>+{bonus}</code> coins!", parse_mode=ParseMode.HTML)

async def givecoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    args = context.args
    try:
        target_id, amount = parse_target_user_and_amount(update, args)
    except Exception:
        await update.message.reply_text("အသုံးပြုပုံ: /givecoin <reply or id> <amount>")
        return

    if not target_id or not amount or amount <= 0:
        await update.message.reply_text("အသုံးပြုပုံ: /givecoin <reply or id> <amount>")
        return

    sender_id = update.effective_user.id
    if target_id == sender_id:
        await update.message.reply_text("🫤 ကိုယ့်ကိုကိုယ် coin ပေးလို့မရပါ။")
        return

    sender = await db.get_user(sender_id)
    if sender["coins"] < amount:
        await update.message.reply_text("⛔ Coins မလုံလောက်ပါ။")
        return

    await db.upsert_user(target_id, "", "User", START_COINS)  # ensure exists
    await db.add_coins(sender_id, -amount)
    await db.add_coins(target_id, amount)
    await update.message.reply_text(
        f"✅ {mention_html(update.effective_user)} → <code>{target_id}</code>\n🪙 Sent: <b>{amount}</b>",
        parse_mode=ParseMode.HTML
    )

async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /set <card id>")
        return
    try:
        card_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Card ID မှန်မှန်ရေးပါ။ ဥပမာ: /set 12")
        return

    card = await db.get_card(card_id)
    if not card:
        await update.message.reply_text("⛔ ဒီ Card ID မရှိပါ။")
        return

    await db.set_fav(update.effective_user.id, card_id)
    await update.message.reply_text(f"⭐ Fav set!\n🆔 <code>{card_id}</code> {card['rarity_emoji']} <b>{card['name']}</b>", parse_mode=ParseMode.HTML)

async def harem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    total_cards = await db.count_distinct_cards()
    page = 1
    cards, pages = await db.inventory_page(update.effective_user.id, page, page_size=5)

    if not cards:
        await update.message.reply_text("📭 Harem က kosong ပါသေးတယ်။ Drop ယူပါ သို့မဟုတ် /shop မှာ ဝယ်နိုင်ပါတယ်။")
        return

    lines = [f"🗂️ <b>{mention_html(update.effective_user)}'s Harem</b>\n"]
    for idx, c in enumerate(cards, start=1):
        lines.append(fmt_card_line(c, idx, total_cards))
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines).strip(),
        parse_mode=ParseMode.HTML,
        reply_markup=harem_keyboard(page, pages),
        disable_web_page_preview=True,
    )

async def shop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    card = await db.random_shop_card()
    if not card:
        await update.message.reply_text("🛍️ Shop ထဲမှာ card မရှိသေးပါ။ Admin /upload လုပ်ပြီး price ထည့်ပေးရပါမယ်။")
        return

    SHOP_STATE[update.effective_user.id] = card["id"]
    txt = (
        "🛍️ <b>Shop</b>\n\n"
        f"🎬 <b>{card['movie']}</b>\n"
        f"🆔 <code>{card['id']}</code>\n"
        f"{card['rarity_emoji']} <b>{card['rarity']}</b>\n"
        f"👤 <b>{card['name']}</b>\n\n"
        f"💰 Price: <code>{card['price']}</code> coins\n"
        "—\n"
        "Buy မလုပ်ချင်ရင် Next နှိပ်ပြီး ပြောင်းနိုင်ပါတယ်။"
    )
    if card.get("file_id"):
        await update.message.reply_photo(card["file_id"], caption=txt, parse_mode=ParseMode.HTML, reply_markup=shop_keyboard(card["id"]))
    else:
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=shop_keyboard(card["id"]))

async def tops_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    top = await db.top_coins(10)
    lines = ["🏆 <b>Top 10 — Coins</b>\n"]
    for i, r in enumerate(top, start=1):
        name = r["username"] or r["first_name"] or str(r["user_id"])
        lines.append(f"{i:02d}. <b>{name}</b> — <code>{r['coins']}</code> 🪙")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=tops_keyboard("coins"))

async def slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /slots <amount>")
        return
    try:
        bet = int(context.args[0])
    except Exception:
        await update.message.reply_text("Bet amount ကို number ဖြင့်ရေးပါ။")
        return
    if bet <= 0:
        await update.message.reply_text("Bet amount > 0 ဖြစ်ရပါမယ်။")
        return

    u = await db.get_user(update.effective_user.id)
    if u["coins"] < bet:
        await update.message.reply_text("⛔ Coins မလုံလောက်ပါ။")
        return

    symbols = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
    msg = await update.message.reply_text("🎰 Spinning: [ ? | ? | ? ]")

    await asyncio.sleep(0.6)
    a = random.choice(symbols); b = random.choice(symbols); c = random.choice(symbols)
    await msg.edit_text(f"🎰 Spinning: [ {a} | ? | ? ]")
    await asyncio.sleep(0.6)
    await msg.edit_text(f"🎰 Spinning: [ {a} | {b} | ? ]")
    await asyncio.sleep(0.6)
    await msg.edit_text(f"🎰 Result:   [ {a} | {b} | {c} ]")

    mult = 0
    if a == b == c:
        mult = 3
    elif a == b or b == c or a == c:
        mult = 2

    if mult == 0:
        await db.add_coins(update.effective_user.id, -bet)
        await update.message.reply_text(f"❌ You lose: <code>-{bet}</code> coins", parse_mode=ParseMode.HTML)
    else:
        win = bet * mult
        profit = win - bet
        await db.add_coins(update.effective_user.id, profit)
        await update.message.reply_text(f"✅ You win <b>{mult}×</b>!\nProfit: <code>+{profit}</code> coins", parse_mode=ParseMode.HTML)

async def basket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /basket <amount>")
        return
    try:
        bet = int(context.args[0])
    except Exception:
        await update.message.reply_text("Bet amount ကို number ဖြင့်ရေးပါ။")
        return
    if bet <= 0:
        await update.message.reply_text("Bet amount > 0 ဖြစ်ရပါမယ်။")
        return

    u = await db.get_user(update.effective_user.id)
    if u["coins"] < bet:
        await update.message.reply_text("⛔ Coins မလုံလောက်ပါ။")
        return

    msg = await update.message.reply_text("🏀 Shooting... ⛹️‍♂️")
    await asyncio.sleep(0.9)
    r = random.random()
    if r < 0.12:
        mult = 3
        await msg.edit_text("🏀✨ SWISH! (3×)")
    elif r < 0.40:
        mult = 2
        await msg.edit_text("🏀✅ Score! (2×)")
    else:
        mult = 0
        await msg.edit_text("🏀❌ Miss!")

    if mult == 0:
        await db.add_coins(update.effective_user.id, -bet)
        await update.message.reply_text(f"❌ You lose: <code>-{bet}</code> coins", parse_mode=ParseMode.HTML)
    else:
        win = bet * mult
        profit = win - bet
        await db.add_coins(update.effective_user.id, profit)
        await update.message.reply_text(f"✅ You win <b>{mult}×</b>!\nProfit: <code>+{profit}</code> coins", parse_mode=ParseMode.HTML)

async def slime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if not require_group(update):
        await update.message.reply_text("🟩 /slime ကို Group ထဲမှာပဲ သုံးပါ (drop ကနေ claim လုပ်ဖို့)။")
        return

    chat_id = update.effective_chat.id
    pending = await db.get_pending_drop(chat_id)
    if not pending:
        await update.message.reply_text("📭 Claim လုပ်စရာ drop မရှိသေးပါ။")
        return

    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /slime <character name>")
        return

    guess = clean_name(" ".join(context.args))
    card = await db.get_card(pending["card_id"])
    if not card:
        await db.clear_pending_drop(chat_id)
        await update.message.reply_text("⚠️ Drop card မတွေ့လို့ reset လုပ်လိုက်ပါတယ်။")
        return

    real = clean_name(card["name"])
    if guess != real:
        await update.message.reply_text("❌ Name မမှန်ပါ။ တိတိကျကျ name အမှန်ရေးပါ။")
        return

    # Claim success
    await db.add_to_inventory(update.effective_user.id, card["id"])
    await db.clear_pending_drop(chat_id)

    # Try edit original drop message
    try:
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=pending["message_id"],
            caption=(
                "🎴 <b>Card Claimed!</b>\n"
                f"{card['rarity_emoji']} <b>{card['name']}</b>\n"
                f"🎬 <b>{card['movie']}</b>\n"
                f"🆔 <code>{card['id']}</code>\n\n"
                f"✅ Claimed by: {mention_html(update.effective_user)}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # If it wasn't a photo message, ignore
        pass

    await update.message.reply_text(
        f"✅ Congrats {mention_html(update.effective_user)}!\nYou got: {card['rarity_emoji']} <b>{card['name']}</b>  (🆔 <code>{card['id']}</code>)",
        parse_mode=ParseMode.HTML,
    )


# ---------------- Admin / Sudo Commands ----------------
async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text(
            "အသုံးပြုပုံ:\n"
            "1) Photo ကို reply လုပ်\n"
            "2) /upload rarity | movie | name | price(optional)\n\n"
            "ဥပမာ:\n"
            "/upload Epic | One Piece | Luffy | 500"
        )
        return

    raw = " ".join(context.args).strip()
    parts = [p.strip() for p in raw.split("|")] if raw else []
    if len(parts) < 3:
        await update.message.reply_text("Format မမှန်ပါ။ /upload rarity | movie | name | price(optional)")
        return

    rarity = parts[0]
    movie = parts[1]
    name = parts[2]
    price = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else default_price_for_rarity(rarity)

    photo = update.message.reply_to_message.photo[-1]
    file_id = photo.file_id

    card_id = await db.create_card(name=name, movie=movie, rarity=rarity, price=price, file_id=file_id, added_by=uid)
    card = await db.get_card(card_id)

    await update.message.reply_text(
        "✅ <b>Card Uploaded</b>\n"
        f"🆔 <code>{card_id}</code>\n"
        f"{card['rarity_emoji']} <b>{card['rarity']}</b>\n"
        f"🎬 <b>{movie}</b>\n"
        f"👤 <b>{name}</b>\n"
        f"💰 Price: <code>{price}</code>",
        parse_mode=ParseMode.HTML
    )

async def setdrop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return
    if not require_group(update):
        await update.message.reply_text("ဒီ command ကို Group ထဲမှာပဲ သုံးပါ။")
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /setdrop <number>  (0=off)")
        return
    try:
        n = int(context.args[0])
    except Exception:
        await update.message.reply_text("Number ဖြင့်ရေးပါ။")
        return
    if n < 0:
        n = 0

    chat_id = update.effective_chat.id
    await db.ensure_chat(chat_id, DEFAULT_DROP_EVERY)
    await db.set_drop_every(chat_id, n)
    await db.reset_msg_count(chat_id)
    await update.message.reply_text(f"✅ Drop set: <b>{n}</b> messages (0=off)", parse_mode=ParseMode.HTML)

async def gift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return

    if len(context.args) < 3:
        await update.message.reply_text("အသုံးပြုပုံ:\n/gift coin <amount> <reply or id>\n/gift card <amount> <reply or id>")
        return

    kind = context.args[0].lower()
    try:
        amount = int(context.args[1])
    except Exception:
        await update.message.reply_text("Amount ကို number ဖြင့်ရေးပါ။")
        return

    # target user
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    else:
        try:
            target_id = int(context.args[2])
        except Exception:
            await update.message.reply_text("Target ကို reply သို့မဟုတ် id ဖြင့်ပေးပါ။")
            return

    await db.upsert_user(target_id, "", "User", START_COINS)

    if kind == "coin":
        if amount <= 0:
            await update.message.reply_text("Amount > 0 ဖြစ်ရပါမယ်။")
            return
        await db.add_coins(target_id, amount)
        await update.message.reply_text(f"✅ Gifted <code>+{amount}</code> coins to <code>{target_id}</code>", parse_mode=ParseMode.HTML)
        return

    if kind == "card":
        if amount <= 0:
            await update.message.reply_text("Amount > 0 ဖြစ်ရပါမယ်။")
            return
        got = 0
        for _ in range(amount):
            c = await db.random_card()
            if not c:
                break
            await db.add_to_inventory(target_id, c["id"])
            got += 1
        await update.message.reply_text(f"✅ Gifted <b>{got}</b> random cards to <code>{target_id}</code>", parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text("Kind မမှန်ပါ။ coin / card ထဲကတစ်ခုကိုသုံးပါ။")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return
    s = await db.stats()
    await update.message.reply_text(
        "📊 <b>Stats</b>\n"
        f"👤 Users: <code>{s['users']}</code>\n"
        f"👥 Groups: <code>{s['groups']}</code>\n"
        f"🎴 Cards: <code>{s['cards']}</code>\n"
        f"🗂️ Inventory rows: <code>{s['inventory']}</code>",
        parse_mode=ParseMode.HTML
    )

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return

    groups = await db.list_groups()
    if not groups:
        await update.message.reply_text("Group list မရှိသေးပါ။")
        return

    src = update.message.reply_to_message if update.message.reply_to_message else update.message
    text = None
    photo_file_id = None

    if src.photo:
        photo_file_id = src.photo[-1].file_id
        text = src.caption or (" ".join(context.args) if context.args else "")
    else:
        text = " ".join(context.args).strip()

    if not photo_file_id and not text:
        await update.message.reply_text("အသုံးပြုပုံ:\n/broadcast your text\nသို့မဟုတ်\ntext/photo ကို reply လုပ်ပြီး /broadcast")
        return

    ok = 0
    fail = 0
    for gid in groups:
        try:
            if photo_file_id:
                await context.bot.send_photo(chat_id=gid, photo=photo_file_id, caption=text or "", parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=gid, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await update.message.reply_text(f"📣 Broadcast done.\n✅ Sent: {ok}\n⚠️ Failed: {fail}")

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return

    os.makedirs("./data", exist_ok=True)
    os.makedirs("./backups", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = f"./backups/bot_backup_{stamp}.sqlite3"

    # safe-ish online backup using VACUUM INTO (needs modern sqlite)
    try:
        await db.conn.execute(f"VACUUM INTO '{out}'")
        await db.conn.commit()
    except Exception:
        # fallback to file copy
        await db.conn.commit()
        shutil.copyfile(DB_PATH, out)

    await update.message.reply_document(document=open(out, "rb"), filename=os.path.basename(out), caption="💾 DB Backup")

async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Restore လုပ်ရန် backup file ကို reply လုပ်ပြီး /restore ပြုလုပ်ပါ။")
        return

    doc = update.message.reply_to_message.document
    file = await context.bot.get_file(doc.file_id)

    tmp = "./data/restore_tmp.sqlite3"
    await file.download_to_drive(custom_path=tmp)

    # swap DB
    await db.close()
    shutil.copyfile(tmp, DB_PATH)
    await db.connect()
    await db.init_schema()
    await update.message.reply_text("♻️ Restore complete. ✅")

async def allclear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return

    await db.close()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    await db.connect()
    await db.init_schema()
    await update.message.reply_text("🧨 All data cleared. DB re-initialized.")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /delete <card id>")
        return
    try:
        card_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Card ID ကို number ဖြင့်ရေးပါ။")
        return
    ok = await db.delete_card(card_id)
    await update.message.reply_text("✅ Deleted." if ok else "⚠️ Card ID မတွေ့ပါ။")

async def addsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return

    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except Exception:
            target_id = None

    if not target_id:
        await update.message.reply_text("အသုံးပြုပုံ: /addsudo <reply or id>")
        return

    await db.add_sudo(target_id)
    await update.message.reply_text(f"✅ Sudo added: <code>{target_id}</code>", parse_mode=ParseMode.HTML)

async def sudolist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admin only.")
        return

    ids = await db.sudo_list()
    if not ids:
        await update.message.reply_text("📜 Sudo list: (empty)")
        return
    text = "📜 <b>Sudo list</b>\n" + "\n".join([f"• <code>{i}</code>" for i in ids])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def evote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)

    uid = update.effective_user.id
    if not (await is_sudo_or_admin(uid)):
        await update.message.reply_text("⛔ Admin/Sudo မဟုတ်ပါ။")
        return
    if not require_group(update):
        await update.message.reply_text("ဒီ command ကို Group ထဲမှာပဲ သုံးပါ။")
        return

    raw = " ".join(context.args).strip()
    names = [x.strip() for x in raw.split("|") if x.strip()]
    if len(names) < 2:
        await update.message.reply_text("အသုံးပြုပုံ: /evote name1 | name2 | name3")
        return

    await db.set_vote(update.effective_chat.id, names)
    opts = await db.get_vote_options(update.effective_chat.id)
    await update.message.reply_text("🗳️ Vote options set ✅\n/vote ဖြင့် မဲပေးနိုင်ပါပြီ။", reply_markup=vote_keyboard(opts))

async def vote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await ensure_group_known(update)
    if not require_group(update):
        await update.message.reply_text("ဒီ command ကို Group ထဲမှာပဲ သုံးပါ။")
        return
    opts = await db.get_vote_options(update.effective_chat.id)
    if not opts:
        await update.message.reply_text("🗳️ Vote မစတင်သေးပါ။ Admin က /evote ဖြင့် options ထည့်ပါ။")
        return
    await update.message.reply_text("🗳️ မဲပေးရန် button ကိုနှိပ်ပါ:", reply_markup=vote_keyboard(opts))


# ---------------- Callback handlers ----------------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if data == "noop":
        return

    if data.startswith("harem:"):
        await db.upsert_user(user_id, query.from_user.username or "", query.from_user.first_name or "", START_COINS)
        total_cards = await db.count_distinct_cards()
        page = int(data.split(":")[1])
        cards, pages = await db.inventory_page(user_id, page, page_size=5)
        if not cards:
            await query.edit_message_text("📭 Harem kosong ပါသေးတယ်။")
            return
        lines = [f"🗂️ <b>{mention_html(query.from_user)}'s Harem</b>\n"]
        for idx, c in enumerate(cards, start=1):
            lines.append(fmt_card_line(c, idx, total_cards))
            lines.append("")
        await query.edit_message_text(
            "\n".join(lines).strip(),
            parse_mode=ParseMode.HTML,
            reply_markup=harem_keyboard(page, pages),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("tops:"):
        mode = data.split(":")[1]
        if mode == "coins":
            top = await db.top_coins(10)
            lines = ["🏆 <b>Top 10 — Coins</b>\n"]
            for i, r in enumerate(top, start=1):
                name = r["username"] or r["first_name"] or str(r["user_id"])
                lines.append(f"{i:02d}. <b>{name}</b> — <code>{r['coins']}</code> 🪙")
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=tops_keyboard("coins"))
        else:
            top = await db.top_cards(10)
            lines = ["🏆 <b>Top 10 — Cards</b>\n"]
            for i, r in enumerate(top, start=1):
                name = r["username"] or r["first_name"] or str(r["user_id"])
                lines.append(f"{i:02d}. <b>{name}</b> — <code>{r['cnt']}</code> 🃏")
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=tops_keyboard("cards"))
        return

    if data == "shop:next":
        await db.upsert_user(user_id, query.from_user.username or "", query.from_user.first_name or "", START_COINS)
        card = await db.random_shop_card()
        if not card:
            await query.edit_message_text("🛍️ Shop empty.")
            return
        SHOP_STATE[user_id] = card["id"]
        txt = (
            "🛍️ <b>Shop</b>\n\n"
            f"🎬 <b>{card['movie']}</b>\n"
            f"🆔 <code>{card['id']}</code>\n"
            f"{card['rarity_emoji']} <b>{card['rarity']}</b>\n"
            f"👤 <b>{card['name']}</b>\n\n"
            f"💰 Price: <code>{card['price']}</code> coins"
        )
        # If photo caption exists, edit caption; else edit text
        try:
            await query.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML, reply_markup=shop_keyboard(card["id"]))
        except Exception:
            await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=shop_keyboard(card["id"]))
        return

    if data.startswith("shop:buy:"):
        await db.upsert_user(user_id, query.from_user.username or "", query.from_user.first_name or "", START_COINS)
        card_id = int(data.split(":")[2])
        card = await db.get_card(card_id)
        if not card:
            await query.answer("Card not found", show_alert=True)
            return
        u = await db.get_user(user_id)
        if u["coins"] < card["price"]:
            await query.answer("Coins မလုံလောက်ပါ!", show_alert=True)
            return
        await db.add_coins(user_id, -card["price"])
        await db.add_to_inventory(user_id, card_id)
        await query.answer("Purchased ✅", show_alert=False)

        new_text = (
            "✅ <b>Purchased!</b>\n"
            f"{card['rarity_emoji']} <b>{card['name']}</b>\n"
            f"🎬 <b>{card['movie']}</b>\n"
            f"🆔 <code>{card['id']}</code>\n"
            f"💰 Paid: <code>{card['price']}</code>\n\n"
            "Next နှိပ်ပြီး ဆက်ကြည့်နိုင်ပါတယ်။"
        )
        try:
            await query.edit_message_caption(caption=new_text, parse_mode=ParseMode.HTML, reply_markup=shop_keyboard(card_id))
        except Exception:
            await query.edit_message_text(new_text, parse_mode=ParseMode.HTML, reply_markup=shop_keyboard(card_id))
        return

    if data.startswith("vote:"):
        if not (query.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)):
            await query.answer("Vote is for groups only.", show_alert=True)
            return

        action = data.split(":")[1]
        opts = await db.get_vote_options(chat_id)
        if not opts:
            await query.answer("Vote not set", show_alert=True)
            return

        if action == "results":
            txt = await render_vote_status(chat_id, user_id)
            await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=vote_keyboard(opts))
            return

        # option id
        try:
            option_id = int(action)
        except Exception:
            return

        valid_ids = {o["option_id"] for o in opts}
        if option_id not in valid_ids:
            await query.answer("Invalid option", show_alert=True)
            return

        await db.cast_vote(chat_id, user_id, option_id)
        txt = await render_vote_status(chat_id, user_id)
        await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=vote_keyboard(opts))
        return


# ---------------- Drop System (message counter) ----------------
async def group_message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not update.message or update.message.from_user.is_bot:
        return

    await ensure_group_known(update)
    await db.ensure_chat(update.effective_chat.id, DEFAULT_DROP_EVERY)

    drop_every = await db.get_drop_every(update.effective_chat.id)
    if drop_every <= 0:
        return

    pending = await db.get_pending_drop(update.effective_chat.id)
    if pending:
        # one pending at a time
        return

    count = await db.inc_msg_count(update.effective_chat.id)
    if count < drop_every:
        return

    await db.reset_msg_count(update.effective_chat.id)

    card = await db.random_card()
    if not card:
        # no cards uploaded yet
        return

    name = card["name"]
    hint = f"{name[:1]}{'•' * max(0, len(name)-2)}{name[-1:]}" if len(name) >= 2 else name[:1]

    caption = (
        "🎴 <b>Card Dropped!</b>\n"
        f"🎬 <b>{card['movie']}</b>\n"
        f"🆔 <code>{card['id']}</code>\n"
        f"{card['rarity_emoji']} <b>{card['rarity']}</b>\n"
        f"❓ Name: <b>{hint}</b>\n\n"
        "🟩 Claim: <code>/slime Character Name</code>\n"
        "(name အမှန်တိတိကျကျရေးပါ)"
    )

    if card.get("file_id"):
        m = await update.message.reply_photo(photo=card["file_id"], caption=caption, parse_mode=ParseMode.HTML)
    else:
        m = await update.message.reply_text(caption, parse_mode=ParseMode.HTML)

    await db.set_pending_drop(update.effective_chat.id, card["id"], m.message_id)


# ---------------- Main ----------------
async def on_startup(app: Application):
    await db.connect()
    await db.init_schema()
    log.info("DB ready")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    # User
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("helps", helps_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("daily", daily_cmd))
    app.add_handler(CommandHandler("givecoin", givecoin_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("harem", harem_cmd))
    app.add_handler(CommandHandler("shop", shop_cmd))
    app.add_handler(CommandHandler("tops", tops_cmd))
    app.add_handler(CommandHandler("slots", slots_cmd))
    app.add_handler(CommandHandler("basket", basket_cmd))
    app.add_handler(CommandHandler("slime", slime_cmd))

    # Admin/Sudo
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("setdrop", setdrop_cmd))
    app.add_handler(CommandHandler("gift", gift_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("restore", restore_cmd))
    app.add_handler(CommandHandler("allclear", allclear_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("addsudo", addsudo_cmd))
    app.add_handler(CommandHandler("sudolist", sudolist_cmd))
    app.add_handler(CommandHandler("evote", evote_cmd))
    app.add_handler(CommandHandler("vote", vote_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callbacks))

    # Group message counter (drops)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), group_message_counter))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
