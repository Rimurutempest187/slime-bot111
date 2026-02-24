import aiosqlite
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
import random

RARITIES = [
    ("Common", "🟤", 60),
    ("Rare", "🟡", 25),
    ("Epic", "🔮", 10),
    ("Legendary", "⚡", 4),
    ("Mythic", "👑", 1),
]

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def rarity_emoji(rarity: str) -> str:
    for r, e, _w in RARITIES:
        if r.lower() == rarity.lower():
            return e
    return "🟤"

def weighted_rarity() -> Tuple[str, str]:
    choices = []
    for r, e, w in RARITIES:
        choices.append((r, e, w))
    total = sum(w for _r, _e, w in choices)
    pick = random.uniform(0, total)
    upto = 0
    for r, e, w in choices:
        if upto + w >= pick:
            return r, e
        upto += w
    return "Common", "🟤"

def default_price_for_rarity(rarity: str) -> int:
    rl = rarity.lower()
    if rl == "common": return 120
    if rl == "rare": return 220
    if rl == "epic": return 420
    if rl == "legendary": return 900
    if rl == "mythic": return 1600
    return 120


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
        self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("DB not connected")
        return self._conn

    async def init_schema(self):
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              user_id INTEGER PRIMARY KEY,
              username TEXT,
              first_name TEXT,
              coins INTEGER NOT NULL DEFAULT 0,
              fav_card_id INTEGER,
              last_daily TEXT
            );

            CREATE TABLE IF NOT EXISTS cards(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              movie TEXT NOT NULL,
              rarity TEXT NOT NULL,
              rarity_emoji TEXT NOT NULL,
              price INTEGER NOT NULL DEFAULT 0,
              file_id TEXT,
              added_by INTEGER,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              card_id INTEGER NOT NULL,
              obtained_at TEXT NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
              FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id);
            CREATE INDEX IF NOT EXISTS idx_inventory_card ON inventory(card_id);

            CREATE TABLE IF NOT EXISTS chat_settings(
              chat_id INTEGER PRIMARY KEY,
              drop_every INTEGER NOT NULL DEFAULT 0,
              msg_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pending_drops(
              chat_id INTEGER PRIMARY KEY,
              card_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS groups(
              chat_id INTEGER PRIMARY KEY,
              title TEXT,
              added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sudo(
              user_id INTEGER PRIMARY KEY,
              added_at TEXT NOT NULL
            );

            -- Voting (per chat, one active poll)
            CREATE TABLE IF NOT EXISTS vote_polls(
              chat_id INTEGER PRIMARY KEY,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vote_options(
              chat_id INTEGER NOT NULL,
              option_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              PRIMARY KEY(chat_id, option_id),
              FOREIGN KEY(chat_id) REFERENCES vote_polls(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS vote_votes(
              chat_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              option_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(chat_id, user_id),
              FOREIGN KEY(chat_id) REFERENCES vote_polls(chat_id) ON DELETE CASCADE
            );
            """
        )
        await self.conn.commit()

    # ---------- Users ----------
    async def upsert_user(self, user_id: int, username: str, first_name: str, start_coins: int):
        cur = await self.conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row:
            await self.conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (username, first_name, user_id),
            )
        else:
            await self.conn.execute(
                "INSERT INTO users(user_id, username, first_name, coins) VALUES(?,?,?,?)",
                (user_id, username, first_name, start_coins),
            )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> Optional[Dict]:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_coins(self, user_id: int, delta: int):
        await self.conn.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (delta, user_id))
        await self.conn.commit()

    async def set_fav(self, user_id: int, card_id: Optional[int]):
        await self.conn.execute("UPDATE users SET fav_card_id=? WHERE user_id=?", (card_id, user_id))
        await self.conn.commit()

    async def set_last_daily(self, user_id: int, iso: str):
        await self.conn.execute("UPDATE users SET last_daily=? WHERE user_id=?", (iso, user_id))
        await self.conn.commit()

    # ---------- Cards ----------
    async def create_card(self, name: str, movie: str, rarity: str, price: int, file_id: Optional[str], added_by: int) -> int:
        e = rarity_emoji(rarity)
        cur = await self.conn.execute(
            "INSERT INTO cards(name,movie,rarity,rarity_emoji,price,file_id,added_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (name, movie, rarity, e, price, file_id, added_by, now_utc_iso()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def delete_card(self, card_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_card(self, card_id: int) -> Optional[Dict]:
        cur = await self.conn.execute("SELECT * FROM cards WHERE id=?", (card_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def random_card(self) -> Optional[Dict]:
        # pick rarity weighted then random card from that rarity; fallback any
        rarity, _e = weighted_rarity()
        cur = await self.conn.execute("SELECT * FROM cards WHERE lower(rarity)=lower(?) ORDER BY RANDOM() LIMIT 1", (rarity,))
        row = await cur.fetchone()
        if row:
            return dict(row)
        cur2 = await self.conn.execute("SELECT * FROM cards ORDER BY RANDOM() LIMIT 1")
        row2 = await cur2.fetchone()
        return dict(row2) if row2 else None

    async def random_shop_card(self) -> Optional[Dict]:
        cur = await self.conn.execute("SELECT * FROM cards WHERE price > 0 ORDER BY RANDOM() LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None

    # ---------- Inventory ----------
    async def add_to_inventory(self, user_id: int, card_id: int):
        await self.conn.execute(
            "INSERT INTO inventory(user_id, card_id, obtained_at) VALUES(?,?,?)",
            (user_id, card_id, now_utc_iso()),
        )
        await self.conn.commit()

    async def inventory_total(self, user_id: int) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM inventory WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def inventory_page(self, user_id: int, page: int, page_size: int = 5) -> Tuple[List[Dict], int]:
        total = await self.inventory_total(user_id)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        offset = (page - 1) * page_size

        cur = await self.conn.execute(
            """
            SELECT i.id AS inv_id, c.*
            FROM inventory i
            JOIN cards c ON c.id = i.card_id
            WHERE i.user_id=?
            ORDER BY i.id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, page_size, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows], pages

    async def count_distinct_cards(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS c FROM cards")
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    # ---------- Chat settings & drops ----------
    async def ensure_chat(self, chat_id: int, default_drop_every: int):
        cur = await self.conn.execute("SELECT chat_id FROM chat_settings WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        if not row:
            await self.conn.execute(
                "INSERT INTO chat_settings(chat_id, drop_every, msg_count) VALUES(?,?,0)",
                (chat_id, default_drop_every),
            )
            await self.conn.commit()

    async def set_drop_every(self, chat_id: int, n: int):
        await self.conn.execute("UPDATE chat_settings SET drop_every=? WHERE chat_id=?", (n, chat_id))
        await self.conn.commit()

    async def get_drop_every(self, chat_id: int) -> int:
        cur = await self.conn.execute("SELECT drop_every FROM chat_settings WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return int(row["drop_every"]) if row else 0

    async def inc_msg_count(self, chat_id: int) -> int:
        await self.conn.execute("UPDATE chat_settings SET msg_count = msg_count + 1 WHERE chat_id=?", (chat_id,))
        await self.conn.commit()
        cur = await self.conn.execute("SELECT msg_count FROM chat_settings WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return int(row["msg_count"]) if row else 0

    async def reset_msg_count(self, chat_id: int):
        await self.conn.execute("UPDATE chat_settings SET msg_count=0 WHERE chat_id=?", (chat_id,))
        await self.conn.commit()

    async def get_pending_drop(self, chat_id: int) -> Optional[Dict]:
        cur = await self.conn.execute("SELECT * FROM pending_drops WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_pending_drop(self, chat_id: int, card_id: int, message_id: int):
        await self.conn.execute(
            "INSERT OR REPLACE INTO pending_drops(chat_id, card_id, message_id, created_at) VALUES(?,?,?,?)",
            (chat_id, card_id, message_id, now_utc_iso()),
        )
        await self.conn.commit()

    async def clear_pending_drop(self, chat_id: int):
        await self.conn.execute("DELETE FROM pending_drops WHERE chat_id=?", (chat_id,))
        await self.conn.commit()

    # ---------- Groups ----------
    async def upsert_group(self, chat_id: int, title: str):
        await self.conn.execute(
            "INSERT OR REPLACE INTO groups(chat_id,title,added_at) VALUES(?,?,COALESCE((SELECT added_at FROM groups WHERE chat_id=?),?))",
            (chat_id, title, chat_id, now_utc_iso()),
        )
        await self.conn.commit()

    async def list_groups(self) -> List[int]:
        cur = await self.conn.execute("SELECT chat_id FROM groups")
        rows = await cur.fetchall()
        return [int(r["chat_id"]) for r in rows]

    async def stats(self) -> Dict:
        cur1 = await self.conn.execute("SELECT COUNT(*) AS c FROM users")
        u = int((await cur1.fetchone())["c"])
        cur2 = await self.conn.execute("SELECT COUNT(*) AS c FROM groups")
        g = int((await cur2.fetchone())["c"])
        cur3 = await self.conn.execute("SELECT COUNT(*) AS c FROM cards")
        c = int((await cur3.fetchone())["c"])
        cur4 = await self.conn.execute("SELECT COUNT(*) AS c FROM inventory")
        i = int((await cur4.fetchone())["c"])
        return {"users": u, "groups": g, "cards": c, "inventory": i}

    # ---------- Sudo ----------
    async def add_sudo(self, user_id: int):
        await self.conn.execute("INSERT OR REPLACE INTO sudo(user_id, added_at) VALUES(?,?)", (user_id, now_utc_iso()))
        await self.conn.commit()

    async def is_sudo(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT user_id FROM sudo WHERE user_id=?", (user_id,))
        return (await cur.fetchone()) is not None

    async def sudo_list(self) -> List[int]:
        cur = await self.conn.execute("SELECT user_id FROM sudo ORDER BY user_id")
        rows = await cur.fetchall()
        return [int(r["user_id"]) for r in rows]

    # ---------- Tops ----------
    async def top_coins(self, limit: int = 10) -> List[Dict]:
        cur = await self.conn.execute(
            "SELECT user_id, username, first_name, coins FROM users ORDER BY coins DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def top_cards(self, limit: int = 10) -> List[Dict]:
        cur = await self.conn.execute(
            """
            SELECT u.user_id, u.username, u.first_name, COUNT(i.id) AS cnt
            FROM users u
            LEFT JOIN inventory i ON i.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- Voting ----------
    async def set_vote(self, chat_id: int, names: List[str]):
        await self.conn.execute("INSERT OR REPLACE INTO vote_polls(chat_id, created_at) VALUES(?,?)", (chat_id, now_utc_iso()))
        await self.conn.execute("DELETE FROM vote_options WHERE chat_id=?", (chat_id,))
        await self.conn.execute("DELETE FROM vote_votes WHERE chat_id=?", (chat_id,))
        for idx, n in enumerate(names, start=1):
            await self.conn.execute(
                "INSERT INTO vote_options(chat_id, option_id, name) VALUES(?,?,?)",
                (chat_id, idx, n.strip()),
            )
        await self.conn.commit()

    async def get_vote_options(self, chat_id: int) -> List[Dict]:
        cur = await self.conn.execute("SELECT option_id, name FROM vote_options WHERE chat_id=? ORDER BY option_id", (chat_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def cast_vote(self, chat_id: int, user_id: int, option_id: int):
        await self.conn.execute(
            "INSERT OR REPLACE INTO vote_votes(chat_id, user_id, option_id, created_at) VALUES(?,?,?,?)",
            (chat_id, user_id, option_id, now_utc_iso()),
        )
        await self.conn.commit()

    async def vote_results(self, chat_id: int) -> List[Dict]:
        cur = await self.conn.execute(
            """
            SELECT o.option_id, o.name, COUNT(v.user_id) AS votes
            FROM vote_options o
            LEFT JOIN vote_votes v
              ON v.chat_id = o.chat_id AND v.option_id = o.option_id
            WHERE o.chat_id=?
            GROUP BY o.option_id, o.name
            ORDER BY votes DESC, o.option_id ASC
            """,
            (chat_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def user_vote(self, chat_id: int, user_id: int) -> Optional[int]:
        cur = await self.conn.execute("SELECT option_id FROM vote_votes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = await cur.fetchone()
        return int(row["option_id"]) if row else None
