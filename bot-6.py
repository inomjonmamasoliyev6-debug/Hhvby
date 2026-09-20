import asyncio
import html
import sqlite3
import subprocess
import sys
import tempfile
import io
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

try:
    import aiosqlite
except ImportError:  # faqat PostgreSQL ishlatilsa shart emas
    aiosqlite = None
try:
    import asyncpg
except ImportError:  # faqat SQLite ishlatilsa shart emas
    asyncpg = None

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InputFile,
)
try:
    from telegram import CopyTextButton
except ImportError:  # eski kutubxona (21.7 dan past) - nusxalash tugmasi ko'rinmaydi
    CopyTextButton = None
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, TimedOut, NetworkError, Forbidden, RetryAfter, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# ========================================================
#  SOZLAMALAR
# ========================================================

# DIQQAT: tokenni kodda emas, Render > Environment ichida BOT_TOKEN sifatida saqlang!
TOKEN = os.environ.get("BOT_TOKEN", "8856340901:AAHZOhvRkqztuguZ58AzzGg-gzPe_yld8L8")
SUPER_ADMIN = 8057184376
ADMIN_PROFILE_ID = 8057184376

# Doimiy baza: Render Environment ichida DATABASE_URL (PostgreSQL) bo'lsa shuni ishlatadi.
# Bo'lmasa eski usul - SQLite fayl (DB_PATH) ishlaydi.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

SUB_CACHE_TTL = 300
DEFAULT_REF_PRICE = 1000.0
MIN_WITHDRAW_PHONE = 5000.0     # telefon raqamga yechish minimal
MIN_WITHDRAW_CARD = 10000.0     # kartaga yechish minimal
MIN_WITHDRAW = MIN_WITHDRAW_PHONE
NET_RETRIES = 4
NET_BASE_DELAY = 1.5

OTZIF_CHANNEL = os.environ.get("OTZIF_CHANNEL", "@pulishla_otzif")   # to'lovlar (otzif) kanali
SUPPORT_GROUP = os.environ.get("SUPPORT_GROUP", "@pulishlabotchat")  # muammo guruhi
BOT_TAG = os.environ.get("BOT_TAG", "@pulishlabbot")                 # otzifda "Bot:" qatori
OWNER_TAG = os.environ.get("OWNER_TAG", "@pulishlabbot")             # qoidalardagi "Bot egasi"

CAPTCHA_ROUNDS = 2                                   # nechta captcha
ONLY_UZ_PHONE = os.environ.get("ONLY_UZ_PHONE", "1") == "1"   # faqat +998 raqamlar
BACKUP_INTERVAL = 24 * 3600                          # egaga avtomatik zaxira (soniya)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _pip_install(pkg):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg])
        return True
    except Exception as e:
        print(f"❌ {pkg} o'rnatib bo'lmadi: {e}")
        return False


# Kutubxona yo'q bo'lsa - bot o'zi o'rnatib oladi (requirements.txt ni o'zgartirish shart emas)
if DATABASE_URL and asyncpg is None:
    if _pip_install("asyncpg"):
        import asyncpg
if not DATABASE_URL and aiosqlite is None:
    if _pip_install("aiosqlite"):
        import aiosqlite

CANCEL_TEXT = "❌ Bekor qilish"
DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
CARD_TYPES = ["HUMO", "UZCARD", "Boshqa"]
RULES_BUTTON = "📜 Ma'lumot va qoidalar"
BAN_TEXT = f"🚫 Akkauntingiz bloklangan.\n\nMuammo bo'lsa guruhimizga yozing: {SUPPORT_GROUP}"

RULES_TEXT = (
    "📜 Ma'lumot va qoidalar\n"
    f"{DIVIDER}\n\n"
    "✅ Bot to'lovlarni barchasini o'z vaqtida to'laydi.\n\n"
    "⚠️ Lekin siz bilishingiz kerakki, soxta yoki 2 ta akkauntingizdan kirsangiz, "
    "sizga pul miqdori qo'shilmaydi. Agar bu ishni juda ko'p takrorlasangiz, "
    "akkauntingizni bloklashga majbur bo'lamiz. Iltimos, halol ishlang.\n\n"
    "🛠 Botda xatolik bo'lsa yoki biror muammo chiqsa, guruhimizga yozing:\n"
    f"👉 {SUPPORT_GROUP}\n\n"
    "🤝 Hurmat bilan, bot egasi\n"
    f"{OWNER_TAG} 💹"
)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def md_esc(s):
    """Telegram (eski) Markdown uchun maxsus belgilarni himoyalash."""
    s = "" if s is None else str(s)
    for ch in ("\\", "_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def fmt_money(x):
    return f"{x:,.0f}".replace(",", " ")


_bg_tasks = set()


def spawn(coro):
    """Orqa fonda vazifa ishga tushirish (bot qotib qolmasligi uchun)."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


# ========================================================
#  CACHE
# ========================================================

_sub_cache = {}
_settings_cache = {}
_admins_cache = None
_admins_cache_time = 0
ADMIN_CACHE_TTL = 10
SETTINGS_CACHE_TTL = 30

_bot_maintenance = False
_bot_maintenance_msg = "🔧 Botda texnik nosozlik bor. Tez orada tiklanadi, iltimos kuting!"


def cache_get_sub(user_id):
    entry = _sub_cache.get(user_id)
    if not entry:
        return None
    is_sub, expires_at = entry
    if time.monotonic() >= expires_at:
        _sub_cache.pop(user_id, None)
        return None
    return is_sub


def cache_set_sub(user_id, is_sub):
    _sub_cache[user_id] = (is_sub, time.monotonic() + SUB_CACHE_TTL)


def cache_clear_sub(user_id=None):
    if user_id is None:
        _sub_cache.clear()
    else:
        _sub_cache.pop(user_id, None)


def get_settings_cached(key):
    entry = _settings_cache.get(key)
    if not entry:
        return None
    val, expires_at = entry
    if time.monotonic() >= expires_at:
        _settings_cache.pop(key, None)
        return None
    return val


def set_settings_cache(key, value):
    _settings_cache[key] = (value, time.monotonic() + SETTINGS_CACHE_TTL)


def clear_settings_cache(key=None):
    if key is None:
        _settings_cache.clear()
    else:
        _settings_cache.pop(key, None)


# ========================================================
#  API CALL
# ========================================================

async def api_call(coro_factory, *, retries=NET_RETRIES, base_delay=NET_BASE_DELAY,
                    swallow=True, default=None, action_desc=""):
    last_exc = None
    attempt = 0
    flood_waits = 0
    while attempt < retries:
        try:
            return await coro_factory()
        except RetryAfter as e:
            # Telegram "sekinroq" dedi - kutib, qayta urinamiz (xabar yo'qolmaydi)
            flood_waits += 1
            if flood_waits > 6:
                return default
            await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 0.5)
            continue
        except (TimedOut, NetworkError) as e:
            last_exc = e
            attempt += 1
            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning("Tarmoq xatosi%s: %s", f" [{action_desc}]" if action_desc else "", e)
                await asyncio.sleep(delay)
        except Forbidden:
            return default
        except TelegramError as e:
            if "message is not modified" not in str(e).lower():
                logger.error("Telegram xatosi: %s", e)
            if not swallow:
                raise
            return default
    if swallow:
        return default
    if last_exc:
        raise last_exc
    return default

# ========================================================
#  BAZA FUNKSIYALARI  (PostgreSQL yoki SQLite)
# ========================================================

USE_PG = bool(DATABASE_URL)
_pg_pool = None
_sqlite = None
_sqlite_lock = None

TABLES = ["users", "channels", "settings", "admins", "withdrawals", "support_messages",
          "promocodes", "promo_uses", "bonus_claims", "payment_channels"]


class TxAbort(Exception):
    pass


def _pg_dsn():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _q(sql):
    """SQLite '?' belgilarini PostgreSQL '$1, $2...' ga aylantiradi."""
    if not USE_PG:
        return sql
    out, n = [], 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


def _ddl(sql):
    m = {
        "{PK}": "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT",
        "{UID_PK}": "BIGINT PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY",
        "{BIG}": "BIGINT" if USE_PG else "INTEGER",
        "{REAL}": "DOUBLE PRECISION" if USE_PG else "REAL",
        "{TS}": "TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC')" if USE_PG else "TEXT DEFAULT CURRENT_TIMESTAMP",
        "{TSN}": "TIMESTAMP" if USE_PG else "TEXT",
    }
    for k, v in m.items():
        sql = sql.replace(k, v)
    return sql


def _rowcount(status):
    try:
        return int(str(status).split()[-1])
    except Exception:
        return 0


async def _pg_retry(op):
    for attempt in range(3):
        try:
            return await op()
        except (asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError,
                ConnectionError, OSError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.5 * (attempt + 1))


async def db_connect():
    global _pg_pool, _sqlite, _sqlite_lock
    if USE_PG:
        if asyncpg is None:
            raise RuntimeError("asyncpg o'rnatilmagan (requirements.txt ga asyncpg qo'shing)")
        _pg_pool = await asyncpg.create_pool(
            _pg_dsn(), min_size=1, max_size=10,
            max_inactive_connection_lifetime=60, command_timeout=30)
        logger.info("PostgreSQL bazaga ulandi (doimiy baza).")
    else:
        if aiosqlite is None:
            raise RuntimeError("aiosqlite o'rnatilmagan")
        _sqlite = await aiosqlite.connect(DB_PATH)
        _sqlite_lock = asyncio.Lock()
        await _sqlite.execute("PRAGMA journal_mode=WAL;")
        await _sqlite.execute("PRAGMA synchronous=NORMAL;")
        await _sqlite.commit()
        logger.warning("SQLite fayl baza ishlatilmoqda: %s", DB_PATH)


async def db_close():
    try:
        if _pg_pool is not None:
            await _pg_pool.close()
        if _sqlite is not None:
            await _sqlite.close()
    except Exception:
        pass


async def db_exec(sql, params=()):
    """So'rovni bajaradi, o'zgargan qatorlar sonini qaytaradi."""
    sql = _q(sql)
    if USE_PG:
        async def op():
            async with _pg_pool.acquire() as c:
                return await c.execute(sql, *params)
        return _rowcount(await _pg_retry(op))
    async with _sqlite_lock:
        try:
            cur = await _sqlite.execute(sql, tuple(params))
            n = cur.rowcount
            await cur.close()
            await _sqlite.commit()
            return n
        except Exception:
            await _sqlite.rollback()
            raise


async def db_insert(sql, params=()):
    """INSERT bajarib, yangi qator id sini qaytaradi."""
    if USE_PG:
        sql2 = _q(sql) + " RETURNING id"
        async def op():
            async with _pg_pool.acquire() as c:
                return await c.fetchval(sql2, *params)
        return await _pg_retry(op)
    async with _sqlite_lock:
        try:
            cur = await _sqlite.execute(sql, tuple(params))
            rid = cur.lastrowid
            await cur.close()
            await _sqlite.commit()
            return rid
        except Exception:
            await _sqlite.rollback()
            raise


async def db_one(sql, params=()):
    sql = _q(sql)
    if USE_PG:
        async def op():
            async with _pg_pool.acquire() as c:
                return await c.fetchrow(sql, *params)
        r = await _pg_retry(op)
        return tuple(r.values()) if r is not None else None
    async with _sqlite_lock:
        cur = await _sqlite.execute(sql, tuple(params))
        row = await cur.fetchone()
        await cur.close()
        return tuple(row) if row is not None else None


async def db_all(sql, params=()):
    sql = _q(sql)
    if USE_PG:
        async def op():
            async with _pg_pool.acquire() as c:
                return await c.fetch(sql, *params)
        rows = await _pg_retry(op)
        return [tuple(r.values()) for r in rows]
    async with _sqlite_lock:
        cur = await _sqlite.execute(sql, tuple(params))
        rows = await cur.fetchall()
        await cur.close()
        return [tuple(r) for r in rows]


async def db_tx(stmts, first_must_change=False):
    """
    Bir nechta so'rovni BITTA tranzaksiyada bajaradi (yoki hammasi, yoki hech biri).
    stmts: [("exec"|"insert", sql, params), ...]
    Natija: har bir so'rov uchun rowcount (insert bo'lsa id). first_must_change=True bo'lib,
    birinchi so'rov hech narsani o'zgartirmasa - hammasi bekor qilinadi va None qaytadi.
    """
    if USE_PG:
        async def op():
            res = []
            async with _pg_pool.acquire() as c:
                async with c.transaction():
                    for kind, sql, params in stmts:
                        s = _q(sql)
                        if kind == "insert":
                            res.append(await c.fetchval(s + " RETURNING id", *params))
                        else:
                            res.append(_rowcount(await c.execute(s, *params)))
                        if first_must_change and len(res) == 1 and not res[0]:
                            raise TxAbort()
            return res
        try:
            return await _pg_retry(op)
        except TxAbort:
            return None
    async with _sqlite_lock:
        try:
            res = []
            for kind, sql, params in stmts:
                cur = await _sqlite.execute(sql, tuple(params))
                res.append(cur.lastrowid if kind == "insert" else cur.rowcount)
                await cur.close()
                if first_must_change and len(res) == 1 and not res[0]:
                    await _sqlite.rollback()
                    return None
            await _sqlite.commit()
            return res
        except Exception:
            await _sqlite.rollback()
            raise


_USER_EXTRA_COLS = [
    ("banned", "INTEGER DEFAULT 0"),
    ("registered", "INTEGER DEFAULT 1"),      # eski foydalanuvchilar = ro'yxatdan o'tgan
    ("captcha_ok", "INTEGER DEFAULT 1"),
    ("phone", "TEXT"),
    ("referral_paid", "INTEGER DEFAULT 1"),   # eski referallar allaqachon to'langan
]


async def init_db():
    await db_connect()
    ddl = [
        'CREATE TABLE IF NOT EXISTS users (user_id {UID_PK}, balance {REAL} DEFAULT 0.0, referred_by {BIG}, joined_at {TS})',
        'CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, channel_name TEXT)',
        'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)',
        "CREATE TABLE IF NOT EXISTS withdrawals (id {PK}, user_id {BIG}, amount {REAL}, card_type TEXT, card_number TEXT, status TEXT DEFAULT 'pending', created_at {TS})",
        "CREATE TABLE IF NOT EXISTS support_messages (id {PK}, user_id {BIG}, message_text TEXT, admin_id {BIG}, answer_text TEXT, status TEXT DEFAULT 'pending', created_at {TS}, answered_at {TSN})",
        'CREATE TABLE IF NOT EXISTS admins (user_id {UID_PK}, added_at {TS})',
        'CREATE TABLE IF NOT EXISTS promocodes (id {PK}, code TEXT UNIQUE, amount {REAL}, max_uses INTEGER, used_count INTEGER DEFAULT 0, created_at {TS}, is_active INTEGER DEFAULT 1)',
        'CREATE TABLE IF NOT EXISTS promo_uses (id {PK}, user_id {BIG}, promo_id INTEGER, used_at {TS}, UNIQUE(user_id, promo_id))',
        'CREATE TABLE IF NOT EXISTS bonus_claims (id {PK}, user_id {BIG}, amount {REAL}, claimed_at {TS})',
        'CREATE TABLE IF NOT EXISTS payment_channels (id {PK}, channel_id TEXT, channel_name TEXT, description TEXT)',
    ]
    for s in ddl:
        await db_exec(_ddl(s))

    # Yangi ustunlar (eski ma'lumotga tegmaydi)
    if USE_PG:
        for name, typ in _USER_EXTRA_COLS:
            await db_exec(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {typ}")
    else:
        rows = await db_all("PRAGMA table_info(users)")
        existing = {r[1] for r in rows}
        for name, typ in _USER_EXTRA_COLS:
            if name not in existing:
                await db_exec(f"ALTER TABLE users ADD COLUMN {name} {typ}")

    await db_exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL")
    await db_exec("CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)")
    await db_exec("CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id)")

    defaults = [
        ("ref_price", str(DEFAULT_REF_PRICE)),
        ("bonus_min", "10"),
        ("bonus_max", "900"),
        ("bonus_interval", "86400"),
        ("bonus_enabled", "1"),
    ]
    for k, v in defaults:
        await db_exec('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING', (k, v))
    await db_exec('INSERT INTO admins (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING', (SUPER_ADMIN,))


async def db_dump_all():
    """Butun bazani JSON ga saqlash uchun (zaxira nusxa)."""
    out = {}
    for t in TABLES:
        sql = f"SELECT * FROM {t}"
        if USE_PG:
            async def op():
                async with _pg_pool.acquire() as c:
                    st = await c.prepare(sql)
                    cols = [a.name for a in st.get_attributes()]
                    rows = await st.fetch()
                    return cols, [list(r.values()) for r in rows]
            cols, rows = await _pg_retry(op)
        else:
            async with _sqlite_lock:
                cur = await _sqlite.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = [list(r) for r in await cur.fetchall()]
                await cur.close()
        out[t] = {"columns": cols, "rows": rows}
    return out


# ---------- adminlar ----------

async def get_admins():
    global _admins_cache, _admins_cache_time
    now = time.monotonic()
    if _admins_cache is not None and (now - _admins_cache_time) < ADMIN_CACHE_TTL:
        return _admins_cache
    rows = await db_all('SELECT user_id FROM admins')
    _admins_cache = [r[0] for r in rows]
    _admins_cache_time = now
    return _admins_cache


async def add_admin_db(user_id):
    global _admins_cache
    await db_exec('INSERT INTO admins (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING', (user_id,))
    _admins_cache = None


async def remove_admin_db(user_id):
    global _admins_cache
    if user_id == SUPER_ADMIN:
        return False
    await db_exec('DELETE FROM admins WHERE user_id=?', (user_id,))
    _admins_cache = None
    return True


# ---------- sozlamalar ----------

async def get_setting(key, default=None):
    cached = get_settings_cached(key)
    if cached is not None:
        return cached
    row = await db_one('SELECT value FROM settings WHERE key=?', (key,))
    val = row[0] if row else default
    if val is not None:
        set_settings_cache(key, val)
    return val


async def set_setting(key, value):
    clear_settings_cache(key)
    await db_exec('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value',
                  (key, str(value)))


async def get_ref_price():
    val = await get_setting("ref_price", str(DEFAULT_REF_PRICE))
    try:
        return float(val)
    except:
        return DEFAULT_REF_PRICE


# ---------- foydalanuvchilar ----------

async def get_user(user_id):
    return await db_one('SELECT user_id, balance, referred_by FROM users WHERE user_id=?', (user_id,))


async def get_user_full(user_id):
    r = await db_one(
        'SELECT user_id, balance, referred_by, banned, registered, captcha_ok, phone, referral_paid '
        'FROM users WHERE user_id=?', (user_id,))
    if not r:
        return None
    return {
        "user_id": r[0], "balance": float(r[1] or 0.0), "referred_by": r[2],
        "banned": int(r[3] or 0),
        "registered": int(r[4]) if r[4] is not None else 1,
        "captcha_ok": int(r[5]) if r[5] is not None else 1,
        "phone": r[6],
        "referral_paid": int(r[7]) if r[7] is not None else 1,
    }


_status_cache = {}
STATUS_TTL = 20


def invalidate_status(user_id):
    _status_cache.pop(user_id, None)


async def get_status(user_id):
    """(banned, registered) yoki None (foydalanuvchi bazada yo'q). 20 soniya keshlanadi."""
    now = time.monotonic()
    e = _status_cache.get(user_id)
    if e and now < e[1]:
        return e[0]
    row = await db_one('SELECT banned, registered FROM users WHERE user_id=?', (user_id,))
    val = None
    if row:
        val = (int(row[0] or 0), int(row[1]) if row[1] is not None else 1)
    if len(_status_cache) > 20000:
        _status_cache.clear()
    _status_cache[user_id] = (val, now + STATUS_TTL)
    return val


async def is_banned(user_id):
    st = await get_status(user_id)
    return bool(st and st[0])


async def create_user(user_id, referrer_id, registered=0):
    reg = 1 if registered else 0
    n = await db_exec(
        'INSERT INTO users (user_id, referred_by, registered, captcha_ok, referral_paid) '
        'VALUES (?, ?, ?, ?, ?) ON CONFLICT (user_id) DO NOTHING',
        (user_id, referrer_id, reg, reg, 1 if reg else 0))
    invalidate_status(user_id)
    return n


async def add_balance(user_id, amount):
    fn = "GREATEST" if USE_PG else "MAX"
    await db_exec(f'UPDATE users SET balance = {fn}(0, balance + ?) WHERE user_id=?', (float(amount), user_id))


async def get_user_by_id(user_id):
    return await db_one('SELECT user_id, balance FROM users WHERE user_id=?', (user_id,))


async def set_banned(user_id, flag):
    n = await db_exec('UPDATE users SET banned=? WHERE user_id=?', (1 if flag else 0, user_id))
    invalidate_status(user_id)
    return n


async def set_captcha_ok(user_id):
    await db_exec('UPDATE users SET captcha_ok=1 WHERE user_id=?', (user_id,))


async def set_registered(user_id):
    await db_exec('UPDATE users SET registered=1, captcha_ok=1 WHERE user_id=?', (user_id,))
    invalidate_status(user_id)


async def set_phone(user_id, phone):
    """'ok' - saqlandi, 'dup' - bu raqam boshqa akkauntda bor."""
    try:
        n = await db_exec(
            'UPDATE users SET phone=? WHERE user_id=? AND NOT EXISTS '
            '(SELECT 1 FROM users u2 WHERE u2.phone=?)', (phone, user_id, phone))
    except Exception:
        return "dup"
    return "ok" if n else "dup"


async def claim_referral_paid(user_id):
    """Referal puli faqat BIR marta berilishi uchun (True = hozir birinchi marta)."""
    n = await db_exec('UPDATE users SET referral_paid=1 WHERE user_id=? AND referral_paid=0', (user_id,))
    return bool(n)


async def count_paid_referrals(user_id):
    r = await db_one('SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_paid=1 AND registered=1', (user_id,))
    return int(r[0]) if r else 0


# ---------- kanallar ----------

async def get_channels():
    return await db_all('SELECT channel_id, channel_name FROM channels')


async def add_channel_db(channel_id, channel_name):
    await db_exec('INSERT INTO channels (channel_id, channel_name) VALUES (?, ?) '
                  'ON CONFLICT (channel_id) DO UPDATE SET channel_name=excluded.channel_name',
                  (channel_id, channel_name))


async def remove_channel_db(channel_id):
    await db_exec('DELETE FROM channels WHERE channel_id=?', (channel_id,))


async def get_stats():
    r = await db_one('SELECT COUNT(*), COALESCE(SUM(balance), 0.0) FROM users')
    return (int(r[0]), float(r[1]))


async def get_all_user_ids():
    rows = await db_all('SELECT user_id FROM users')
    return [r[0] for r in rows]


# ---------- pul yechish ----------

async def create_withdrawal(user_id, amount, card_type, card_number):
    return await db_insert(
        "INSERT INTO withdrawals (user_id, amount, card_type, card_number, status) VALUES (?, ?, ?, ?, 'pending')",
        (user_id, float(amount), card_type, card_number))


async def create_withdrawal_atomic(user_id, amount, card_type, card_number):
    """Balansdan yechish + so'rov yaratish BITTA tranzaksiyada. Balans yetmasa None."""
    res = await db_tx([
        ("exec", 'UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?',
         (float(amount), user_id, float(amount))),
        ("insert", "INSERT INTO withdrawals (user_id, amount, card_type, card_number, status) VALUES (?, ?, ?, ?, 'pending')",
         (user_id, float(amount), card_type, card_number)),
    ], first_must_change=True)
    return res[1] if res else None


async def get_withdrawal(wid):
    return await db_one('SELECT id, user_id, amount, card_type, card_number, status FROM withdrawals WHERE id=?', (wid,))


async def set_withdrawal_status(wid, status):
    await db_exec('UPDATE withdrawals SET status=? WHERE id=?', (status, wid))


async def claim_withdrawal_status(wid, new_status):
    """Faqat 'pending' bo'lsa o'zgartiradi (ikki marta tasdiqlashdan himoya)."""
    n = await db_exec("UPDATE withdrawals SET status=? WHERE id=? AND status='pending'", (new_status, wid))
    return bool(n)


# ---------- murojaat ----------

async def create_support_message(user_id, message_text):
    return await db_insert("INSERT INTO support_messages (user_id, message_text, status) VALUES (?, ?, 'pending')",
                           (user_id, message_text))


async def get_support_message(sid):
    return await db_one('SELECT id, user_id, message_text, status FROM support_messages WHERE id=?', (sid,))


async def set_support_status(sid, status, answer_text=None, admin_id=None):
    if answer_text and admin_id:
        await db_exec('UPDATE support_messages SET status=?, answer_text=?, admin_id=?, answered_at=CURRENT_TIMESTAMP WHERE id=?',
                      (status, answer_text, admin_id, sid))
    else:
        await db_exec('UPDATE support_messages SET status=? WHERE id=?', (status, sid))


# ---------- promokod ----------

async def create_promocode(code, amount, max_uses):
    try:
        return await db_insert('INSERT INTO promocodes (code, amount, max_uses) VALUES (?, ?, ?)',
                               (code.upper(), float(amount), int(max_uses)))
    except Exception:
        return None


async def get_promocode(code):
    return await db_one('SELECT id, code, amount, max_uses, used_count, is_active FROM promocodes WHERE code=?', (code.upper(),))


async def get_all_promocodes():
    return await db_all('SELECT id, code, amount, max_uses, used_count, is_active FROM promocodes ORDER BY id DESC')


async def use_promocode(user_id, promo_id):
    try:
        n = await db_exec('UPDATE promocodes SET used_count = used_count + 1 '
                          'WHERE id=? AND used_count < max_uses AND is_active=1', (promo_id,))
        if not n:
            return False
        try:
            await db_exec('INSERT INTO promo_uses (user_id, promo_id) VALUES (?, ?)', (user_id, promo_id))
        except Exception:
            await db_exec('UPDATE promocodes SET used_count = used_count - 1 WHERE id=?', (promo_id,))
            return False
        return True
    except Exception:
        return False


async def has_used_promo(user_id, promo_id):
    return await db_one('SELECT id FROM promo_uses WHERE user_id=? AND promo_id=?', (user_id, promo_id)) is not None


async def delete_promocode(promo_id):
    await db_exec('UPDATE promocodes SET is_active=0 WHERE id=?', (promo_id,))


# ---------- bonus ----------

async def last_bonus_claim(user_id):
    row = await db_one('SELECT claimed_at FROM bonus_claims WHERE user_id=? ORDER BY id DESC LIMIT 1', (user_id,))
    return row[0] if row else None


async def add_bonus_claim(user_id, amount):
    await db_tx([
        ("exec", 'INSERT INTO bonus_claims (user_id, amount) VALUES (?, ?)', (user_id, float(amount))),
        ("exec", 'UPDATE users SET balance = balance + ? WHERE user_id=?', (float(amount), user_id)),
    ])


# ---------- to'lov kanali ----------

async def get_payment_channel():
    return await db_one('SELECT channel_id, channel_name, description FROM payment_channels ORDER BY id DESC LIMIT 1')


async def set_payment_channel(channel_id, channel_name, description):
    await db_tx([
        ("exec", 'DELETE FROM payment_channels', ()),
        ("exec", 'INSERT INTO payment_channels (channel_id, channel_name, description) VALUES (?, ?, ?)',
         (channel_id, channel_name, description)),
    ])

# ========================================================
#  OBUNANI TEKSHIRISH VA KLAVIATURALAR
# ========================================================

async def check_one_channel(context, channel_id, user_id):
    result = await api_call(
        lambda: context.bot.get_chat_member(chat_id=channel_id, user_id=user_id),
        action_desc=f"member:{channel_id}", swallow=True, default=None)
    if result is None:
        return True
    return result.status not in ('left', 'kicked')


async def is_subscribed(user_id, context, use_cache=True):
    admins = await get_admins()
    if user_id in admins:
        return True
    if use_cache:
        cached = cache_get_sub(user_id)
        if cached is not None:
            return cached
    channels = await get_channels()
    if not channels:
        result = True
    else:
        results = await asyncio.gather(*[check_one_channel(context, cid, user_id) for cid, _ in channels])
        result = all(results)
    cache_set_sub(user_id, result)
    return result


def main_keyboard(user_id):
    buttons = [
        [KeyboardButton("💰 Pul ishlash"), KeyboardButton("👤 Balans")],
        [KeyboardButton("💸 Pul yechish"), KeyboardButton("🎁 Bonus")],
        [KeyboardButton("🎟 Promokod"), KeyboardButton("💳 To'lov kanali")],
        [KeyboardButton("☎️ Murojaat"), KeyboardButton(RULES_BUTTON)],
    ]
    if user_id == SUPER_ADMIN:
        buttons.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_TEXT)]], resize_keyboard=True)


def subscription_keyboard(channels):
    btns = [[InlineKeyboardButton(f"📢 {name}", url=f"https://t.me/{cid.replace('@', '')}")] for cid, name in channels]
    btns.append([InlineKeyboardButton("✅ Obunani tasdiqlash", callback_data="check_sub")])
    return InlineKeyboardMarkup(btns)


def admin_panel_keyboard(user_id=None):
    rows = [
        [InlineKeyboardButton("➕ Kanal qo'shish", callback_data="admin_add_channel"),
         InlineKeyboardButton("➖ Kanal o'chirish", callback_data="admin_remove_channel")],
        [InlineKeyboardButton("📋 Kanallar ro'yxati", callback_data="admin_list_channels")],
        [InlineKeyboardButton("💵 Referal narxi", callback_data="admin_set_price")],
        [InlineKeyboardButton("👑 Admin qo'shish", callback_data="admin_add_admin"),
         InlineKeyboardButton("🚫 Admin o'chirish", callback_data="admin_remove_admin")],
        [InlineKeyboardButton("💰 Pul qo'shish", callback_data="admin_add_money"),
         InlineKeyboardButton("💸 Pul ayirish", callback_data="admin_remove_money")],
        [InlineKeyboardButton("🛠 Texnik ish rejimi", callback_data="admin_maintenance")],
        [InlineKeyboardButton("🎁 Bonus sozlamalari", callback_data="admin_bonus_settings")],
        [InlineKeyboardButton("🎟 Promokod yaratish", callback_data="admin_create_promo"),
         InlineKeyboardButton("📋 Promokodlar", callback_data="admin_list_promos")],
        [InlineKeyboardButton("💳 To'lov kanali", callback_data="admin_payment_channel")],
        [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats"),
         InlineKeyboardButton("📢 Xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🚪 Egallikdan chiqish", callback_data="admin_leave_ownership")],
        [InlineKeyboardButton("✖️ Yopish", callback_data="admin_close")],
    ]
    if user_id == SUPER_ADMIN:   # bloklash tugmalari FAQAT egada ko'rinadi
        rows.insert(len(rows) - 2, [
            InlineKeyboardButton("🚫 Foydalanuvchini bloklash", callback_data="admin_ban_user"),
            InlineKeyboardButton("✅ Blokdan chiqarish", callback_data="admin_unban_user"),
        ])
    return InlineKeyboardMarkup(rows)


def remove_channel_keyboard(channels):
    rows = [[InlineKeyboardButton(f"🗑 {name} ({cid})", callback_data=f"rmch:{cid}")] for cid, name in channels]
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def admin_list_keyboard(admins):
    rows = [[InlineKeyboardButton(f"🗑 Admin {aid}", callback_data=f"rmadm:{aid}")] for aid in admins if aid != SUPER_ADMIN]
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def promo_list_keyboard(promos):
    rows = []
    for pid, code, amount, max_uses, used, active in promos[:20]:
        status = "✅" if active else "❌"
        rows.append([InlineKeyboardButton(f"{status} {code} | {amount:,.0f} | {used}/{max_uses}", callback_data=f"delpromo:{pid}")])
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")]])


def share_keyboard(ref_link):
    share_text = "🎁 Bu bot orqali pul ishlashni boshla!"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Do'stlarga ulashish", url=share_url)],
        [InlineKeyboardButton("🔄 Yangilash", callback_data="refresh_ref")],
    ])


def card_type_keyboard():
    btns = [[InlineKeyboardButton(f"💳 {c}", callback_data=f"wd_type:{c}")] for c in CARD_TYPES]
    btns.append([InlineKeyboardButton("‹ Bekor qilish", callback_data="wd_cancel")])
    return InlineKeyboardMarkup(btns)


def withdraw_submitted_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 To'lovlar kanali", url=f"https://t.me/{OTZIF_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("👨‍💼 Admin", url=f"tg://user?id={ADMIN_PROFILE_ID}")],
    ])


def admin_withdraw_keyboard(wid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"wdok:{wid}"),
         InlineKeyboardButton("❌ Rad etish", callback_data=f"wdno:{wid}")]
    ])


def support_answer_keyboard(sid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✍️ Javob yozish", callback_data=f"sup_answer:{sid}")]])


def confirm_leave_1():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Ha, egallikdan chiqaman", callback_data="leave_yes_1")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="leave_cancel")],
    ])


def confirm_leave_2():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 100% ha", callback_data="leave_yes_2")],
        [InlineKeyboardButton("❌ Yo'q", callback_data="leave_cancel")],
    ])


def confirm_leave_3():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Ha, egallikdan chiq", callback_data="leave_yes_3")],
    ])


async def animate(message, frames, delay=0.35, parse_mode="Markdown", reply_markup=None):
    last = len(frames) - 1
    for i, frame in enumerate(frames):
        await api_call(
            lambda f=frame, i=i: message.edit_text(f, parse_mode=parse_mode, reply_markup=reply_markup if i == last else None),
            action_desc="animate")
        if i != last:
            await asyncio.sleep(delay)


async def show_subscription_gate(update, context):
    channels = await get_channels()
    if not channels:
        return
    await api_call(lambda: update.effective_chat.send_message(
        "🔐 *Kirish cheklangan*", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()), action_desc="g1")
    await api_call(lambda: update.effective_chat.send_message(
        f"⚡️ *Kanal(lar)ga a'zo bo'ling:*\n{DIVIDER}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=subscription_keyboard(channels)), action_desc="g2")
# ========================================================
#  START, PUL ISHLASH, BALANS, BONUS, PROMO, MUROJAAT
# ========================================================

async def get_bot_username(context):
    name = context.application.bot_data.get("username")
    if name:
        return name
    me = await api_call(lambda: context.bot.get_me(), action_desc="me", swallow=False)
    if me:
        context.application.bot_data["username"] = me.username
        return me.username
    return None


def invite_keyboard(ref_link):
    share_text = "🎁 Bu bot orqali pul ishlashni boshla!"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"
    rows = [[InlineKeyboardButton("📤 Yana taklif qilish", url=share_url)]]
    if CopyTextButton is not None:
        try:
            rows.append([InlineKeyboardButton("📋 Havolani nusxalash", copy_text=CopyTextButton(text=ref_link))])
        except Exception:
            pass
    return InlineKeyboardMarkup(rows)


# ---------- ro'yxatdan o'tish: captcha -> kanallar -> telefon ----------

def build_captcha():
    if random.random() < 0.5:
        a, b = random.randint(2, 9), random.randint(2, 9)
        op = random.choice(["+", "-", "×"])
        if op == "-" and a < b:
            a, b = b, a
        ans = a + b if op == "+" else (a - b if op == "-" else a * b)
        opts = {str(ans)}
        while len(opts) < 4:
            opts.add(str(max(0, ans + random.choice([-6, -4, -3, -2, -1, 1, 2, 3, 4, 6]))))
        text = f"🧮 Hisoblang: {a} {op} {b} = ?"
        answer = str(ans)
    else:
        emojis = ["🍎", "🍌", "🍇", "🍒", "🍉", "🍓", "🥝", "🍋", "🍑", "🍍"]
        target = random.choice(emojis)
        others = random.sample([e for e in emojis if e != target], 3)
        opts = set(others + [target])
        text = f"🧩 Quyidagilar ichidan {target} ni toping:"
        answer = target
    opts = list(opts)
    random.shuffle(opts)
    return text, answer, opts


def captcha_markup(opts):
    btns = [InlineKeyboardButton(o, callback_data=f"cap:{o}") for o in opts]
    return InlineKeyboardMarkup([btns[:2], btns[2:]])


async def send_captcha(context, chat_id, rnd):
    text, answer, opts = build_captcha()
    context.user_data["captcha"] = {"answer": answer, "round": rnd}
    await api_call(lambda: context.bot.send_message(
        chat_id=chat_id,
        text=f"🤖 *Bot tekshiruvi* ({rnd + 1}/{CAPTCHA_ROUNDS})\n{DIVIDER}\n{text}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=captcha_markup(opts)), action_desc="captcha")


async def send_gate(context, chat_id):
    channels = await get_channels()
    if not channels:
        return
    await api_call(lambda: context.bot.send_message(
        chat_id=chat_id, text="🔐 *Kirish cheklangan*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove()), action_desc="g1")
    await api_call(lambda: context.bot.send_message(
        chat_id=chat_id, text=f"⚡️ *Kanal(lar)ga a'zo bo'ling:*\n{DIVIDER}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=subscription_keyboard(channels)), action_desc="g2")


async def ask_phone(context, chat_id):
    kb = ReplyKeyboardMarkup([[KeyboardButton("📱 Raqamni yuborish", request_contact=True)]],
                             resize_keyboard=True, one_time_keyboard=True)
    await api_call(lambda: context.bot.send_message(
        chat_id=chat_id,
        text=("📱 *Oxirgi qadam!*\n\nTelefon raqamingizni tasdiqlang — pastdagi "
              "«📱 Raqamni yuborish» tugmasini bosing."),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb), action_desc="ask_phone")


async def registration_step(context, user_id, chat_id):
    """Ro'yxatdan o'tmagan foydalanuvchini keyingi bosqichga o'tkazadi."""
    u = await get_user_full(user_id)
    if not u:
        return
    if u["registered"]:
        await api_call(lambda: context.bot.send_message(
            chat_id=chat_id, text="👇 Menyudan tanlang:", reply_markup=main_keyboard(user_id)), action_desc="reg_menu")
        return
    if not u["captcha_ok"]:
        await send_captcha(context, chat_id, 0)
        return
    if not await is_subscribed(user_id, context):
        await send_gate(context, chat_id)
        return
    if not u["phone"]:
        await ask_phone(context, chat_id)
        return
    await complete_registration(context, user_id, chat_id)


async def complete_registration(context, user_id, chat_id, tg_user=None):
    await set_registered(user_id)
    cache_clear_sub(user_id)
    context.user_data.pop("captcha", None)
    await api_call(lambda: context.bot.send_message(
        chat_id=chat_id,
        text=(f"✅ *Ro'yxatdan o'tish yakunlandi!*\n\n🎊 Xush kelibsiz!\n"
              f"🤖 Do'stlaringizni taklif qilib pul ishlang!\n"
              f"💰 Bonus, promokod, to'lov kanali\n💸 Pulni yechib oling\n{DIVIDER}"),
        parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="reg_done")

    # Referal puli - faqat to'liq ro'yxatdan o'tgandan keyin va faqat BIR marta
    u = await get_user_full(user_id)
    ref = u["referred_by"] if u else None
    if not ref or ref == user_id:
        return
    if not await claim_referral_paid(user_id):
        return
    st = await get_status(ref)
    if not st or st[0]:          # referrer yo'q yoki bloklangan
        return
    price = await get_ref_price()
    await add_balance(ref, price)
    cache_clear_sub(ref)
    try:
        if tg_user is None:
            tg_user = await api_call(lambda: context.bot.get_chat(user_id), action_desc="ref_chat")
        name = html.escape(getattr(tg_user, "full_name", None) or "Do'st")
        uname = f"@{html.escape(tg_user.username)}" if getattr(tg_user, "username", None) else "yo'q"
        ru = await get_user(ref)
        balance = ru[1] if ru else 0.0
        total = await count_paid_referrals(ref)
        need = MIN_WITHDRAW_PHONE - balance
        if need > 0:
            tail = (f"🎯 Yechish uchun yana <b>{fmt_money(need)} so'm</b> kerak "
                    f"(min: {fmt_money(MIN_WITHDRAW_PHONE)} so'm)")
        else:
            tail = "✅ Endi pul yechib olishingiz mumkin!"
        text = (
            "🎉 <b>Yangi do'st qo'shildi!</b>\n\n"
            f"<blockquote>👤 {name}\n🔗 Username: {uname}\n🆔 ID: {user_id}</blockquote>\n\n"
            f"💰 <b>+{fmt_money(price)} so'm</b> balansingizga tushdi!\n\n"
            f"👥 Jami takliflar: <b>{total}</b>\n"
            f"💳 Joriy balans: <b>{fmt_money(balance)} so'm</b>\n\n{tail}"
        )
        bot_username = await get_bot_username(context)
        kb = invite_keyboard(f"https://t.me/{bot_username}?start={ref}") if bot_username else None
        await api_call(lambda: context.bot.send_message(
            chat_id=ref, text=text, parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="ref_notify")
    except Exception:
        logger.exception("referal xabari xato")


async def captcha_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        if await is_banned(user_id):
            await api_call(lambda: query.answer("🚫", show_alert=True), action_desc="cap_ban")
            return
        u = await get_user_full(user_id)
        if not u or u["registered"] or u["captcha_ok"]:
            await api_call(lambda: query.answer(), action_desc="cap_skip")
            return
        cap = context.user_data.get("captcha")
        val = query.data.split(":", 1)[1]
        if not cap:
            await api_call(lambda: query.answer("🔄 Qayta urinib ko'ring"), action_desc="cap_new")
            await send_captcha(context, query.message.chat.id, 0)
            return
        if val == cap["answer"]:
            nxt = cap["round"] + 1
            await api_call(lambda: query.answer("✅"), action_desc="cap_ok")
            if nxt >= CAPTCHA_ROUNDS:
                await set_captcha_ok(user_id)
                context.user_data.pop("captcha", None)
                await api_call(lambda: query.message.delete(), action_desc="cap_del")
                await registration_step(context, user_id, query.message.chat.id)
            else:
                text, answer, opts = build_captcha()
                context.user_data["captcha"] = {"answer": answer, "round": nxt}
                await api_call(lambda: query.message.edit_text(
                    f"🤖 *Bot tekshiruvi* ({nxt + 1}/{CAPTCHA_ROUNDS})\n{DIVIDER}\n{text}",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=captcha_markup(opts)), action_desc="cap_next")
        else:
            await api_call(lambda: query.answer("❌ Noto'g'ri, qayta urinib ko'ring", show_alert=True), action_desc="cap_bad")
            text, answer, opts = build_captcha()
            context.user_data["captcha"] = {"answer": answer, "round": cap["round"]}
            await api_call(lambda: query.message.edit_text(
                f"🤖 *Bot tekshiruvi* ({cap['round'] + 1}/{CAPTCHA_ROUNDS})\n{DIVIDER}\n{text}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=captcha_markup(opts)), action_desc="cap_retry")
    except Exception:
        logger.exception("captcha xato")


async def contact_handler(update, context):
    try:
        msg = update.message
        if not msg or not msg.contact:
            return
        user_id = msg.from_user.id
        chat_id = msg.chat_id
        if await is_banned(user_id):
            await api_call(lambda: msg.reply_text(BAN_TEXT), action_desc="c_ban")
            return
        u = await get_user_full(user_id)
        if not u or u["registered"]:
            await api_call(lambda: msg.reply_text("👇 Menyudan tanlang:", reply_markup=main_keyboard(user_id)), action_desc="c_menu")
            return
        # avval captcha va obuna bajarilgan bo'lishi kerak
        if not u["captcha_ok"] or not await is_subscribed(user_id, context):
            await registration_step(context, user_id, chat_id)
            return
        contact = msg.contact
        if contact.user_id != user_id:
            await api_call(lambda: msg.reply_text("❌ Faqat o'zingizning raqamingizni yuboring (pastdagi tugma orqali)."), action_desc="c_own")
            await ask_phone(context, chat_id)
            return
        digits = "".join(ch for ch in (contact.phone_number or "") if ch.isdigit())
        if ONLY_UZ_PHONE and not (digits.startswith("998") and len(digits) == 12):
            await api_call(lambda: msg.reply_text("❌ Faqat O'zbekiston (+998) raqamlari qabul qilinadi."), action_desc="c_uz")
            await ask_phone(context, chat_id)
            return
        if not u["phone"]:
            res = await set_phone(user_id, "+" + digits)
            if res == "dup":
                await api_call(lambda: msg.reply_text(
                    "❌ Bu raqam boshqa akkauntda ro'yxatdan o'tgan.\n\n"
                    f"Muammo bo'lsa guruhimizga yozing: {SUPPORT_GROUP}", reply_markup=ReplyKeyboardRemove()), action_desc="c_dup")
                return
        await complete_registration(context, user_id, chat_id, tg_user=update.effective_user)
    except Exception:
        logger.exception("contact xato")


async def start(update, context):
    try:
        if not update.message:
            return
        user_id = update.effective_user.id

        if _bot_maintenance and user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text(_bot_maintenance_msg, parse_mode=ParseMode.MARKDOWN), action_desc="maint")
            return

        if user_id != SUPER_ADMIN and await is_banned(user_id):
            await api_call(lambda: update.message.reply_text(BAN_TEXT), action_desc="banned")
            return

        if context.user_data.get("left_ownership"):
            user = await get_user(user_id)
            if not user:
                await create_user(user_id, None, registered=1)
            await api_call(lambda: update.message.reply_text(
                f"👋 Xush kelibsiz, *{md_esc(update.effective_user.first_name)}*!\n\n🚪 Siz egallikdan chiqdingiz.\n{DIVIDER}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="left")
            return

        referrer_id = None
        if context.args and context.args[0].isdigit():
            referrer_id = int(context.args[0])

        user = await get_user_full(user_id)
        if not user:
            is_adm = user_id in await get_admins()
            ref_ok = None
            if referrer_id and referrer_id != user_id and not is_adm:
                if await get_user(referrer_id):
                    ref_ok = referrer_id
            await create_user(user_id, ref_ok, registered=1 if is_adm else 0)
            user = await get_user_full(user_id)

        # Ro'yxatdan o'tish tugamagan bo'lsa - keyingi bosqich
        if user and not user["registered"]:
            await registration_step(context, user_id, update.effective_chat.id)
            return

        if not await is_subscribed(user_id, context):
            await show_subscription_gate(update, context)
            return

        await api_call(lambda: context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING), action_desc="typing")

        await api_call(lambda: update.message.reply_text(
            f"👋 Qaytib keldingiz, *{md_esc(update.effective_user.first_name)}*!\n\n"
            f"🤖 Do'stlaringizni taklif qilib pul ishlang!\n"
            f"💰 Bonus, promokod, to'lov kanali\n"
            f"💸 Pulni kartangizga yechib oling\n{DIVIDER}",
            reply_markup=main_keyboard(user_id), parse_mode=ParseMode.MARKDOWN), action_desc="start_r")
    except Exception:
        logger.exception("start xato")


async def check_sub_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        if user_id != SUPER_ADMIN and await is_banned(user_id):
            await api_call(lambda: query.answer("🚫 Bloklangansiz", show_alert=True), action_desc="ck_ban")
            return
        cache_clear_sub(user_id)
        await api_call(lambda: query.answer("🔄"), action_desc="ck")
        ok = await is_subscribed(user_id, context, use_cache=False)
        if ok:
            await animate(query.message, ["🔄...", "🔄..", "✅ *Obuna tasdiqlandi!*"], delay=0.35)
            await asyncio.sleep(0.4)
            await api_call(lambda: query.message.delete(), action_desc="del")
            user = await get_user_full(user_id)
            if not user:
                is_adm = user_id in await get_admins()
                await create_user(user_id, None, registered=1 if is_adm else 0)
                user = await get_user_full(user_id)
            if user and not user["registered"]:
                await registration_step(context, user_id, query.message.chat.id)
                return
            await api_call(lambda: context.bot.send_message(
                chat_id=query.message.chat.id,
                text=f"✅ Obuna tasdiqlandi!\n{DIVIDER}\n👇 Menyudan tanlang:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="main")
        else:
            await api_call(lambda: query.answer("❌ Hali obuna bo'lmadingiz!", show_alert=True), action_desc="ck_fail")
    except Exception:
        logger.exception("check_sub xato")


async def handle_earn(update, context, user_id):
    placeholder = await api_call(lambda: update.message.reply_text("⏳"), action_desc="earn_ph")
    if not placeholder:
        return
    ref_price = await get_ref_price()
    bot_username = await get_bot_username(context)
    if not bot_username:
        return
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    await animate(placeholder, ["⏳", "⏳."], delay=0.35)
    text = (
        "🚀 *Sizning taklif havolangiz:*\n"
        f"`{ref_link}`\n\n"
        f"💵 Har bir do'st: *{ref_price:,.0f} so'm*\n{DIVIDER}\n📤 Ulashing!"
    )
    await api_call(lambda: placeholder.edit_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=share_keyboard(ref_link)), action_desc="earn_e")


async def refresh_ref_callback(update, context):
    try:
        query = update.callback_query
        await api_call(lambda: query.answer("🔄"), action_desc="ref_a")
        user_id = query.from_user.id
        ref_price = await get_ref_price()
        bot_username = await get_bot_username(context)
        if not bot_username:
            return
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        text = f"🚀 *Taklif havolangiz:*\n`{ref_link}`\n\n💵 *{ref_price:,.0f} so'm*"
        await api_call(lambda: query.message.edit_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=share_keyboard(ref_link)), action_desc="ref_e")
    except Exception:
        logger.exception("refresh xato")


async def handle_rules(update):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Guruhga yozish", url=f"https://t.me/{SUPPORT_GROUP.lstrip('@')}")],
        [InlineKeyboardButton("📢 To'lovlar kanali", url=f"https://t.me/{OTZIF_CHANNEL.lstrip('@')}")],
    ])
    await api_call(lambda: update.message.reply_text(RULES_TEXT, reply_markup=kb), action_desc="rules")


async def handle_balance(update, user_id):
    cache_clear_sub(user_id)
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    await api_call(lambda: update.message.reply_text(
        f"💳 *Balans:* *{balance:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="bal")


async def handle_stats(update):
    count, total = await get_stats()
    await api_call(lambda: update.message.reply_text(
        f"📊 *Statistika*\n{DIVIDER}\n👤 *{count}* ta foydalanuvchi\n💰 *{total:,.0f} so'm*",
        parse_mode=ParseMode.MARKDOWN), action_desc="stats")


async def handle_bonus(update, context, user_id):
    enabled = await get_setting("bonus_enabled", "1")
    if enabled != "1":
        await api_call(lambda: update.message.reply_text("❌ Bonus o'chirilgan.", parse_mode=ParseMode.MARKDOWN), action_desc="b_off")
        return
    last = await last_bonus_claim(user_id)
    interval = int(await get_setting("bonus_interval", "86400"))
    if last:
        try:
            last_dt = last if isinstance(last, datetime) else datetime.fromisoformat(str(last).replace('T', ' ')[:26])
            next_claim = last_dt + timedelta(seconds=interval)
            if _utcnow() < next_claim:
                remaining = next_claim - _utcnow()
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                await api_call(lambda: update.message.reply_text(
                    f"⏰ Keyingi bonus: *{hours}s {minutes}m* dan so'ng", parse_mode=ParseMode.MARKDOWN), action_desc="b_wait")
                return
        except:
            pass
    bmin = int(await get_setting("bonus_min", "10"))
    bmax = int(await get_setting("bonus_max", "900"))
    amount = random.randint(bmin, bmax)
    await add_bonus_claim(user_id, amount)
    cache_clear_sub(user_id)
    await api_call(lambda: update.message.reply_text(
        f"🎁 *Tabriklaymiz!*\n💰 *+{amount} so'm* bonus!", parse_mode=ParseMode.MARKDOWN), action_desc="b_done")


async def handle_promo_start(update, context, user_id):
    context.user_data["state"] = "enter_promo"
    await api_call(lambda: update.message.reply_text(
        "🎟 *Promokodni kiriting:*", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="pr_p")


async def handle_promo_state(update, context, user_id, text):
    if context.user_data.get("state") != "enter_promo":
        return False
    context.user_data["state"] = None
    if text == CANCEL_TEXT:
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="pr_c")
        return True
    promo = await get_promocode(text.strip())
    if not promo or not promo[5]:
        await api_call(lambda: update.message.reply_text("❌ Topilmadi.", reply_markup=main_keyboard(user_id)), action_desc="pr_nf")
        return True
    pid, code, amount, max_uses, used_count, is_active = promo
    if used_count >= max_uses:
        await api_call(lambda: update.message.reply_text("❌ Limit tugagan.", reply_markup=main_keyboard(user_id)), action_desc="pr_lim")
        return True
    if await has_used_promo(user_id, pid):
        await api_call(lambda: update.message.reply_text("❌ Ishlatgansiz.", reply_markup=main_keyboard(user_id)), action_desc="pr_us")
        return True
    if await use_promocode(user_id, pid):
        await add_balance(user_id, amount)
        cache_clear_sub(user_id)
        await api_call(lambda: update.message.reply_text(
            f"🎉 *+{amount:,.0f} so'm!*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="pr_ok")
    return True


async def handle_payment_channel(update, context, user_id):
    pc = await get_payment_channel()
    if not pc:
        await api_call(lambda: update.message.reply_text("💳 Kanal yo'q.", parse_mode=ParseMode.MARKDOWN), action_desc="pc_e")
        return
    cid, name, desc = pc
    await api_call(lambda: update.message.reply_text(
        f"💳 *{name}*\n{DIVIDER}\n{desc}\n\n👉 {cid}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 O'tish", url=f"https://t.me/{cid.replace('@', '')}")]])), action_desc="pc_s")


async def handle_support_start(update, context, user_id):
    context.user_data["state"] = "support_message"
    await api_call(lambda: update.message.reply_text(
        f"☎️ *Murojaat*\n{DIVIDER}\nXabaringizni yozing:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="sup_p")


async def handle_support_state(update, context, user_id, text):
    if context.user_data.get("state") != "support_message":
        return False
    if text == CANCEL_TEXT:
        context.user_data["state"] = None
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="sup_c")
        return True
    context.user_data["state"] = None
    sid = await create_support_message(user_id, text)
    user_info = update.effective_user
    uname = f"@{user_info.username}" if user_info.username else "yo'q"
    admins = await get_admins()
    for aid in admins:
        await api_call(lambda a=aid: context.bot.send_message(
            chat_id=a,
            text=f"☎️ *Murojaat!*\n{DIVIDER}\n👤 {md_esc(user_info.full_name)} {md_esc(uname)}\n🆔 `{user_info.id}`\n{DIVIDER}\n💬 {md_esc(text)}\n\n📋 ID: `{sid}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=support_answer_keyboard(sid)), action_desc=f"sup_a:{aid}")
    await api_call(lambda: update.message.reply_text(
        "✅ *Yuborildi!*\nAdmin tez orada javob beradi.", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="sup_ok")
    return True


async def support_answer_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id not in await get_admins():
        await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="sa_d")
        return
    sid = int(query.data.split(":")[1])
    context.user_data["state"] = "support_reply"
    context.user_data["support_reply_sid"] = sid
    await api_call(lambda: query.message.edit_text("✍️ *Javobingizni yozing:*", parse_mode=ParseMode.MARKDOWN), action_desc="sa_p")
    await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="sa_kb")


async def handle_support_reply_state(update, context, user_id, text):
    if context.user_data.get("state") != "support_reply":
        return False
    sid = context.user_data.get("support_reply_sid")
    context.user_data["state"] = None
    context.user_data.pop("support_reply_sid", None)
    if text == CANCEL_TEXT:
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="sr_c")
        return True
    if not sid:
        return True
    msg = await get_support_message(sid)
    if not msg:
        return True
    target = msg[1]
    await set_support_status(sid, "answered", text, user_id)
    await api_call(lambda: context.bot.send_message(
        chat_id=target,
        text=f"💬 *Admin javobi:*\n{DIVIDER}\n{text}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(target)), action_desc="sr_s")
    await api_call(lambda: update.message.reply_text("✅ Yuborildi!", reply_markup=main_keyboard(user_id)), action_desc="sr_ok")
    return True
# ========================================================
#  PUL YECHISH LOGIKASI
# ========================================================

# Operator kodlari (taxminiy; xato bo'lsa shu yerda tuzatiladi)
PHONE_OPERATORS = {
    "90": "Beeline", "91": "Beeline",
    "93": "Ucell", "94": "Ucell", "50": "Ucell",
    "95": "Uzmobile", "99": "Uzmobile",
    "97": "Mobiuz", "88": "Mobiuz",
    "33": "Humans",
}


def normalize_phone(text):
    d = "".join(ch for ch in (text or "") if ch.isdigit())
    if len(d) == 9:
        d = "998" + d
    if len(d) == 12 and d.startswith("998"):
        return d
    return None


def format_phone(d):
    return f"+998 {d[3:5]} {d[5:8]} {d[8:10]} {d[10:12]}"


def method_label(ctype, cnum):
    if ctype == "Telefon":
        d = "".join(ch for ch in cnum if ch.isdigit())
        op = PHONE_OPERATORS.get(d[3:5]) if len(d) >= 5 else None
        return f"{op} (PAYNET)" if op else "Telefon (PAYNET)"
    if ctype == "Boshqa":
        return "Karta"
    return ctype


def mask_number(ctype, cnum):
    d = "".join(ch for ch in cnum if ch.isdigit())
    if ctype == "Telefon" and len(d) == 12:
        return f"+998 {d[3:5]} *** ** {d[-2:]}"
    if len(d) >= 12:
        return f"{d[:4]} **** **** {d[-4:]}"
    return "****"


def withdraw_method_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📱 Telefon raqamga (min {fmt_money(MIN_WITHDRAW_PHONE)})", callback_data="wd_method:phone")],
        [InlineKeyboardButton(f"💳 Kartaga (min {fmt_money(MIN_WITHDRAW_CARD)})", callback_data="wd_method:card")],
        [InlineKeyboardButton("‹ Bekor qilish", callback_data="wd_cancel")],
    ])


async def handle_withdraw_start(update, context, user_id):
    cache_clear_sub(user_id)
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    if balance < MIN_WITHDRAW_PHONE:
        await api_call(lambda: update.message.reply_text(
            f"🚫 Yetarli emas.\n\n📱 Telefonga minimal: *{fmt_money(MIN_WITHDRAW_PHONE)} so'm*\n"
            f"💳 Kartaga minimal: *{fmt_money(MIN_WITHDRAW_CARD)} so'm*\n\n💰 Balansingiz: *{fmt_money(balance)} so'm*",
            parse_mode=ParseMode.MARKDOWN), action_desc="wd_l")
        return
    await api_call(lambda: update.message.reply_text(
        f"💸 *Pul yechish*\n{DIVIDER}\n💰 *{fmt_money(balance)} so'm*\n\nQaysi usulda yechasiz?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=withdraw_method_keyboard()), action_desc="wd_s")


async def withdraw_type_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        if user_id != SUPER_ADMIN and await is_banned(user_id):
            await api_call(lambda: query.answer("🚫 Bloklangansiz", show_alert=True), action_desc="wt_ban")
            return
        data = query.data
        if data == "wd_cancel":
            await api_call(lambda: query.answer(), action_desc="wt_a")
            await api_call(lambda: query.message.edit_text("🚫 Bekor qilindi."), action_desc="wt_c")
            return
        user = await get_user(user_id)
        balance = user[1] if user else 0.0

        if data == "wd_method:phone":
            if balance < MIN_WITHDRAW_PHONE:
                await api_call(lambda: query.answer(f"🚫 Minimal {fmt_money(MIN_WITHDRAW_PHONE)} so'm", show_alert=True), action_desc="wt_l1")
                return
            await api_call(lambda: query.answer(), action_desc="wt_a2")
            context.user_data["state"] = "withdraw_phone"
            await api_call(lambda: query.message.edit_text(
                "📱 *Telefon raqamga yechish*\n\nRaqamingizni kiriting (masalan: `901234567` yoki `+998901234567`):",
                parse_mode=ParseMode.MARKDOWN), action_desc="wt_pp")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="wt_pk")
            return

        if data == "wd_method:card":
            if balance < MIN_WITHDRAW_CARD:
                await api_call(lambda: query.answer(
                    f"🚫 Kartaga yechish uchun minimal {fmt_money(MIN_WITHDRAW_CARD)} so'm. "
                    f"Telefonga {fmt_money(MIN_WITHDRAW_PHONE)} dan yechish mumkin.", show_alert=True), action_desc="wt_l2")
                return
            await api_call(lambda: query.answer(), action_desc="wt_a3")
            await api_call(lambda: query.message.edit_text(
                f"💳 *Kartaga yechish*\n{DIVIDER}\n💰 *{fmt_money(balance)} so'm*\nKarta turini tanlang:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=card_type_keyboard()), action_desc="wt_ct")
            return

        if data.startswith("wd_type:"):
            card_type = data.split(":", 1)[1]
            if card_type not in CARD_TYPES:
                await api_call(lambda: query.answer(), action_desc="wt_a4")
                return
            if balance < MIN_WITHDRAW_CARD:
                await api_call(lambda: query.answer(f"🚫 Minimal {fmt_money(MIN_WITHDRAW_CARD)} so'm", show_alert=True), action_desc="wt_l3")
                return
            await api_call(lambda: query.answer(), action_desc="wt_a5")
            context.user_data["withdraw_card_type"] = card_type
            context.user_data["state"] = "withdraw_card_number"
            await api_call(lambda: query.message.edit_text(
                f"💳 *{card_type}*\nKarta raqamini kiriting (16 raqam):", parse_mode=ParseMode.MARKDOWN), action_desc="wt_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="wt_kb")
            return
        await api_call(lambda: query.answer(), action_desc="wt_a6")
    except Exception:
        logger.exception("wt xato")


async def finalize_withdrawal(update, context, user_id, ctype, cnum, min_amount):
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    context.user_data["state"] = None
    context.user_data.pop("withdraw_card_type", None)
    if balance < min_amount:
        await api_call(lambda: update.message.reply_text("🚫 Yetarli emas.", reply_markup=main_keyboard(user_id)), action_desc="ws_l")
        return
    amount = balance
    wid = await create_withdrawal_atomic(user_id, amount, ctype, cnum)
    if wid is None:
        await api_call(lambda: update.message.reply_text("🚫 Yetarli emas.", reply_markup=main_keyboard(user_id)), action_desc="ws_l2")
        return
    cache_clear_sub(user_id)
    label = method_label(ctype, cnum)
    await api_call(lambda: update.message.reply_text(
        f"✅ <b>So'rovingiz qabul qilindi!</b>\n\n"
        f"🆔 So'rov: <b>#{wid}</b>\n"
        f"💵 Summa: <b>{fmt_money(amount)} so'm</b>\n"
        f"📡 Usul: <b>{html.escape(label)}</b>\n"
        f"🔢 Raqam: <code>{html.escape(cnum)}</code>\n\n"
        f"📢 Barcha to'lovlar {html.escape(OTZIF_CHANNEL)} kanalida ochiq e'lon qilinadi — haqiqiy to'lovlar shaffof ko'rinadi.\n\n"
        f"⏳ Admin tasdiqlagach pul karta yoki telefoningizga o'tkaziladi va sizga xabar beramiz.",
        parse_mode=ParseMode.HTML, reply_markup=withdraw_submitted_keyboard()), action_desc="ws_ok")
    await api_call(lambda: update.message.reply_text("👇", reply_markup=main_keyboard(user_id)), action_desc="ws_m")
    requester = update.effective_user
    uname = f"@{html.escape(requester.username)}" if requester.username else "yo'q"
    text = (f"💸 <b>Pul yechish</b>\n{DIVIDER}\n👤 {html.escape(requester.full_name)} {uname}\n"
            f"🆔 <code>{requester.id}</code>\n💰 <b>{fmt_money(amount)} so'm</b>\n"
            f"📡 {html.escape(label)}: <code>{html.escape(cnum)}</code>\n📋 <code>{wid}</code>")
    for aid in await get_admins():
        await api_call(lambda a=aid: context.bot.send_message(
            chat_id=a, text=text, parse_mode=ParseMode.HTML, reply_markup=admin_withdraw_keyboard(wid)),
            action_desc=f"ws_a:{aid}")


async def handle_withdraw_state(update, context, user_id, text):
    state = context.user_data.get("state")
    if state not in ("withdraw_card_number", "withdraw_phone"):
        return False
    if text == CANCEL_TEXT:
        context.user_data["state"] = None
        context.user_data.pop("withdraw_card_type", None)
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="ws_c")
        return True

    if state == "withdraw_phone":
        d = normalize_phone(text)
        if not d:
            await api_call(lambda: update.message.reply_text(
                "❌ Raqam noto'g'ri. Masalan: 901234567 yoki +998901234567"), action_desc="ws_pi")
            return True
        await finalize_withdrawal(update, context, user_id, "Telefon", format_phone(d), MIN_WITHDRAW_PHONE)
        return True

    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 16:
        await api_call(lambda: update.message.reply_text("❌ 16 raqam.", parse_mode=ParseMode.MARKDOWN), action_desc="ws_i")
        return True
    card_type = context.user_data.get("withdraw_card_type", "Boshqa")
    card_number = " ".join(digits[i:i+4] for i in range(0, 16, 4))
    await finalize_withdrawal(update, context, user_id, card_type, card_number, MIN_WITHDRAW_CARD)
    return True


async def announce_withdrawal(context, wid, user_id, amount, ctype, cnum):
    """Tasdiqlangan to'lovni otzif kanaliga yuboradi va taklif qilgan odamga xabar beradi."""
    label = method_label(ctype, cnum)
    chat = await api_call(lambda: context.bot.get_chat(user_id), action_desc="ann_chat")
    name = html.escape(getattr(chat, "full_name", None) or getattr(chat, "first_name", None) or "Foydalanuvchi") if chat else "Foydalanuvchi"
    bot_username = await get_bot_username(context)
    bot_url = f"https://t.me/{bot_username}" if bot_username else f"https://t.me/{BOT_TAG.lstrip('@')}"

    # 1) Otzif kanali
    ch_text = (
        "✅ <b>To'lov amalga oshirildi!</b>\n\n"
        f"🆔 So'rov: <b>#{wid}</b>\n"
        f"💵 Summa: <b>{fmt_money(amount)} so'm</b>\n"
        f"👤 Foydalanuvchi: <b>{name}</b>\n"
        f"📡 Usul: <b>{html.escape(label)}</b>\n"
        f"🔢 Raqam: <code>{html.escape(mask_number(ctype, cnum))}</code>\n"
        f"🤖 Bot: {html.escape(BOT_TAG)}\n\n"
        "🚀 Siz ham pul ishlashingiz mumkin! 👇"
    )
    sent = await api_call(lambda: context.bot.send_message(
        chat_id=OTZIF_CHANNEL, text=ch_text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 Botga o'tish", url=bot_url)]])),
        action_desc="otzif")
    if not sent:
        await api_call(lambda: context.bot.send_message(
            chat_id=SUPER_ADMIN,
            text=f"⚠️ #{wid} to'lovini {OTZIF_CHANNEL} kanaliga yuborib bo'lmadi. Bot kanalda ADMIN ekanini tekshiring."),
            action_desc="otzif_warn")

    # 2) Taklif qilgan odamga
    u = await get_user(user_id)
    ref = u[2] if u else None
    if not ref or ref == user_id:
        return
    st = await get_status(ref)
    if not st or st[0]:
        return
    ref_link = f"https://t.me/{bot_username}?start={ref}" if bot_username else None
    text = (
        "✅ <b>Do'stingiz pul yechib oldi!</b>\n\n"
        f"💵 Summa: <b>{fmt_money(amount)} so'm</b>\n"
        f"👤 Do'stingiz: <b>{name}</b>\n"
        f"📡 Usul: <b>{html.escape(label)}</b>\n"
        f"🆔 So'rov: <b>#{wid}</b>\n"
        f"🤖 Bot: {html.escape(BOT_TAG)}\n\n"
        "🎉 <b>Siz ham pul ishlashingiz mumkin!</b>\n\n"
        f"Do'stingiz <b>{name}</b> shu bot orqali <b>{fmt_money(amount)} so'm</b> yechib oldi — "
        "bu tizim haqiqatan ishlayotganini ko'rsatadi.\n\n"
        "👥 Siz ham do'stlaringizni taklif qiling, vazifalarni bajaring va balansingizni o'stiring — navbat sizda!\n\n"
        "🚀 Hoziroq pul ishlashni boshlang 👇"
    )
    await api_call(lambda: context.bot.send_message(
        chat_id=ref, text=text, parse_mode=ParseMode.HTML,
        reply_markup=invite_keyboard(ref_link) if ref_link else None), action_desc="ann_ref")


async def withdraw_admin_decision_callback(update, context):
    try:
        query = update.callback_query
        if query.from_user.id not in await get_admins():
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="wd_d")
            return
        action, wid_str = query.data.split(":", 1)
        wid = int(wid_str)
        row = await get_withdrawal(wid)
        if not row:
            await api_call(lambda: query.answer("❌ Topilmadi", show_alert=True), action_desc="wd_nf")
            return
        w_id, target, amount, ctype, cnum, status = row
        if status != "pending":
            await api_call(lambda: query.answer("ℹ️ Ko'rilgan", show_alert=True), action_desc="wd_dn")
            return
        await api_call(lambda: query.answer(), action_desc="wd_a")
        if action == "wdok":
            if not await claim_withdrawal_status(wid, "approved"):
                return
            await api_call(lambda: query.message.edit_reply_markup(reply_markup=None), action_desc="wd_oe")
            await api_call(lambda: query.message.reply_text(f"✅ #{wid} tasdiqlandi"), action_desc="wd_or")
            await api_call(lambda: context.bot.send_message(
                chat_id=target,
                text=(f"✅ *Pul tasdiqlandi!*\n💰 *{amount:,.0f} so'm*\n\n"
                      f"📢 To'lov {md_esc(OTZIF_CHANNEL)} kanalida e'lon qilindi."),
                parse_mode=ParseMode.MARKDOWN), action_desc="wd_uo")
            try:
                await announce_withdrawal(context, wid, target, amount, ctype, cnum)
            except Exception:
                logger.exception("announce xato")
        elif action == "wdno":
            context.user_data["state"] = "wd_reject_reason"
            context.user_data["wd_reject_id"] = wid
            await api_call(lambda: query.message.edit_text("✍️ Sababni yozing:", parse_mode=ParseMode.MARKDOWN), action_desc="wd_ne")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="wd_nk")
    except Exception:
        logger.exception("wdc xato")


async def handle_wd_reject_reason(update, context, user_id, text):
    if context.user_data.get("state") != "wd_reject_reason":
        return False
    wid = context.user_data.get("wd_reject_id")
    context.user_data["state"] = None
    context.user_data.pop("wd_reject_id", None)
    if text == CANCEL_TEXT:
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="wr_c")
        return True
    row = await get_withdrawal(wid) if wid else None
    if not row or row[5] != "pending":
        return True
    target, amount = row[1], row[2]
    if not await claim_withdrawal_status(wid, "rejected"):
        return True
    await add_balance(target, amount)
    cache_clear_sub(target)
    await api_call(lambda: context.bot.send_message(
        chat_id=target,
        text=f"❌ *Rad etildi*\n{DIVIDER}\n📝 {text}\n💰 *{amount:,.0f} so'm* qaytarildi",
        parse_mode=ParseMode.MARKDOWN), action_desc="wr_u")
    await api_call(lambda: update.message.reply_text("✅ Yuborildi", reply_markup=main_keyboard(user_id)), action_desc="wr_d")
    return True

# ========================================================
#  ADMIN PANEL METODLARI
# ========================================================

async def open_admin_panel(target, edit=False):
    target_uid = getattr(getattr(target, 'from_user', None), 'id', None)
    ref_price = await get_ref_price()
    channels = await get_channels()
    text = (f"⚙️ *Admin Panel*\n{DIVIDER}\n💵 Ref: *{ref_price:,.0f}*\n📢 Kanallar: *{len(channels)}*")
    if edit:
        await api_call(lambda: target.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_keyboard(target_uid)), action_desc="ap_e")
    else:
        await api_call(lambda: target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_keyboard(target_uid)), action_desc="ap_r")


async def admin_panel_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        if user_id not in await get_admins():
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="ad_d")
            return
        data = query.data
        await api_call(lambda: query.answer(), action_desc="ad_a")
        if data == "admin_close":
            await api_call(lambda: query.message.delete(), action_desc="ad_x")
            return
        if data == "admin_back":
            await open_admin_panel(query, edit=True)
            return
        if data == "admin_add_channel":
            context.user_data["state"] = "add_channel"
            await api_call(lambda: query.message.edit_text(f"➕ *Kanal qo'shish*\n{DIVIDER}\n`@username Nomi`\n⚠️ Bot admin bo'lishi kerak!", parse_mode=ParseMode.MARKDOWN), action_desc="ac_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="ac_kb")
            return
        if data == "admin_remove_channel":
            channels = await get_channels()
            if not channels:
                await api_call(lambda: query.message.edit_text("📋 Bo'sh", reply_markup=back_keyboard()), action_desc="rc_e")
                return
            await api_call(lambda: query.message.edit_text("➖ Tanlang:", parse_mode=ParseMode.MARKDOWN, reply_markup=remove_channel_keyboard(channels)), action_desc="rc_p")
            return
        if data == "admin_list_channels":
            channels = await get_channels()
            if not channels:
                body = "📋 Bo'sh"
            else:
                body = "📋 *Kanallar:*\n" + "\n".join([f"• *{n}* — `{c}`" for c, n in channels])
            await api_call(lambda: query.message.edit_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=back_keyboard()), action_desc="lc")
            return
        if data == "admin_set_price":
            context.user_data["state"] = "set_price"
            current = await get_ref_price()
            await api_call(lambda: query.message.edit_text(f"💵 Hozirgi: *{current:,.0f}*\nYangi narxni kiriting:", parse_mode=ParseMode.MARKDOWN), action_desc="sp_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="sp_kb")
            return
        if data == "admin_add_admin":
            if user_id != SUPER_ADMIN:
                await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="aa_d")
                return
            context.user_data["state"] = "add_admin"
            await api_call(lambda: query.message.edit_text("👑 *Admin ID kiriting:*", parse_mode=ParseMode.MARKDOWN), action_desc="aa_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="aa_kb")
            return
        if data == "admin_remove_admin":
            if user_id != SUPER_ADMIN:
                await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="ra_d")
                return
            others = [a for a in await get_admins() if a != SUPER_ADMIN]
            if not others:
                await api_call(lambda: query.message.edit_text("Boshqa admin yo'q", reply_markup=back_keyboard()), action_desc="ra_e")
                return
            await api_call(lambda kb=admin_list_keyboard(await get_admins()): query.message.edit_text("🚫 O'chirish:", reply_markup=kb), action_desc="ra_l")
            return
        if data == "admin_add_money":
            context.user_data["state"] = "add_money"
            await api_call(lambda: query.message.edit_text("💰 *Pul qo'shish*\n\n`user_id miqdor`", parse_mode=ParseMode.MARKDOWN), action_desc="am_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="am_kb")
            return
        if data == "admin_remove_money":
            context.user_data["state"] = "remove_money"
            await api_call(lambda: query.message.edit_text("💸 *Pul ayirish*\n\n`user_id miqdor`", parse_mode=ParseMode.MARKDOWN), action_desc="rm_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="rm_kb")
            return
        if data == "admin_maintenance":
            global _bot_maintenance
            if _bot_maintenance:
                _bot_maintenance = False
                await api_call(lambda: query.message.edit_text("✅ Bot yoqildi!", reply_markup=back_keyboard()), action_desc="mt_off")
            else:
                await api_call(lambda: query.message.edit_text("🛠 *Qanday xabar?*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("1️⃣ Standart", callback_data="maint_standard")],
                        [InlineKeyboardButton("2️⃣ O'zim yozaman", callback_data="maint_custom")],
                        [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
                    ])), action_desc="mt_m")
            return
        if data == "maint_standard":
            _bot_maintenance = True
            await api_call(lambda: query.message.edit_text("✅ Standart xabar yoqildi!", reply_markup=back_keyboard()), action_desc="mt_s")
            return
        if data == "maint_custom":
            context.user_data["state"] = "maint_custom_msg"
            await api_call(lambda: query.message.edit_text("✍️ Xabarni kiriting:", parse_mode=ParseMode.MARKDOWN), action_desc="mt_c")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="mt_ck")
            return
        if data == "admin_bonus_settings":
            interval = await get_setting("bonus_interval", "86400")
            bmin = await get_setting("bonus_min", "10")
            bmax = await get_setting("bonus_max", "900")
            enabled = await get_setting("bonus_enabled", "1")
            interval_text = "⏰ 1 soatlik" if interval == "3600" else "⏰ 1 kunlik"
            enabled_text = "✅ Yoqilgan" if enabled == "1" else "❌ O'chirilgan"
            await api_call(lambda: query.message.edit_text(
                f"🎁 *Bonus*\n{DIVIDER}\n📊 {enabled_text}\n⏰ {interval_text}\n💰 *{bmin}-{bmax} so'm*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏰ 1 soat", callback_data="bonus_int_3600"),
                     InlineKeyboardButton("⏰ 1 kun", callback_data="bonus_int_86400")],
                    [InlineKeyboardButton("💰 Miqdor", callback_data="bonus_amount")],
                    [InlineKeyboardButton("✅/❌ Yoqish", callback_data="bonus_toggle")],
                    [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
                ])), action_desc="bs")
            return
        if data.startswith("bonus_int_"):
            await set_setting("bonus_interval", data.split("_")[-1])
            await api_call(lambda: query.message.edit_text("✅", reply_markup=back_keyboard()), action_desc="bi")
            return
        if data == "bonus_amount":
            context.user_data["state"] = "bonus_amount"
            await api_call(lambda: query.message.edit_text("💰 `min max`", parse_mode=ParseMode.MARKDOWN), action_desc="ba_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="ba_kb")
            return
        if data == "bonus_toggle":
            cur = await get_setting("bonus_enabled", "1")
            await set_setting("bonus_enabled", "0" if cur == "1" else "1")
            await api_call(lambda: query.message.edit_text("✅", reply_markup=back_keyboard()), action_desc="bt")
            return
        if data == "admin_create_promo":
            context.user_data["state"] = "create_promo"
            await api_call(lambda: query.message.edit_text("🎟 `KOD miqdor max`\nMasalan: `SALE 5000 100`", parse_mode=ParseMode.MARKDOWN), action_desc="cp_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="cp_kb")
            return
        if data == "admin_list_promos":
            promos = await get_all_promocodes()
            if not promos:
                await api_call(lambda: query.message.edit_text("📋 Bo'sh", reply_markup=back_keyboard()), action_desc="lp_e")
                return
            await api_call(lambda: query.message.edit_text("🎟 Promokodlar:", reply_markup=promo_list_keyboard(promos)), action_desc="lp")
            return
        if data == "admin_payment_channel":
            pc = await get_payment_channel()
            if pc:
                await api_call(lambda: query.message.edit_text(
                    f"💳 *{pc[1]}*\n`{pc[0]}`\n{pc[2]}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✏️ O'zgartirish", callback_data="set_pay_channel")],
                        [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
                    ])), action_desc="pc_i")
            else:
                context.user_data["state"] = "set_payment_channel"
                await api_call(lambda: query.message.edit_text("💳 `@username Nomi|Tavsif`", parse_mode=ParseMode.MARKDOWN), action_desc="pc_p")
                await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="pc_kb")
                return
        if data == "set_pay_channel":
            context.user_data["state"] = "set_payment_channel"
            await api_call(lambda: query.message.edit_text("💳 `@username Nomi|Tavsif`", parse_mode=ParseMode.MARKDOWN), action_desc="spc_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="spc_kb")
            return
        if data == "admin_stats":
            count, total = await get_stats()
            await api_call(lambda: query.message.edit_text(f"📊 *{count}* ta foydalanuvchi\n💰 *{total:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN, reply_markup=back_keyboard()), action_desc="as")
            return
        if data == "admin_broadcast":
            context.user_data["state"] = "broadcast"
            await api_call(lambda: query.message.edit_text("✍️ Xabar:", parse_mode=ParseMode.MARKDOWN), action_desc="bc_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="bc_kb")
            return
        if data in ("admin_ban_user", "admin_unban_user"):
            if user_id != SUPER_ADMIN:     # faqat ega
                await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="bn_d")
                return
            ban = data == "admin_ban_user"
            context.user_data["state"] = "ban_user" if ban else "unban_user"
            prompt = ("🚫 Bloklanadigan foydalanuvchi ID sini yuboring.\nSabab yozish ixtiyoriy: `ID sabab`" if ban
                      else "✅ Blokdan chiqariladigan foydalanuvchi ID sini yuboring:")
            await api_call(lambda: query.message.edit_text(prompt, parse_mode=ParseMode.MARKDOWN), action_desc="bn_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="bn_kb")
            return
        if data == "admin_leave_ownership":
            await api_call(lambda: query.message.edit_text("⚠️ *Egallikdan chiqasizmi?*", parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_leave_1()), action_desc="lv1")
            return
        if data == "leave_cancel":
            await open_admin_panel(query, edit=True)
            return
        if data == "leave_yes_1":
            await api_call(lambda: query.message.edit_text("🤔 *100% ishonchingiz komilmi?*", parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_leave_2()), action_desc="lv2")
            return
        if data == "leave_yes_2":
            await api_call(lambda: query.message.edit_text("😢 *Rostdan ham?*", parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_leave_3()), action_desc="lv3")
            return
        if data == "leave_yes_3":
            context.user_data["left_ownership"] = True
            await api_call(lambda: query.message.edit_text("✅ Chiqdingiz!", parse_mode=ParseMode.MARKDOWN), action_desc="lvd")
            for aid in await get_admins():
                await api_call(lambda a=aid: context.bot.send_message(chat_id=a, text=f"⚠️ {query.from_user.full_name} chiqdi!", parse_mode=ParseMode.MARKDOWN), action_desc="lvn")
            return
        if data.startswith("rmch:"):
            cid = data.split(":", 1)[1]
            await remove_channel_db(cid)
            cache_clear_sub()
            channels = await get_channels()
            if channels:
                await api_call(lambda: query.message.edit_text("✅", reply_markup=remove_channel_keyboard(channels)), action_desc="rch_o")
            else:
                await api_call(lambda: query.message.edit_text("✅ Bo'sh", reply_markup=back_keyboard()), action_desc="rch_e")
            return
        if data.startswith("rmadm:"):
            aid = int(data.split(":", 1)[1])
            if await remove_admin_db(aid):
                await api_call(lambda: query.answer("✅"), action_desc="ra_a")
                others = [a for a in await get_admins() if a != SUPER_ADMIN]
                if not others:
                    await api_call(lambda: query.message.edit_text("Yo'q", reply_markup=back_keyboard()), action_desc="ra_ne")
                else:
                    await api_call(lambda kb=admin_list_keyboard(await get_admins()): query.message.edit_text("🚫", reply_markup=kb), action_desc="ra_nl")
            return
        if data.startswith("delpromo:"):
            pid = int(data.split(":", 1)[1])
            await delete_promocode(pid)
            await api_call(lambda: query.answer("✅"), action_desc="dp_a")
            await api_call(lambda kb=promo_list_keyboard(await get_all_promocodes()): query.message.edit_text("🎟:", reply_markup=kb), action_desc="dp_e")
            return
    except Exception:
        logger.exception("admin_panel xato")
# ========================================================
#  OMMAVIY XABAR (orqa fonda), ZAXIRA NUSXA
# ========================================================

async def _send_one(bot, uid, text):
    """'ok' | 'blocked' | 'failed'"""
    plain = False
    for _ in range(4):
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode=None if plain else ParseMode.MARKDOWN)
            return "ok"
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 0.5)
        except Forbidden:
            return "blocked"
        except BadRequest as e:
            msg = str(e).lower()
            if not plain and ("parse" in msg or "entities" in msg):
                plain = True       # Markdown xato - oddiy matn bilan yuboramiz
                continue
            return "failed"
        except (TimedOut, NetworkError):
            await asyncio.sleep(2)
        except TelegramError:
            return "failed"
        except Exception:
            return "failed"
    return "failed"


async def run_broadcast(bot, admin_chat, user_ids, text):
    total = len(user_ids)
    counts = {"ok": 0, "blocked": 0, "failed": 0}
    progress = await api_call(lambda: bot.send_message(chat_id=admin_chat, text=f"📤 0/{total}"), action_desc="bc_start")
    last_edit = time.monotonic()
    BATCH = 20   # Telegram limiti ~30 xabar/soniya, biz 20/soniya yuboramiz
    done = 0
    for i in range(0, total, BATCH):
        t0 = time.monotonic()
        chunk = user_ids[i:i + BATCH]
        results = await asyncio.gather(*[_send_one(bot, u, text) for u in chunk], return_exceptions=True)
        for r in results:
            counts[r if r in counts else "failed"] += 1
        done += len(chunk)
        if progress and time.monotonic() - last_edit > 5:
            last_edit = time.monotonic()
            await api_call(lambda d=done: progress.edit_text(f"📤 {d}/{total}"), action_desc="bc_prog")
        await asyncio.sleep(max(0.0, 1.05 - (time.monotonic() - t0)))
    report = (f"✅ Xabar yuborish tugadi\n{DIVIDER}\n👥 Jami: {total}\n"
              f"✅ Yetkazildi: {counts['ok']}\n🚫 Botni bloklagan: {counts['blocked']}\n⚠️ Xato: {counts['failed']}")
    if progress:
        await api_call(lambda: progress.edit_text(report), action_desc="bc_done")
    else:
        await api_call(lambda: bot.send_message(chat_id=admin_chat, text=report), action_desc="bc_done2")


async def send_backup(bot, chat_id, caption="💾 Baza zaxira nusxasi"):
    data = await db_dump_all()
    raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    fname = f"backup_{_utcnow().strftime('%Y-%m-%d_%H-%M')}.json"
    users = len(data.get("users", {}).get("rows", []))
    await api_call(lambda: bot.send_document(
        chat_id=chat_id, document=InputFile(io.BytesIO(raw), filename=fname),
        caption=f"{caption}\n👥 Foydalanuvchilar: {users}"), action_desc="backup")


async def backup_command(update, context):
    try:
        if not update.message or update.effective_user.id != SUPER_ADMIN:
            return
        await send_backup(context.bot, update.effective_chat.id)
    except Exception:
        logger.exception("backup xato")


async def backup_loop(bot):
    await asyncio.sleep(90)
    while True:
        try:
            await send_backup(bot, SUPER_ADMIN, "💾 Avtomatik zaxira nusxa")
        except Exception:
            logger.exception("avto-backup xato")
        await asyncio.sleep(BACKUP_INTERVAL)


_last_action = {}


def _throttled(user_id, gap=0.4):
    now = time.monotonic()
    last = _last_action.get(user_id, 0.0)
    if now - last < gap:
        return True
    if len(_last_action) > 20000:
        _last_action.clear()
    _last_action[user_id] = now
    return False


async def post_init(app):
    await init_db()
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    try:
        await auto_migrate_from_sqlite(app.bot)
    except Exception:
        logger.exception("avto-ko'chirish xato")
    if not USE_PG and os.environ.get("RENDER"):
        await api_call(lambda: app.bot.send_message(
            chat_id=SUPER_ADMIN,
            text=("⚠️ DIQQAT: doimiy baza (DATABASE_URL) ulanmagan! Bot SQLite fayldan foydalanmoqda — "
                  "Render qayta deploy qilganda ma'lumotlar o'chib ketishi mumkin. "
                  "Render Environment ga DATABASE_URL qo'shing.")), action_desc="warn_db")
    spawn(backup_loop(app.bot))


async def post_shutdown(app):
    await db_close()


# ========================================================
#  MA'LUMOTLARNI KO'CHIRISH / TIKLASH (SQLite .db yoki zaxira .json)
# ========================================================

def read_sqlite_file(path):
    con = sqlite3.connect(path)
    data = {}
    try:
        for t in TABLES:
            try:
                cur = con.execute(f"SELECT * FROM {t}")
            except sqlite3.OperationalError:
                continue
            data[t] = ([d[0] for d in cur.description], [list(r) for r in cur.fetchall()])
    finally:
        con.close()
    return data


def read_json_bytes(raw):
    obj = json.loads(raw.decode("utf-8"))
    return {t: (v["columns"], v["rows"]) for t, v in obj.items() if t in TABLES}


async def _target_columns(t):
    if USE_PG:
        rows = await db_all("SELECT column_name, data_type FROM information_schema.columns WHERE table_name=?", (t,))
        return {r[0]: r[1] for r in rows}
    rows = await db_all(f"PRAGMA table_info({t})")
    return {r[1]: (r[2] or "").lower() for r in rows}


def _conv(val, typ):
    if val is None or not USE_PG:
        return val
    if typ.startswith("timestamp"):
        if isinstance(val, datetime):
            return val
        try:
            return datetime.fromisoformat(str(val).replace("T", " ")[:26])
        except Exception:
            return None
    if typ in ("bigint", "integer", "smallint"):
        return int(val)
    if typ in ("double precision", "real", "numeric"):
        return float(val)
    return str(val)


async def import_tables(data):
    """Ma'lumotni bazaga qo'shadi. Mavjud qatorlarni O'ZGARTIRMAYDI va o'chirmaydi."""
    report = {}
    for t in TABLES:
        if t not in data:
            continue
        cols, rows = data[t]
        types = await _target_columns(t)
        use = [(i, c) for i, c in enumerate(cols) if c in types]
        if not use:
            continue
        sql = (f"INSERT INTO {t} ({', '.join(c for _, c in use)}) VALUES "
               f"({', '.join('?' for _ in use)}) ON CONFLICT DO NOTHING")
        n = 0
        for row in rows:
            try:
                n += await db_exec(sql, [_conv(row[i], types[c]) for i, c in use])
            except Exception:
                logger.exception("import xato (%s)", t)
        report[t] = (n, len(rows))
    if USE_PG:
        for t in ("withdrawals", "support_messages", "promocodes", "promo_uses", "bonus_claims", "payment_channels"):
            try:
                await db_exec(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),1), "
                              f"(SELECT MAX(id) FROM {t}) IS NOT NULL)")
            except Exception:
                logger.exception("sequence xato (%s)", t)
    _status_cache.clear()
    return report


def _report_text(report):
    return "\n".join(f"• {t}: {n}/{total}" for t, (n, total) in report.items()) or "hech narsa topilmadi"


async def auto_migrate_from_sqlite(bot):
    """PostgreSQL bo'sh bo'lsa va eski SQLite fayl (DB_PATH) mavjud bo'lsa - avtomatik ko'chiradi."""
    if not USE_PG or not os.path.exists(DB_PATH):
        return
    r = await db_one("SELECT COUNT(*) FROM users")
    if r and int(r[0]) > 0:
        return
    data = await asyncio.to_thread(read_sqlite_file, DB_PATH)
    if not data.get("users") or not data["users"][1]:
        return
    report = await import_tables(data)
    logger.info("Eski SQLite baza PostgreSQL ga ko'chirildi: %s", report)
    await api_call(lambda: bot.send_message(
        chat_id=SUPER_ADMIN,
        text=f"✅ Eski baza ({DB_PATH}) doimiy PostgreSQL bazaga ko'chirildi:\n{_report_text(report)}"),
        action_desc="migr_note")


async def restore_document(update, context):
    """Ega .db yoki zaxira .json faylni botga yuborib, sarlavhaga /restore yozsa - ma'lumot tiklanadi."""
    try:
        msg = update.message
        if not msg or not msg.document or update.effective_user.id != SUPER_ADMIN:
            return
        if not (msg.caption or "").strip().lower().startswith("/restore"):
            return
        name = (msg.document.file_name or "").lower()
        if not name.endswith((".json", ".db", ".sqlite", ".sqlite3")):
            await api_call(lambda: msg.reply_text("❌ Faqat .db yoki .json fayl."), action_desc="rs_ext")
            return
        f = await context.bot.get_file(msg.document.file_id)
        raw = bytes(await f.download_as_bytearray())
        if name.endswith(".json"):
            data = read_json_bytes(raw)
        else:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp.write(raw)
                path = tmp.name
            try:
                data = await asyncio.to_thread(read_sqlite_file, path)
            finally:
                os.unlink(path)
        report = await import_tables(data)
        await api_call(lambda: msg.reply_text(f"✅ Tiklandi (mavjud ma'lumot o'zgarmadi):\n{_report_text(report)}"), action_desc="rs_ok")
    except Exception:
        logger.exception("restore xato")
        await api_call(lambda: update.message.reply_text("❌ Tiklashda xato. Log'ni tekshiring."), action_desc="rs_err")


# ========================================================
#  ADMIN MATN STATE, ASOSIY BUTTONLAR, ERROR HANDLER, MAIN
# ========================================================

async def handle_admin_text_state(update, context, user_id, text):
    state = context.user_data.get("state")
    if state not in ("add_channel", "set_price", "broadcast", "add_admin",
                     "add_money", "remove_money", "maint_custom_msg", "bonus_amount",
                     "create_promo", "set_payment_channel", "ban_user", "unban_user"):
        return False
    if text == CANCEL_TEXT:
        context.user_data["state"] = None
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="cs")
        return True
    if state == "add_channel":
        context.user_data["state"] = None
        parts = text.strip().split(maxsplit=1)
        cid = parts[0]
        given = parts[1] if len(parts) > 1 else None
        chat = await api_call(lambda: context.bot.get_chat(cid), action_desc="gc", swallow=True, default=None)
        title = given or (chat.title if chat else cid) or cid
        await add_channel_db(cid, title)
        cache_clear_sub()
        await api_call(lambda: update.message.reply_text(f"✅ *{title}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="ca")
        return True
    if state == "set_price":
        context.user_data["state"] = None
        try:
            price = float(text.strip())
        except:
            await api_call(lambda: update.message.reply_text("❌ Raqam", reply_markup=main_keyboard(user_id)), action_desc="spe")
            return True
        await set_setting("ref_price", price)
        await api_call(lambda: update.message.reply_text(f"✅ *{price:,.0f}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="spo")
        return True
    if state == "add_admin":
        context.user_data["state"] = None
        if user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text("⛔", reply_markup=main_keyboard(user_id)), action_desc="aap")
            return True
        try:
            new_admin = int(text.strip())
        except:
            await api_call(lambda: update.message.reply_text("❌ ID", reply_markup=main_keyboard(user_id)), action_desc="aae")
            return True
        await add_admin_db(new_admin)
        await api_call(lambda: update.message.reply_text(f"✅ `{new_admin}`", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="aao")
        return True
    if state == "add_money":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            tid = int(parts[0])
            amount = float(parts[1])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="ame")
            return True
        if not await get_user_by_id(tid):
            await create_user(tid, None, registered=1)
        await add_balance(tid, amount)
        cache_clear_sub(tid)
        await api_call(lambda: update.message.reply_text(f"✅ *+{amount:,.0f}* → `{tid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="amo")
        await api_call(lambda: context.bot.send_message(chat_id=tid, text=f"💰 *+{amount:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="amn")
        return True
    if state == "remove_money":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            tid = int(parts[0])
            amount = float(parts[1])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="rme")
            return True
        await add_balance(tid, -amount)
        cache_clear_sub(tid)
        await api_call(lambda: update.message.reply_text(f"✅ *-{amount:,.0f}* → `{tid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="rmo")
        await api_call(lambda: context.bot.send_message(chat_id=tid, text=f"💸 *-{amount:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="rmn")
        return True
    if state == "maint_custom_msg":
        context.user_data["state"] = None
        global _bot_maintenance_msg, _bot_maintenance
        _bot_maintenance_msg = text
        _bot_maintenance = True
        await api_call(lambda: update.message.reply_text("✅ Yoqildi!", reply_markup=main_keyboard(user_id)), action_desc="mcd")
        return True
    if state == "bonus_amount":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            bmin, bmax = int(parts[0]), int(parts[1])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="bae")
            return True
        await set_setting("bonus_min", bmin)
        await set_setting("bonus_max", bmax)
        await api_call(lambda: update.message.reply_text(f"✅ *{bmin}-{bmax}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="bao")
        return True
    if state == "create_promo":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            code = parts[0]
            amount = float(parts[1])
            max_uses = int(parts[2])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="cpe")
            return True
        pid = await create_promocode(code, amount, max_uses)
        if pid:
            await api_call(lambda: update.message.reply_text(f"✅ *{code.upper()}* - *{amount:,.0f}* ({max_uses})", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="cpo")
        else:
            await api_call(lambda: update.message.reply_text("❌ Mavjud", reply_markup=main_keyboard(user_id)), action_desc="cpd")
        return True
    if state == "set_payment_channel":
        context.user_data["state"] = None
        try:
            parts = text.split("|", 1)
            channel_part = parts[0].strip().split(maxsplit=1)
            cid = channel_part[0]
            cname = channel_part[1] if len(channel_part) > 1 else cid
            desc = parts[1].strip() if len(parts) > 1 else "To'lovlar"
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="pce")
            return True
        await set_payment_channel(cid, cname, desc)
        await api_call(lambda: update.message.reply_text(f"✅ *{cname}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="pco")
        return True
    if state == "ban_user" or state == "unban_user":
        context.user_data["state"] = None
        if user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text("⛔", reply_markup=main_keyboard(user_id)), action_desc="bnx")
            return True
        parts = text.strip().split(maxsplit=1)
        try:
            tid = int(parts[0])
        except Exception:
            await api_call(lambda: update.message.reply_text("❌ ID noto'g'ri", reply_markup=main_keyboard(user_id)), action_desc="bne")
            return True
        if state == "ban_user":
            if tid == SUPER_ADMIN:
                await api_call(lambda: update.message.reply_text("❌ Egani bloklab bo'lmaydi", reply_markup=main_keyboard(user_id)), action_desc="bns")
                return True
            reason = parts[1] if len(parts) > 1 else None
            if not await get_user_by_id(tid):
                await create_user(tid, None, registered=1)
            await set_banned(tid, True)
            await api_call(lambda: update.message.reply_text(f"🚫 {tid} bloklandi.", reply_markup=main_keyboard(user_id)), action_desc="bno")
            note = BAN_TEXT + (f"\n\nSabab: {reason}" if reason else "")
            await api_call(lambda: context.bot.send_message(chat_id=tid, text=note), action_desc="bnn")
        else:
            await set_banned(tid, False)
            await api_call(lambda: update.message.reply_text(f"✅ {tid} blokdan chiqarildi.", reply_markup=main_keyboard(user_id)), action_desc="ubo")
            await api_call(lambda: context.bot.send_message(chat_id=tid, text="✅ Akkauntingiz blokdan chiqarildi. /start bosing."), action_desc="ubn")
        return True
    if state == "broadcast":
        context.user_data["state"] = None
        user_ids = await get_all_user_ids()
        await api_call(lambda: update.message.reply_text(
            f"📤 Yuborish boshlandi ({len(user_ids)} ta). Bot ishlashda davom etadi, tugagach hisobot beraman.",
            reply_markup=main_keyboard(user_id)), action_desc="bcs")
        spawn(run_broadcast(context.bot, update.effective_chat.id, user_ids, text))
        return True
    return False


async def handle_buttons(update, context):
    try:
        if not update.message or not update.message.text:
            return
        text = update.message.text
        user_id = update.effective_user.id
        is_admin = user_id in await get_admins()
        if _bot_maintenance and user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text(_bot_maintenance_msg, parse_mode=ParseMode.MARKDOWN), action_desc="mb")
            return
        if context.user_data.get("left_ownership"):
            if text == "⚙️ Admin Panel":
                await api_call(lambda: update.message.reply_text("🚪 Chiqdingiz.", parse_mode=ParseMode.MARKDOWN), action_desc="loa")
                return
        if user_id != SUPER_ADMIN and await is_banned(user_id):
            await api_call(lambda: update.message.reply_text(BAN_TEXT), action_desc="bb")
            return
        if not is_admin and _throttled(user_id):
            return
        _st = await get_status(user_id)
        if _st is not None and _st[1] == 0 and not is_admin:
            await registration_step(context, user_id, update.effective_chat.id)
            return
        if await handle_support_state(update, context, user_id, text):
            return
        if await handle_support_reply_state(update, context, user_id, text):
            return
        if await handle_wd_reject_reason(update, context, user_id, text):
            return
        if await handle_withdraw_state(update, context, user_id, text):
            return
        if await handle_promo_state(update, context, user_id, text):
            return
        if is_admin and await handle_admin_text_state(update, context, user_id, text):
            return
        if not await is_subscribed(user_id, context):
            await show_subscription_gate(update, context)
            return
        if text == "💰 Pul ishlash":
            await handle_earn(update, context, user_id)
        elif text == "💸 Pul yechish":
            await handle_withdraw_start(update, context, user_id)
        elif text == "👤 Balans":
            await handle_balance(update, user_id)
        elif text == "📊 Statistika" and is_admin:
            await handle_stats(update)
        elif text == "📢 Xabar yuborish" and is_admin:
            context.user_data["state"] = "broadcast"
            await api_call(lambda: update.message.reply_text("✍️ Xabar:", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="bcp")
        elif text == "☎️ Murojaat":
            await handle_support_start(update, context, user_id)
        elif text == "🎁 Bonus":
            await handle_bonus(update, context, user_id)
        elif text == "🎟 Promokod":
            await handle_promo_start(update, context, user_id)
        elif text == "💳 To'lov kanali":
            await handle_payment_channel(update, context, user_id)
        elif text == RULES_BUTTON:
            await handle_rules(update)
        elif text == "⚙️ Admin Panel" and is_admin:
            await open_admin_panel(update.message)
        else:
            await api_call(lambda: update.message.reply_text("🤔 Tanlang 👇", reply_markup=main_keyboard(user_id)), action_desc="un")
    except Exception:
        logger.exception("handle_buttons xato")


async def error_handler(update, context):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        return
    logger.error("Xato: %s", err, exc_info=err)


def main():
    if not TOKEN:
        print("❌ BOT_TOKEN yo'q!")
        return
    # Muhim: oldin bitta ulanish (pool_size=1) barcha foydalanuvchilarni navbatga qo'yib, botni qotirardi.
    request = HTTPXRequest(connection_pool_size=256, connect_timeout=20.0, read_timeout=20.0,
                           write_timeout=20.0, pool_timeout=30.0)
    get_updates_request = HTTPXRequest(connection_pool_size=4, connect_timeout=20.0, read_timeout=30.0,
                                       write_timeout=20.0, pool_timeout=20.0)
    app = (ApplicationBuilder().token(TOKEN).request(request).get_updates_request(get_updates_request)
           .concurrent_updates(True)          # foydalanuvchilar bir-birini kutmaydi
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(captcha_callback, pattern="^cap:"))
    app.add_handler(CallbackQueryHandler(refresh_ref_callback, pattern="^refresh_ref$"))
    app.add_handler(CallbackQueryHandler(support_answer_callback, pattern="^sup_answer:"))
    app.add_handler(CallbackQueryHandler(withdraw_type_callback, pattern="^(wd_type:|wd_method:|wd_cancel$)"))
    app.add_handler(CallbackQueryHandler(withdraw_admin_decision_callback, pattern="^(wdok:|wdno:)"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^(admin_|maint_|bonus_|rmch:|rmadm:|delpromo:|set_pay_channel|leave_)"))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(user_id=SUPER_ADMIN), restore_document))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_error_handler(error_handler)
    print("🚀 Bot ishga tushdi!")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == '__main__':
    main()
