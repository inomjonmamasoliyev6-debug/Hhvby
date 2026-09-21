import asyncio
import html
import socket
import sqlite3
import subprocess
import sys
import tempfile
import io
import json
import logging
import os
import random
import re
import time
import traceback
import weakref
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
    BotCommand,
    BotCommandScopeAllGroupChats,
    MessageEntity,
)
try:
    from telegram import CopyTextButton
except ImportError:  # eski kutubxona (21.7 dan past) - nusxalash tugmasi ko'rinmaydi
    CopyTextButton = None
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, TimedOut, NetworkError, Forbidden, RetryAfter, BadRequest
try:
    from telegram.error import Conflict
except ImportError:
    class Conflict(TelegramError):
        pass
from telegram.ext import (
    ApplicationBuilder,
    ExtBot,
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
TOKEN = os.environ.get("BOT_TOKEN_OVERRIDE") or "8856340901:AAF64e7WUXPxQBAmzTgWQWcM45Z1oyynCFw"
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
TRANSFER_BUTTON = "💸 Do'stga pul yuborish"
MIN_TRANSFER = 1000.0   # do'stga yuborish uchun minimal summa
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


def _task_done(t):
    _bg_tasks.discard(t)
    if t.cancelled():
        return
    exc = t.exception()
    if exc:
        logger.error("Orqa fon vazifasi xatosi: %r", exc, exc_info=exc)


def spawn(coro):
    """Orqa fonda vazifa ishga tushirish (bot qotib qolmasligi uchun)."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_task_done)
    return t


# ========================================================
#  EGAGA XABAR BERISH, XATOLARNI KUZATISH, YORDAMCHILAR
# ========================================================

BOT_VERSION = "v8 (2026-09-21)"
INSTANCE_ID = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=5))
START_TIME = time.time()

_BOT = None
_notify_q = None
_notify_recent = {}
_singleton_note = ""
_IGNORABLE_ERRORS = (
    "message is not modified", "query is too old", "message to edit not found",
    "message to delete not found", "message can't be deleted", "bot was blocked",
    "user is deactivated", "have no rights to send", "terminated by other getupdates",
    "telegram.error.networkerror", "telegram.error.timedout", "httpx.connecterror", "httpx.readerror",
    "httpx.connecttimeout", "httpx.readtimeout", "httpx.pooltimeout", "httpx.remoteprotocolerror",
)


def notify_owner(text, key=None, ttl=60):
    """Egaga xabar yuborish (navbat orqali; bir xil xabar ttl soniyada bir marta)."""
    if _notify_q is None:
        return
    k = key or text[:160]
    now = time.monotonic()
    if now - _notify_recent.get(k, -1e9) < ttl:
        return
    if len(_notify_recent) > 3000:
        _notify_recent.clear()
    _notify_recent[k] = now
    try:
        _notify_q.put_nowait(text[:3900])
    except asyncio.QueueFull:
        pass


async def _notify_worker():
    while True:
        text = await _notify_q.get()
        for _ in range(3):
            try:
                if _BOT is not None:
                    await _BOT.send_message(chat_id=SUPER_ADMIN, text=text)
                break
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 0.5)
            except Exception:
                break
        await asyncio.sleep(0.4)


async def notify_event(text):
    """Oddiy voqealar (yangi foydalanuvchi, referal, to'lov...). Ega o'chirib qo'yishi mumkin."""
    try:
        if (await get_setting("owner_events", "1")) != "0":
            notify_owner(text, key=text, ttl=5)
    except Exception:
        pass


class OwnerLogHandler(logging.Handler):
    """Kodda qayd etilgan har bir XATO (logger.error / logger.exception) egaga ham yuboriladi."""
    def emit(self, record):
        try:
            if record.levelno < logging.ERROR:
                return
            if record.name.startswith(("httpx", "httpcore")):
                return
            msg = self.format(record)
            low = msg.lower()
            if any(s in low for s in _IGNORABLE_ERRORS):
                return
            notify_owner("🐞 Xato (nusxa " + INSTANCE_ID + "):\n" + msg[-3500:], key=record.getMessage()[:160])
        except Exception:
            pass


_user_locks = weakref.WeakValueDictionary()


def user_lock(user_id):
    """Bir foydalanuvchining pulga tegishli amallari bir vaqtda ikki marta bajarilmasligi uchun."""
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def channel_url(cid):
    cid = (cid or "").strip()
    if cid.startswith("http"):
        return cid
    name = cid.lstrip("@")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", name):
        return f"https://t.me/{name}"
    return None


def parse_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("T", " ")[:26])
    except Exception:
        return None


def fmt_dt(v):
    d = parse_dt(v)
    return (d + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M") if d else "—"   # Toshkent vaqti


def ago(v):
    d = parse_dt(v)
    if not d:
        return ""
    delta = _utcnow() - d
    if delta.days >= 1:
        return f"{delta.days} kun"
    h = delta.seconds // 3600
    if h >= 1:
        return f"{h} soat"
    return f"{max(1, delta.seconds // 60)} daqiqa"


def _ts_param(dt):
    return dt if USE_PG else dt.strftime("%Y-%m-%d %H:%M:%S")


_profile_seen = {}


def touch_profile(tg_user):
    """Foydalanuvchi ismi/username ni bazaga saqlaydi (o'zgargandagina)."""
    try:
        if not tg_user:
            return
        name = (getattr(tg_user, "full_name", None) or "")[:100]
        uname = getattr(tg_user, "username", None) or None
        key = (name, uname)
        if _profile_seen.get(tg_user.id) == key:
            return
        if len(_profile_seen) > 50000:
            _profile_seen.clear()
        _profile_seen[tg_user.id] = key
        spawn(_save_profile(tg_user.id, name, uname))
    except Exception:
        pass


async def _save_profile(uid, name, uname):
    try:
        n = await db_exec("UPDATE users SET full_name=?, username=? WHERE user_id=?", (name, uname, uid))
        if not n:
            _profile_seen.pop(uid, None)
    except Exception:
        _profile_seen.pop(uid, None)
        logger.exception("profil saqlash xato")


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
        return None
    return is_sub


def cache_peek_sub(user_id):
    """Muddati o'tgan bo'lsa ham oxirgi ma'lum qiymat (yoki None)."""
    entry = _sub_cache.get(user_id)
    return entry[0] if entry else None


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
                logger.error("Telegram xatosi [%s]: %s", action_desc, e)
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
          "promocodes", "promo_uses", "bonus_claims", "payment_channels",
          "campaigns", "campaign_claims", "transfers"]


class TxAbort(Exception):
    pass


def _pg_dsn():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    hostport = hostport.replace("-pooler.", ".")   # Neon pooler (pgbouncer) o'rniga to'g'ridan-to'g'ri ulanish
    return urlunsplit((parts.scheme, userinfo + sep + hostport, parts.path, urlencode(q), parts.fragment))


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
    ("full_name", "TEXT"),
    ("username", "TEXT"),
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
        'CREATE TABLE IF NOT EXISTS bot_lock (id INTEGER PRIMARY KEY, owner TEXT, expires_at {REAL} DEFAULT 0, info TEXT, updated_at {REAL})',
        'CREATE TABLE IF NOT EXISTS campaigns (id {PK}, link TEXT, amount {REAL}, body TEXT, created_at {TS})',
        "CREATE TABLE IF NOT EXISTS campaign_claims (id {PK}, campaign_id INTEGER, user_id {BIG}, status TEXT DEFAULT 'pending', strikes INTEGER DEFAULT 0, created_at {TS}, updated_at {TS}, UNIQUE(campaign_id, user_id))",
        'CREATE TABLE IF NOT EXISTS transfers (id {PK}, from_id {BIG}, to_id {BIG}, amount {REAL}, received {REAL}, created_at {TS})',
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
    await db_exec('INSERT INTO bot_lock (id, owner, expires_at) VALUES (1, NULL, 0) ON CONFLICT (id) DO NOTHING')


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



# ---------- v3: qidiruv, top referallar, statistika ----------

async def get_user_profile(uid):
    r = await db_one(
        'SELECT user_id, balance, referred_by, banned, registered, phone, joined_at, full_name, username '
        'FROM users WHERE user_id=?', (uid,))
    if not r:
        return None
    return {"user_id": r[0], "balance": float(r[1] or 0.0), "referred_by": r[2], "banned": int(r[3] or 0),
            "registered": int(r[4]) if r[4] is not None else 1, "phone": r[5], "joined_at": r[6],
            "full_name": r[7], "username": r[8]}


async def find_user_id_by_username(name):
    r = await db_one('SELECT user_id FROM users WHERE LOWER(username)=?', ((name or "").lstrip("@").lower(),))
    return r[0] if r else None


async def find_user_id_by_phone(phone):
    r = await db_one('SELECT user_id FROM users WHERE phone=?', (phone,))
    return r[0] if r else None


async def count_refs(uid):
    """(tasdiqlangan, jami kelgan)"""
    r = await db_one(
        'SELECT COALESCE(SUM(CASE WHEN referral_paid=1 AND registered=1 THEN 1 ELSE 0 END), 0), COUNT(*) '
        'FROM users WHERE referred_by=?', (uid,))
    return (int(r[0]), int(r[1])) if r else (0, 0)


async def list_referrals(uid, limit, offset):
    return await db_all(
        'SELECT user_id, full_name, username, registered, referral_paid, joined_at FROM users '
        'WHERE referred_by=? ORDER BY joined_at DESC, user_id LIMIT ? OFFSET ?', (uid, limit, offset))


async def top_referrers(limit=20):
    return await db_all(
        'SELECT r.referred_by, COUNT(*) AS c, u.full_name, u.username, u.balance FROM users r '
        'LEFT JOIN users u ON u.user_id = r.referred_by '
        'WHERE r.referred_by IS NOT NULL AND r.referral_paid=1 AND r.registered=1 '
        'GROUP BY r.referred_by, u.full_name, u.username, u.balance '
        'ORDER BY c DESC, r.referred_by LIMIT ?', (limit,))


async def withdraw_stats_user(uid):
    rows = await db_all('SELECT status, COUNT(*), COALESCE(SUM(amount), 0.0) FROM withdrawals WHERE user_id=? GROUP BY status', (uid,))
    return {r[0]: (int(r[1]), float(r[2])) for r in rows}


# ---------- v6: referal topshiriqlari va do'stga o'tkazma ----------

async def create_campaign(link, amount, body):
    return await db_insert('INSERT INTO campaigns (link, amount, body) VALUES (?, ?, ?)', (link, float(amount), body))


async def get_campaign(cid):
    return await db_one('SELECT id, link, amount, body FROM campaigns WHERE id=?', (cid,))


async def submit_claim(cid, uid):
    """('new'|'pending'|'approved'|'blocked', claim_id, strikes)"""
    n = await db_exec(
        "INSERT INTO campaign_claims (campaign_id, user_id, status, strikes) VALUES (?, ?, 'pending', 0) "
        "ON CONFLICT (campaign_id, user_id) DO NOTHING", (cid, uid))
    row = await db_one('SELECT id, status, strikes FROM campaign_claims WHERE campaign_id=? AND user_id=?', (cid, uid))
    claim_id, status, strikes = row[0], row[1], int(row[2] or 0)
    if n:
        return "new", claim_id, 0
    if status == "approved":
        return "approved", claim_id, strikes
    if status == "pending":
        return "pending", claim_id, strikes
    if strikes >= 2:
        return "blocked", claim_id, strikes
    n2 = await db_exec("UPDATE campaign_claims SET status='pending', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='rejected'", (claim_id,))
    return ("new" if n2 else "pending"), claim_id, strikes


async def get_claim_full(claim_id):
    return await db_one(
        'SELECT c.id, c.campaign_id, c.user_id, c.status, c.strikes, p.amount, p.link '
        'FROM campaign_claims c JOIN campaigns p ON p.id = c.campaign_id WHERE c.id=?', (claim_id,))


async def approve_claim(claim_id, uid, amount):
    """Tasdiqlash + pul qo'shish BITTA tranzaksiyada (ikki marta to'lanmaydi)."""
    res = await db_tx([
        ("exec", "UPDATE campaign_claims SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", (claim_id,)),
        ("exec", 'UPDATE users SET balance = balance + ? WHERE user_id=?', (float(amount), uid)),
    ], first_must_change=True)
    return res is not None


async def reject_claim(claim_id):
    n = await db_exec("UPDATE campaign_claims SET status='rejected', strikes = strikes + 1, updated_at=CURRENT_TIMESTAMP "
                      "WHERE id=? AND status='pending'", (claim_id,))
    if not n:
        return None
    r = await db_one('SELECT strikes FROM campaign_claims WHERE id=?', (claim_id,))
    return int(r[0]) if r else None


async def do_transfer(from_id, to_id, amount):
    """Yuboruvchidan to'liq summa yechiladi, oluvchiga yarmi tushadi. (transfer_id yoki None, oluvchiga tushgan)"""
    received = float(int(amount // 2))
    res = await db_tx([
        ("exec", 'UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?', (float(amount), from_id, float(amount))),
        ("exec", 'UPDATE users SET balance = balance + ? WHERE user_id=?', (received, to_id)),
        ("insert", 'INSERT INTO transfers (from_id, to_id, amount, received) VALUES (?, ?, ?, ?)',
         (from_id, to_id, float(amount), received)),
    ], first_must_change=True)
    return (res[2] if res else None), received


# ---------- v7: kutilayotgan to'lovlar va shaxsiy ma'lumot ----------

async def list_pending_withdrawals(limit=15):
    return await db_all(
        "SELECT w.id, w.user_id, w.amount, w.card_type, w.card_number, w.created_at, u.full_name, u.username "
        "FROM withdrawals w LEFT JOIN users u ON u.user_id = w.user_id "
        "WHERE w.status='pending' ORDER BY w.id LIMIT ?", (limit,))


async def count_pending_withdrawals():
    r = await db_one("SELECT COUNT(*), COALESCE(SUM(amount), 0.0) FROM withdrawals WHERE status='pending'")
    return (int(r[0]), float(r[1])) if r else (0, 0.0)


async def get_my_stats(uid):
    p = await get_user_profile(uid)
    if not p:
        return None
    paid, total = await count_refs(uid)
    b = await db_one('SELECT COALESCE(SUM(amount), 0.0), COUNT(*) FROM bonus_claims WHERE user_id=?', (uid,))
    pr = await db_one('SELECT COALESCE(SUM(p.amount), 0.0), COUNT(*) FROM promo_uses u '
                      'JOIN promocodes p ON p.id = u.promo_id WHERE u.user_id=?', (uid,))
    cp = await db_one("SELECT COALESCE(SUM(p.amount), 0.0), COUNT(*) FROM campaign_claims c "
                      "JOIN campaigns p ON p.id = c.campaign_id WHERE c.user_id=? AND c.status='approved'", (uid,))
    ws = await withdraw_stats_user(uid)
    sent = await db_one('SELECT COALESCE(SUM(amount), 0.0), COUNT(*) FROM transfers WHERE from_id=?', (uid,))
    got = await db_one('SELECT COALESCE(SUM(received), 0.0), COUNT(*) FROM transfers WHERE to_id=?', (uid,))
    rank = None
    if paid > 0:
        r = await db_one('SELECT COUNT(*) FROM (SELECT referred_by FROM users WHERE referred_by IS NOT NULL '
                         'AND referral_paid=1 AND registered=1 GROUP BY referred_by HAVING COUNT(*) > ?) t', (paid,))
        rank = int(r[0]) + 1
    last = await last_bonus_claim(uid)
    try:
        interval = int(float(await get_setting("bonus_interval", "86400")))
    except Exception:
        interval = 86400
    return {"p": p, "paid": paid, "total": total, "bonus": (float(b[0]), int(b[1])), "promo": (float(pr[0]), int(pr[1])),
            "camp": (float(cp[0]), int(cp[1])), "ws": ws, "sent": (float(sent[0]), int(sent[1])),
            "got": (float(got[0]), int(got[1])), "rank": rank, "last_bonus": last, "interval": interval}


async def get_broadcast_ids():
    """Kunlik xabar uchun: bloklanmagan va ro'yxatdan o'tgan foydalanuvchilar."""
    rows = await db_all('SELECT user_id FROM users WHERE banned=0 AND registered=1')
    return [r[0] for r in rows]


async def get_full_stats():
    day0 = (_utcnow() + timedelta(hours=5)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=5)
    week0 = day0 - timedelta(days=6)
    a = await db_one(
        'SELECT COUNT(*), COALESCE(SUM(balance), 0.0), COALESCE(SUM(CASE WHEN registered=1 THEN 1 ELSE 0 END), 0), '
        'COALESCE(SUM(CASE WHEN banned=1 THEN 1 ELSE 0 END), 0) FROM users')
    t = await db_one('SELECT COUNT(*) FROM users WHERE joined_at >= ?', (_ts_param(day0),))
    w = await db_one('SELECT COUNT(*) FROM users WHERE joined_at >= ?', (_ts_param(week0),))
    refs = await db_one('SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL AND referral_paid=1 AND registered=1')
    wd = await db_all('SELECT status, COUNT(*), COALESCE(SUM(amount), 0.0) FROM withdrawals GROUP BY status')
    return {"users": int(a[0]), "balance": float(a[1]), "registered": int(a[2]), "banned": int(a[3]),
            "today": int(t[0]), "week": int(w[0]), "refs": int(refs[0]),
            "wd": {r[0]: (int(r[1]), float(r[2])) for r in wd}}


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
        if cache_peek_sub(user_id) is True:
            # obuna bo'lgan edi: darhol javob beramiz, tekshiruv orqa fonda yangilanadi (tezlik uchun)
            spawn(_refresh_sub(user_id, context))
            return True
    channels = await get_channels()
    if not channels:
        result = True
    else:
        results = await asyncio.gather(*[check_one_channel(context, cid, user_id) for cid, _ in channels])
        result = all(results)
    cache_set_sub(user_id, result)
    return result


_sub_refreshing = set()


async def _refresh_sub(user_id, context):
    if user_id in _sub_refreshing:
        return
    _sub_refreshing.add(user_id)
    try:
        await is_subscribed(user_id, context, use_cache=False)
    except Exception:
        logger.exception("obuna yangilash xato")
    finally:
        _sub_refreshing.discard(user_id)


def main_keyboard(user_id):
    buttons = [
        [KeyboardButton("💰 Pul ishlash"), KeyboardButton("👤 Balans")],
        [KeyboardButton("💸 Pul yechish"), KeyboardButton("🎁 Bonus")],
        [KeyboardButton("🎟 Promokod"), KeyboardButton("💳 To'lov kanali")],
        [KeyboardButton("☎️ Murojaat"), KeyboardButton(RULES_BUTTON)],
        [KeyboardButton(TRANSFER_BUTTON)],
    ]
    if user_id == SUPER_ADMIN:
        buttons.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_TEXT)]], resize_keyboard=True)


def subscription_keyboard(channels):
    btns = [[InlineKeyboardButton(f"📢 {name}", url=channel_url(cid))] for cid, name in channels if channel_url(cid)]
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
    rows.insert(1, [
        InlineKeyboardButton("🏆 Top referallar", callback_data="admin_top"),
        InlineKeyboardButton("🔎 Foydalanuvchi qidirish", callback_data="admin_find"),
    ])
    rows.insert(2, [InlineKeyboardButton("⏳ Kutilayotgan to'lovlar", callback_data="admin_pending")])
    if user_id == SUPER_ADMIN:   # bloklash va bildirishnoma tugmalari FAQAT egada ko'rinadi
        rows.insert(len(rows) - 2, [
            InlineKeyboardButton("🚫 Foydalanuvchini bloklash", callback_data="admin_ban_user"),
            InlineKeyboardButton("✅ Blokdan chiqarish", callback_data="admin_unban_user"),
        ])
        rows.insert(len(rows) - 2, [
            InlineKeyboardButton("🔔 Bildirishnomalar", callback_data="admin_toggle_events"),
            InlineKeyboardButton("📅 Kunlik xabar", callback_data="admin_daily"),
        ])
        rows.insert(len(rows) - 2, [InlineKeyboardButton("📨 Referal yuborish", callback_data="admin_camp")])
        rows.insert(len(rows) - 2, [InlineKeyboardButton("✨ Premium emoji", callback_data="admin_emoji")])
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
    touch_profile(tg_user)
    try:
        _uu = await get_user_full(user_id)
        _nm = getattr(tg_user, "full_name", "") or ""
        _un = getattr(tg_user, "username", None)
        await notify_event(f"🆕 Yangi foydalanuvchi ro'yxatdan o'tdi\n👤 {_nm} {'@' + _un if _un else ''}\n🆔 {user_id}\n"
                           f"🔗 Taklif qilgan: {(_uu or {}).get('referred_by') or 'yo`q'}")
    except Exception:
        pass
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
    await notify_event(f"💰 Referal puli: +{fmt_money(price)} so'm → ID {ref} (yangi do'st: {user_id})")
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
                await notify_event(f"⚠️ Takroriy telefon raqam: ID {user_id} raqami boshqa akkauntda bor (+{digits[-4:]})")
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
        touch_profile(update.effective_user)

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
    # Bitta xabar, hech qanday kutish yo'q: havola darhol yuboriladi
    bot_username = context.application.bot_data.get("username") or await get_bot_username(context)
    if not bot_username:
        return
    ref_price = await get_ref_price()
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    text = (
        "🚀 *Sizning taklif havolangiz:*\n"
        f"`{ref_link}`\n\n"
        f"💵 Har bir do'st: *{ref_price:,.0f} so'm*\n{DIVIDER}\n📤 Ulashing!"
    )
    await api_call(lambda: update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=share_keyboard(ref_link)), action_desc="earn")


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
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    paid, _total = await count_refs(user_id)
    ws = await withdraw_stats_user(user_id)
    got = ws.get("approved", (0, 0.0))[1]
    await api_call(lambda: update.message.reply_text(
        f"💳 *Balans:* *{fmt_money(balance)} so'm*\n{DIVIDER}\n"
        f"👥 Takliflaringiz: *{paid}* ta\n💸 Yechib olgansiz: *{fmt_money(got)} so'm*",
        parse_mode=ParseMode.MARKDOWN), action_desc="bal")


def format_stats(s):
    wd = s["wd"]
    pend = wd.get("pending", (0, 0.0))
    appr = wd.get("approved", (0, 0.0))
    rej = wd.get("rejected", (0, 0.0))
    return (
        f"📊 <b>Statistika</b>\n{DIVIDER}\n"
        f"👤 Jami foydalanuvchi: <b>{s['users']}</b>\n"
        f"✅ Ro'yxatdan o'tgan: <b>{s['registered']}</b>\n"
        f"🆕 Bugun: <b>{s['today']}</b> · 7 kun: <b>{s['week']}</b>\n"
        f"🚫 Bloklangan: <b>{s['banned']}</b>\n"
        f"👥 Tasdiqlangan takliflar: <b>{s['refs']}</b>\n"
        f"💰 Jami balans: <b>{fmt_money(s['balance'])} so'm</b>\n{DIVIDER}\n"
        f"💸 <b>Yechish so'rovlari</b>\n"
        f"⏳ Kutilmoqda: <b>{pend[0]}</b> ta ({fmt_money(pend[1])} so'm)\n"
        f"✅ To'langan: <b>{appr[0]}</b> ta ({fmt_money(appr[1])} so'm)\n"
        f"❌ Rad etilgan: <b>{rej[0]}</b> ta"
    )


async def handle_stats(update):
    s = await get_full_stats()
    await api_call(lambda: update.message.reply_text(format_stats(s), parse_mode=ParseMode.HTML), action_desc="stats")


async def handle_bonus(update, context, user_id):
    async with user_lock(user_id):     # ikki marta bosib, ikki marta bonus olishdan himoya
        await _handle_bonus_inner(update, context, user_id)


async def _handle_bonus_inner(update, context, user_id):
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
        await notify_event(f"🎟 Promokod ishlatildi: {code} (+{amount:,.0f} so'm) — ID {user_id}")
        await api_call(lambda: update.message.reply_text(
            f"🎉 *+{amount:,.0f} so'm!*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="pr_ok")
    return True


async def handle_payment_channel(update, context, user_id):
    pc = await get_payment_channel()
    if pc:
        cid, name, desc = pc
    else:
        cid, name, desc = OTZIF_CHANNEL, "To'lovlar kanali", "Barcha to'lovlar shu kanalda ochiq e'lon qilinadi."
    url = channel_url(cid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 O'tish", url=url)]]) if url else None
    await api_call(lambda: update.message.reply_text(
        f"💳 <b>{html.escape(str(name))}</b>\n{DIVIDER}\n{html.escape(str(desc))}\n\n👉 {html.escape(str(cid))}",
        parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="pc_s")


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


def _wd_min(method):
    return MIN_WITHDRAW_PHONE if method == "phone" else MIN_WITHDRAW_CARD


def parse_amount(text):
    d = "".join(ch for ch in (text or "") if ch.isdigit())
    if not d or len(d) > 10:
        return None
    return float(int(d))


def amount_keyboard(balance):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💯 Hammasi — {fmt_money(balance)} so'm", callback_data="wd_amt:all")],
        [InlineKeyboardButton("‹ Bekor qilish", callback_data="wd_cancel")],
    ])


def amount_prompt(balance, method):
    mn = _wd_min(method)
    title = "📱 Telefon raqamga" if method == "phone" else f"💳 {method}"
    return (f"{title}\n{DIVIDER}\n💰 *Qancha yechmoqchisiz?*\n\n"
            f"💳 Balansingiz: *{fmt_money(balance)} so'm*\n📉 Minimal: *{fmt_money(mn)} so'm*\n\n"
            f"Summani raqam bilan yozing (masalan: `{int(mn)}`) yoki pastdagi tugmani bosing 👇")


def _wd_clear(context):
    context.user_data["state"] = None
    for k in ("wd_method", "wd_amount", "withdraw_card_type"):
        context.user_data.pop(k, None)


async def after_amount_chosen(reply, context, method, amount):
    context.user_data["wd_amount"] = amount
    if method == "phone":
        context.user_data["state"] = "withdraw_phone"
        text = (f"📱 *Telefon raqamga yechish*\n💵 Summa: *{fmt_money(amount)} so'm*\n\n"
                "Raqamingizni kiriting (masalan: `901234567` yoki `+998901234567`):")
    else:
        context.user_data["state"] = "withdraw_card_number"
        context.user_data["withdraw_card_type"] = method
        text = f"💳 *{method}*\n💵 Summa: *{fmt_money(amount)} so'm*\n\nKarta raqamini kiriting (16 raqam):"
    await api_call(lambda: reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="wd_after")


async def withdraw_type_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        if user_id != SUPER_ADMIN and await is_banned(user_id):
            await api_call(lambda: query.answer("🚫 Bloklangansiz", show_alert=True), action_desc="wt_ban")
            return
        data = query.data
        if data == "wd_cancel":
            _wd_clear(context)
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
            context.user_data["wd_method"] = "phone"
            context.user_data["state"] = "withdraw_amount"
            await api_call(lambda: query.message.edit_text(
                amount_prompt(balance, "phone"), parse_mode=ParseMode.MARKDOWN, reply_markup=amount_keyboard(balance)), action_desc="wt_pp")
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
            context.user_data["wd_method"] = card_type
            context.user_data["withdraw_card_type"] = card_type
            context.user_data["state"] = "withdraw_amount"
            await api_call(lambda: query.message.edit_text(
                amount_prompt(balance, card_type), parse_mode=ParseMode.MARKDOWN, reply_markup=amount_keyboard(balance)), action_desc="wt_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="wt_kb")
            return

        if data == "wd_amt:all":
            method = context.user_data.get("wd_method")
            if context.user_data.get("state") != "withdraw_amount" or not method:
                await api_call(lambda: query.answer("⏳ Qaytadan boshlang: 💸 Pul yechish", show_alert=True), action_desc="wt_x")
                return
            if balance < _wd_min(method):
                await api_call(lambda: query.answer(f"🚫 Minimal {fmt_money(_wd_min(method))} so'm", show_alert=True), action_desc="wt_l4")
                return
            await api_call(lambda: query.answer(), action_desc="wt_a7")
            await api_call(lambda: query.message.edit_text(
                f"💵 Summa: *{fmt_money(balance)} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="wt_am")
            await after_amount_chosen(query.message.chat.send_message, context, method, balance)
            return
        await api_call(lambda: query.answer(), action_desc="wt_a6")
    except Exception:
        logger.exception("wt xato")


async def finalize_withdrawal(update, context, user_id, ctype, cnum, min_amount):
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    amount = context.user_data.get("wd_amount") or balance
    _wd_clear(context)
    if amount < min_amount or balance < amount:
        await api_call(lambda: update.message.reply_text("🚫 Yetarli emas.", reply_markup=main_keyboard(user_id)), action_desc="ws_l")
        return
    wid = await create_withdrawal_atomic(user_id, amount, ctype, cnum)
    if wid is None:
        await api_call(lambda: update.message.reply_text("🚫 Yetarli emas.", reply_markup=main_keyboard(user_id)), action_desc="ws_l2")
        return
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
    if state not in ("withdraw_card_number", "withdraw_phone", "withdraw_amount"):
        return False
    if text == CANCEL_TEXT:
        _wd_clear(context)
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="ws_c")
        return True

    if state == "withdraw_amount":
        method = context.user_data.get("wd_method")
        if not method:
            _wd_clear(context)
            return True
        amt = parse_amount(text)
        user = await get_user(user_id)
        balance = user[1] if user else 0.0
        mn = _wd_min(method)
        if amt is None:
            msg = "❌ Faqat raqam yozing. Masalan: " + str(int(mn))
        elif amt < mn:
            msg = f"❌ Minimal summa: {fmt_money(mn)} so'm"
        elif amt > balance:
            msg = f"❌ Balansingizda {fmt_money(balance)} so'm bor. Shundan oshmagan summa yozing."
        else:
            await after_amount_chosen(update.message.reply_text, context, method, amt)
            return True
        await api_call(lambda: update.message.reply_text(msg), action_desc="ws_ai")
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
            await notify_event(f"✅ To'lov tasdiqlandi #{wid}: {fmt_money(amount)} so'm → ID {target} (admin {query.from_user.id})")
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
    await notify_event(f"❌ To'lov rad etildi #{wid}: {fmt_money(amount)} so'm qaytarildi → ID {target}. Sabab: {text[:100]}")
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
            s = await get_full_stats()
            await api_call(lambda: query.message.edit_text(format_stats(s), parse_mode=ParseMode.HTML, reply_markup=back_keyboard()), action_desc="as")
            return
        if data == "admin_broadcast":
            context.user_data["state"] = "broadcast"
            await api_call(lambda: query.message.edit_text("✍️ Xabar:", parse_mode=ParseMode.MARKDOWN), action_desc="bc_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="bc_kb")
            return
        if await admin_extra_callback(query, context, user_id, data):
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
#  KUNLIK XABAR (har kuni 08:00 Toshkent vaqti bilan)
# ========================================================

DEFAULT_DAILY_TEXT = (
    "🌞 Xayrli tong!\n\n"
    "🤖 Bu bot orqali pul ishlashingiz mumkin:\n\n"
    "👥 Do'stlaringizni taklif qiling — har bir do'st uchun {ref_price} so'm\n"
    "🎁 Har kuni bonus oling\n"
    "🎟 Promokodlar orqali qo'shimcha pul\n"
    "💸 Pulni telefon raqamingizga ({min_phone} so'mdan) yoki kartangizga ({min_card} so'mdan) yechib oling\n"
    "📢 Barcha to'lovlar {otzif} kanalida ochiq e'lon qilinadi\n\n"
    "👇 Hoziroq boshlang: menyudan «💰 Pul ishlash» tugmasini bosing!"
)


async def daily_text_final():
    t = (await get_setting("daily_text", None)) or DEFAULT_DAILY_TEXT
    price = await get_ref_price()
    return (t.replace("{ref_price}", fmt_money(price)).replace("{min_phone}", fmt_money(MIN_WITHDRAW_PHONE))
             .replace("{min_card}", fmt_money(MIN_WITHDRAW_CARD)).replace("{otzif}", OTZIF_CHANNEL)
             .replace("{bot}", BOT_TAG))


async def daily_loop(bot):
    await asyncio.sleep(20)
    while True:
        try:
            now_tk = _utcnow() + timedelta(hours=5)
            today = now_tk.strftime("%Y-%m-%d")
            if 8 <= now_tk.hour < 10 and (await get_setting("daily_enabled", "1")) != "0" \
                    and (await get_setting("daily_last", "")) != today:
                await set_setting("daily_last", today)
                ids = await get_broadcast_ids()
                text = await daily_text_final()
                notify_owner(f"📅 Kunlik xabar yuborilmoqda ({len(ids)} ta foydalanuvchi)...", key="daily-" + today, ttl=1)
                await run_broadcast(bot, SUPER_ADMIN, ids, text, markdown=False)
        except Exception:
            logger.exception("kunlik xabar xato")
        await asyncio.sleep(60)


async def daily_panel():
    en = (await get_setting("daily_enabled", "1")) != "0"
    last = (await get_setting("daily_last", "")) or "—"
    body = await daily_text_final()
    text = (f"📅 Kunlik xabar\n{DIVIDER}\nHolat: {'✅ yoqilgan' if en else '❌ o‘chirilgan'}\n"
            f"Har kuni 08:00 (Toshkent vaqti) barcha ro'yxatdan o'tgan foydalanuvchilarga yuboriladi.\n"
            f"Oxirgi yuborilgan sana: {last}\n\nHozirgi matn:\n{DIVIDER}\n{body[:1500]}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔕 O'chirish" if en else "🔔 Yoqish", callback_data="admin_daily_toggle")],
        [InlineKeyboardButton("✏️ Matnni o'zgartirish", callback_data="admin_daily_edit")],
        [InlineKeyboardButton("📤 Menga sinov yuborish", callback_data="admin_daily_test")],
        [InlineKeyboardButton("♻️ Standart matn", callback_data="admin_daily_reset")],
        [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
    ])
    return text, kb


async def admin_daily_callback(query, context, user_id, data):
    if user_id != SUPER_ADMIN:
        await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="dl_d")
        return True
    if data == "admin_daily_toggle":
        cur = await get_setting("daily_enabled", "1")
        await set_setting("daily_enabled", "0" if cur != "0" else "1")
    elif data == "admin_daily_reset":
        await set_setting("daily_text", "")
    elif data == "admin_daily_edit":
        context.user_data["state"] = "daily_text"
        await api_call(lambda: query.message.edit_text(
            "✏️ Yangi kunlik xabar matnini yuboring.\n\nQuyidagi so'zlar avtomatik almashtiriladi:\n"
            "{ref_price} — referal puli\n{min_phone} — telefonga minimal\n{min_card} — kartaga minimal\n"
            "{otzif} — otzif kanali\n{bot} — bot nomi"), action_desc="dl_e")
        await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="dl_k")
        return True
    elif data == "admin_daily_test":
        txt = await daily_text_final()
        await api_call(lambda: context.bot.send_message(chat_id=SUPER_ADMIN, text=txt), action_desc="dl_t")
        await api_call(lambda: query.answer("📤 Yuborildi"), action_desc="dl_ta")
        return True
    text, kb = await daily_panel()
    await api_call(lambda: query.message.edit_text(text, reply_markup=kb), action_desc="dl_p")
    return True


# ========================================================
#  REFERAL / HAVOLA YUBORISH (ega: havola + bonus + matn -> hammaga)
# ========================================================

def normalize_link(text):
    t = (text or "").strip()
    if not t or " " in t or "\n" in t:
        return None
    if t.startswith("@"):
        t = "https://t.me/" + t[1:]
    elif t.startswith(("t.me/", "telegram.me/")):
        t = "https://" + t
    if not t.startswith(("https://", "http://")) or len(t) > 500 or "." not in t:
        return None
    return t


def camp_final_text(camp):
    amt = fmt_money(camp["amount"])
    t = camp["text"].replace("{amount}", amt)
    if "{amount}" not in camp["text"]:
        t += f"\n\n🎁 Bonus: {amt} so'm"
    return t


def camp_markup(link, cid=None):
    rows = [[InlineKeyboardButton("🔗 Havolaga o'tish", url=link)]]
    if cid is not None:
        rows.append([InlineKeyboardButton("✅ Ro'yxatdan o'ttim", callback_data=f"camp_done:{cid}")])
    return InlineKeyboardMarkup(rows)


async def admin_camp_callback(query, context, user_id, data):
    if user_id != SUPER_ADMIN:
        await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="cp_d")
        return True
    if data == "admin_camp":
        context.user_data["camp"] = {}
        context.user_data["state"] = "camp_link"
        await api_call(lambda: query.message.edit_text(
            "📨 Referal yuborish\n\n1/3 — Havolani yuboring\n(masalan: https://t.me/botnomi?start=123)"), action_desc="cp_1")
        await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="cp_k")
        return True
    if data == "admin_camp_cancel":
        context.user_data.pop("camp", None)
        context.user_data["state"] = None
        await api_call(lambda: query.answer(), action_desc="cp_ca")
        await api_call(lambda: query.message.edit_text("🚫 Bekor qilindi."), action_desc="cp_cx")
        return True
    if data == "admin_camp_send":
        camp = context.user_data.get("camp") or {}
        if not all(k in camp for k in ("link", "amount", "text")):
            await api_call(lambda: query.answer("⏳ Qaytadan boshlang: 📨 Referal yuborish", show_alert=True), action_desc="cp_nf")
            return True
        context.user_data.pop("camp", None)
        cid = await create_campaign(camp["link"], camp["amount"], camp["text"])
        ids = await get_broadcast_ids()
        await api_call(lambda: query.answer(), action_desc="cp_sa")
        await api_call(lambda: query.message.edit_text(f"📤 Yuborish boshlandi ({len(ids)} ta). Tugagach hisobot beraman."), action_desc="cp_s")
        spawn(run_broadcast(context.bot, SUPER_ADMIN, ids, camp_final_text(camp), markdown=False,
                            reply_markup=camp_markup(camp["link"], cid)))
        notify_owner(f"📨 Referal xabari yuborilmoqda: {camp['link']} | bonus {fmt_money(camp['amount'])} so'm | {len(ids)} ta",
                     key="camp-" + str(time.time()), ttl=1)
        return True
    return False


async def handle_camp_text(update, context, user_id, state, text):
    if user_id != SUPER_ADMIN:
        context.user_data["state"] = None
        return True
    camp = context.user_data.setdefault("camp", {})
    if state == "camp_link":
        link = normalize_link(text)
        if not link:
            await api_call(lambda: update.message.reply_text(
                "❌ Havola noto'g'ri. https://... yoki t.me/... ko'rinishida yuboring."), action_desc="cp_le")
            return True
        camp["link"] = link
        context.user_data["state"] = "camp_amount"
        await api_call(lambda: update.message.reply_text(
            "2/3 — Bonus miqdorini yuboring (so'mda, masalan: 300):", reply_markup=cancel_keyboard()), action_desc="cp_2")
        return True
    if state == "camp_amount":
        amt = parse_amount(text)
        if not amt:
            await api_call(lambda: update.message.reply_text("❌ Faqat raqam yuboring. Masalan: 300"), action_desc="cp_ae")
            return True
        camp["amount"] = amt
        context.user_data["state"] = "camp_text"
        await api_call(lambda: update.message.reply_text(
            "3/3 — Xabar matnini yozing.\n\n{amount} deb yozsangiz, o'rniga miqdor qo'yiladi. "
            "Yozmasangiz, oxiriga «🎁 Bonus: ... so'm» qatori avtomatik qo'shiladi.", reply_markup=cancel_keyboard()), action_desc="cp_3")
        return True
    # camp_text
    if len(text) > 3500:
        await api_call(lambda: update.message.reply_text("❌ Matn juda uzun (3500 belgigacha)."), action_desc="cp_te")
        return True
    camp["text"] = text
    context.user_data["state"] = None
    ids = await get_broadcast_ids()
    await api_call(lambda: update.message.reply_text("👀 Oldindan ko'rish (foydalanuvchilar shunday ko'radi):",
                                                     reply_markup=main_keyboard(user_id)), action_desc="cp_p0")
    await api_call(lambda: update.message.reply_text(camp_final_text(camp), reply_markup=camp_markup(camp["link"], 0)), action_desc="cp_p1")
    await api_call(lambda: update.message.reply_text(
        f"Yuqoridagi xabar {len(ids)} ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Yuborish ({len(ids)} ta)", callback_data="admin_camp_send")],
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="admin_camp_cancel")],
        ])), action_desc="cp_p2")
    return True


# ========================================================
#  REFERAL TOPSHIRIG'I: "Ro'yxatdan o'ttim" -> ega tekshiradi
# ========================================================

async def camp_user_callback(update, context):
    try:
        query = update.callback_query
        uid = query.from_user.id
        if uid != SUPER_ADMIN and await is_banned(uid):
            await api_call(lambda: query.answer("🚫 Bloklangansiz", show_alert=True), action_desc="cu_ban")
            return
        try:
            cid = int(query.data.split(":", 1)[1])
        except ValueError:
            return
        if cid == 0:
            await api_call(lambda: query.answer("👀 Bu oldindan ko'rish", show_alert=True), action_desc="cu_pv")
            return
        st = await get_status(uid)
        if not st or st[1] == 0:
            await api_call(lambda: query.answer("Avval botda ro'yxatdan o'ting: /start", show_alert=True), action_desc="cu_reg")
            return
        camp = await get_campaign(cid)
        if not camp:
            await api_call(lambda: query.answer("❌ Topshiriq topilmadi", show_alert=True), action_desc="cu_nf")
            return
        _id, link, amount, _body = camp
        state, claim_id, strikes = await submit_claim(cid, uid)
        if state == "approved":
            await api_call(lambda: query.answer("✅ Bu topshiriq uchun bonus allaqachon berilgan.", show_alert=True), action_desc="cu_a")
        elif state == "pending":
            await api_call(lambda: query.answer("⏳ So'rovingiz ko'rib chiqilmoqda. Natijani kuting.", show_alert=True), action_desc="cu_p")
        elif state == "blocked":
            await api_call(lambda: query.answer("❌ Bu topshiriq uchun so'rov yuborish yopilgan.", show_alert=True), action_desc="cu_b")
        else:
            await api_call(lambda: query.answer("✅ So'rovingiz yuborildi. Tekshirilgach xabar beramiz.", show_alert=True), action_desc="cu_n")
            await api_call(lambda: query.message.edit_reply_markup(reply_markup=camp_markup(link)), action_desc="cu_rm")
            u = query.from_user
            text = (f"📨 Referal tekshiruvi\n{DIVIDER}\n👤 {u.full_name} {'@' + u.username if u.username else ''}\n🆔 {uid}\n"
                    f"🎁 Bonus: {fmt_money(amount)} so'm\n🔗 {link}\n🔁 Urinish: {strikes + 1}/2\n\n"
                    "Havola orqali ro'yxatdan o'tganini tekshiring.")
            await api_call(lambda: context.bot.send_message(
                chat_id=SUPER_ADMIN, text=text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"camp_ok:{claim_id}"),
                    InlineKeyboardButton("❌ Rad etish", callback_data=f"camp_no:{claim_id}"),
                ]])), action_desc="cu_owner")
    except Exception:
        logger.exception("camp_user xato")


async def camp_decide_callback(update, context):
    try:
        query = update.callback_query
        if query.from_user.id != SUPER_ADMIN:
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="cd_d")
            return
        action, claim_str = query.data.split(":", 1)
        row = await get_claim_full(int(claim_str))
        if not row:
            await api_call(lambda: query.answer("❌ Topilmadi", show_alert=True), action_desc="cd_nf")
            return
        claim_id, cid, target, status, _strikes, amount, link = row
        if status != "pending":
            await api_call(lambda: query.answer("ℹ️ Bu so'rov ko'rib chiqilgan", show_alert=True), action_desc="cd_done")
            return
        old_text = query.message.text or ""
        if action == "camp_ok":
            if not await approve_claim(claim_id, target, amount):
                await api_call(lambda: query.answer("ℹ️ Ko'rib chiqilgan", show_alert=True), action_desc="cd_race")
                return
            await api_call(lambda: query.answer("✅ Tasdiqlandi"), action_desc="cd_ok")
            await api_call(lambda: query.message.edit_text(f"{old_text}\n\n✅ TASDIQLANDI (+{fmt_money(amount)} so'm)"), action_desc="cd_edit")
            await api_call(lambda: context.bot.send_message(
                chat_id=target, text=f"🎉 Tasdiqlandi!\n\n💰 Balansingizga +{fmt_money(amount)} so'm qo'shildi. Rahmat!"), action_desc="cd_user")
        else:
            strikes = await reject_claim(claim_id)
            if strikes is None:
                await api_call(lambda: query.answer("ℹ️ Ko'rib chiqilgan", show_alert=True), action_desc="cd_race2")
                return
            await api_call(lambda: query.answer("❌ Rad etildi"), action_desc="cd_no")
            await api_call(lambda: query.message.edit_text(f"{old_text}\n\n❌ RAD ETILDI ({strikes}/2)"), action_desc="cd_edit2")
            if strikes < 2:
                await api_call(lambda: context.bot.send_message(
                    chat_id=target,
                    text=("❌ Ro'yxatdan o'tganingiz tasdiqlanmadi.\n\n"
                          "Barcha shartlarni to'liq bajarib, havola orqali qayta /start bosing. "
                          "Agar botda «Tekshirish» yoki «Tasdiqlash» tugmasi bo'lsa, shuni bosing.\n\n"
                          "Shartlarni bajargach, pastdagi «✅ Ro'yxatdan o'ttim» tugmasini bosing 👇"),
                    reply_markup=camp_markup(link, cid)), action_desc="cd_user2")
            else:
                await api_call(lambda: context.bot.send_message(
                    chat_id=target,
                    text=("❌ Siz shartlarni bajarmasdan turib 2 marta «Ro'yxatdan o'ttim» tugmasini bosdingiz. "
                          "Iltimos, aldamang!\n\nBu topshiriq uchun tugma endi chiqmaydi. "
                          "Keyingi topshiriqlarda yana ishtirok etishingiz mumkin.")), action_desc="cd_user3")
    except Exception:
        logger.exception("camp_decide xato")


# ========================================================
#  DO'STGA PUL YUBORISH (yuboruvchidan to'liq summa, do'stga yarmi)
# ========================================================

def transfer_info_text():
    return ("💸 Do'stga pul yuborish\n" + DIVIDER + "\n\n"
            "Siz do'stingizga pul yuborishni tanlasangiz, qancha yuborsangiz ham uning YARMI tushadi.\n\n"
            "Masalan: 10 000 so'm yuborsangiz, do'stingizga 5 000 so'm tushadi ♻️\n\n"
            "Rozimisiz?")


async def handle_transfer_start(update, context, user_id):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Roziman", callback_data="tr_agree"),
        InlineKeyboardButton("❌ Rad etish", callback_data="tr_cancel"),
    ]])
    await api_call(lambda: update.message.reply_text(transfer_info_text(), reply_markup=kb), action_desc="tr_start")


def _tr_clear(context):
    context.user_data["state"] = None
    for k in ("transfer_to", "transfer_name", "transfer_amount"):
        context.user_data.pop(k, None)


async def transfer_callback(update, context):
    try:
        query = update.callback_query
        uid = query.from_user.id
        data = query.data
        if uid != SUPER_ADMIN and await is_banned(uid):
            await api_call(lambda: query.answer("🚫 Bloklangansiz", show_alert=True), action_desc="tr_ban")
            return
        if data == "tr_cancel":
            _tr_clear(context)
            await api_call(lambda: query.answer(), action_desc="tr_ca")
            await api_call(lambda: query.message.edit_text("🚫 Bekor qilindi."), action_desc="tr_ce")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=main_keyboard(uid)), action_desc="tr_cm")
            return
        user = await get_user(uid)
        balance = user[1] if user else 0.0
        if data == "tr_agree":
            if balance < MIN_TRANSFER:
                await api_call(lambda: query.answer(
                    f"🚫 Balansingiz yetarli emas. Minimal: {fmt_money(MIN_TRANSFER)} so'm.", show_alert=True), action_desc="tr_low")
                return
            await api_call(lambda: query.answer(), action_desc="tr_ag")
            _tr_clear(context)
            context.user_data["state"] = "transfer_id"
            await api_call(lambda: query.message.edit_text(
                "👤 Do'stingiz ID raqamini kiriting.\n\n"
                "⚠️ Do'stingiz botga /start bosgan (ro'yxatdan o'tgan) bo'lishi kerak, aks holda pul o'tmaydi."), action_desc="tr_id")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="tr_idk")
            return
        if data == "tr_confirm":
            async with user_lock(uid):
                to_id = context.user_data.get("transfer_to")
                amount = context.user_data.get("transfer_amount")
                name = context.user_data.get("transfer_name") or str(to_id)
                if context.user_data.get("state") != "transfer_confirm" or not to_id or not amount:
                    await api_call(lambda: query.answer("⏳ Qaytadan boshlang", show_alert=True), action_desc="tr_x")
                    return
                _tr_clear(context)
                tid, received = await do_transfer(uid, to_id, amount)
            if tid is None:
                await api_call(lambda: query.answer("🚫 Balansingiz yetarli emas", show_alert=True), action_desc="tr_nb")
                return
            await api_call(lambda: query.answer("✅"), action_desc="tr_ok")
            user = await get_user(uid)
            await api_call(lambda: query.message.edit_text(
                f"✅ {name} ga {fmt_money(received)} so'm yuborildi.\n"
                f"({fmt_money(amount)} so'm balansingizdan yechildi)\n💰 Joriy balans: {fmt_money(user[1] if user else 0)} so'm"), action_desc="tr_done")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=main_keyboard(uid)), action_desc="tr_dm")
            u = query.from_user
            await api_call(lambda: context.bot.send_message(
                chat_id=to_id, text=f"💸 {u.full_name} sizga {fmt_money(received)} so'm yubordi!"), action_desc="tr_rcv")
            await notify_event(f"💸 O'tkazma #{tid}: {uid} → {to_id}, {fmt_money(amount)} so'm (tushdi: {fmt_money(received)})")
            return
        await api_call(lambda: query.answer(), action_desc="tr_un")
    except Exception:
        logger.exception("transfer xato")


async def handle_transfer_state(update, context, user_id, text):
    state = context.user_data.get("state")
    if state not in ("transfer_id", "transfer_amount", "transfer_confirm"):
        return False
    if text == CANCEL_TEXT:
        _tr_clear(context)
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="ts_c")
        return True
    if state == "transfer_confirm":
        await api_call(lambda: update.message.reply_text("👆 Yuqoridagi xabardagi tugmalardan birini bosing yoki ❌ Bekor qilish."), action_desc="ts_cf")
        return True
    if state == "transfer_id":
        d = "".join(ch for ch in text if ch.isdigit())
        if not d or len(d) > 15:
            await api_call(lambda: update.message.reply_text("❌ Faqat ID raqamini yozing (masalan: 123456789)."), action_desc="ts_i1")
            return True
        tid = int(d)
        if tid == user_id:
            await api_call(lambda: update.message.reply_text("❌ O'zingizga pul yubora olmaysiz. Do'stingiz ID sini kiriting."), action_desc="ts_i2")
            return True
        p = await get_user_profile(tid)
        if not p or p["banned"] or not p["registered"]:
            await api_call(lambda: update.message.reply_text(
                "❌ Bunday foydalanuvchi topilmadi.\nDo'stingiz botga /start bosib, ro'yxatdan o'tgan bo'lishi kerak. ID ni tekshirib qayta yuboring."), action_desc="ts_i3")
            return True
        name = p["full_name"]
        if not name:
            chat = await api_call(lambda: context.bot.get_chat(tid), action_desc="ts_gc")
            name = getattr(chat, "full_name", None) if chat else None
        name = name or f"ID {tid}"
        context.user_data["transfer_to"] = tid
        context.user_data["transfer_name"] = name
        context.user_data["state"] = "transfer_amount"
        user = await get_user(user_id)
        await api_call(lambda: update.message.reply_text(
            f"👤 Siz {name} (ID: {tid}) ga pul o'tkazyapsiz.\n\n"
            f"💰 Balansingiz: {fmt_money(user[1] if user else 0)} so'm\n"
            f"Summani kiriting (minimal {fmt_money(MIN_TRANSFER)} so'm):", reply_markup=cancel_keyboard()), action_desc="ts_a0")
        return True
    # transfer_amount
    amt = parse_amount(text)
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    if amt is None:
        msg = "❌ Faqat raqam yozing. Masalan: 10000"
    elif amt < MIN_TRANSFER:
        msg = f"❌ Minimal summa: {fmt_money(MIN_TRANSFER)} so'm"
    elif amt > balance:
        msg = f"❌ Balansingizda {fmt_money(balance)} so'm bor. Shundan oshmagan summa yozing."
    else:
        name = context.user_data.get("transfer_name")
        to_id = context.user_data.get("transfer_to")
        context.user_data["transfer_amount"] = amt
        context.user_data["state"] = "transfer_confirm"
        await api_call(lambda: update.message.reply_text(
            f"💸 Tasdiqlang\n{DIVIDER}\n👤 Qabul qiluvchi: {name} (ID: {to_id})\n"
            f"💵 Siz yuborasiz: {fmt_money(amt)} so'm\n♻️ Do'stingizga tushadi: {fmt_money(float(int(amt // 2)))} so'm",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Tasdiqlash", callback_data="tr_confirm"),
                InlineKeyboardButton("❌ Bekor qilish", callback_data="tr_cancel"),
            ]])), action_desc="ts_a1")
        return True
    await api_call(lambda: update.message.reply_text(msg), action_desc="ts_ae")
    return True


# ========================================================
#  BUYRUQLAR: /malumot, /referalim (shaxsiy chatda ham, guruhda ham)
# ========================================================

_group_last = {}


def _group_throttled(uid, gap=5.0):
    now = time.monotonic()
    if now - _group_last.get(uid, -1e9) < gap:
        return True
    if len(_group_last) > 20000:
        _group_last.clear()
    _group_last[uid] = now
    return False


def build_my_info(s):
    p = s["p"]
    ws = s["ws"]
    appr = ws.get("approved", (0, 0.0))
    pend = ws.get("pending", (0, 0.0))
    name = html.escape(p["full_name"]) if p["full_name"] else "—"
    uname = f" @{html.escape(p['username'])}" if p["username"] else ""
    bonus_total = s["bonus"][0] + s["promo"][0] + s["camp"][0]
    nxt = "tayyor ✅"
    if s["last_bonus"]:
        d = parse_dt(s["last_bonus"])
        if d:
            left = (d + timedelta(seconds=s["interval"])) - _utcnow()
            if left.total_seconds() > 0:
                secs = int(left.total_seconds())
                nxt = f"{secs // 3600} soat {(secs % 3600) // 60} daqiqadan keyin"
    lines = [
        f"📋 <b>Sizning ma'lumotlaringiz</b>\n{DIVIDER}",
        f"👤 <b>{name}</b>{uname}",
        f"🆔 <code>{p['user_id']}</code>",
        f"📅 Botga qo'shilgan: <b>{fmt_dt(p['joined_at'])}</b> ({ago(p['joined_at'])} oldin)",
        DIVIDER,
        f"💰 Balans: <b>{fmt_money(p['balance'])} so'm</b>",
        f"👥 Taklif qilganlar: <b>{s['paid']}</b> ta (jami kelgan: {s['total']})",
    ]
    if s["rank"]:
        lines.append(f"🏆 Referal reytingi: <b>{s['rank']}-o'rin</b>")
    lines += [
        f"🎁 Olingan bonuslar: <b>{fmt_money(bonus_total)} so'm</b>",
        f"   • Kunlik bonus: {fmt_money(s['bonus'][0])} so'm ({s['bonus'][1]} marta)",
        f"   • Promokod: {fmt_money(s['promo'][0])} so'm ({s['promo'][1]} marta)",
        f"   • Topshiriq: {fmt_money(s['camp'][0])} so'm ({s['camp'][1]} marta)",
        f"💸 Yechib olgan: <b>{fmt_money(appr[1])} so'm</b> ({appr[0]} marta)",
    ]
    if pend[0]:
        lines.append(f"⏳ Kutilayotgan yechish: {fmt_money(pend[1])} so'm ({pend[0]} ta)")
    if s["sent"][1] or s["got"][1]:
        lines.append(f"🔁 Do'stga yuborgan: {fmt_money(s['sent'][0])} so'm ({s['sent'][1]} marta)")
        lines.append(f"🔁 Do'stdan olgan: {fmt_money(s['got'][0])} so'm ({s['got'][1]} marta)")
    lines.append(f"⏰ Keyingi kunlik bonus: {nxt}")
    if p["referred_by"]:
        lines.append(f"🔗 Sizni taklif qilgan: <code>{p['referred_by']}</code>")
    lines.append("\nℹ️ Referal pullari bonuslar hisobiga kirmagan.")
    return "\n".join(lines)


async def _cmd_gate(update, context):
    """Buyruq uchun umumiy tekshiruvlar. (uid, guruhmi) yoki None."""
    msg = update.message
    if not msg or not update.effective_user or not update.effective_chat:
        return None
    uid = update.effective_user.id
    is_group = update.effective_chat.type != "private"
    if is_group and _group_throttled(uid):
        return None
    if uid != SUPER_ADMIN and await is_banned(uid):
        if not is_group:
            await api_call(lambda: msg.reply_text(BAN_TEXT), action_desc="cmd_ban")
        return None
    st = await get_status(uid)
    if not st or st[1] == 0:
        uname = await get_bot_username(context)
        await api_call(lambda: msg.reply_text(f"Avval botda ro'yxatdan o'ting: https://t.me/{uname}"), action_desc="cmd_reg")
        return None
    if not is_group and not await is_subscribed(uid, context):
        await show_subscription_gate(update, context)
        return None
    return uid, is_group


async def malumot_command(update, context):
    try:
        g = await _cmd_gate(update, context)
        if not g:
            return
        uid, _is_group = g
        s = await get_my_stats(uid)
        if not s:
            return
        text = build_my_info(s)
        await api_call(lambda: update.message.reply_text(text, parse_mode=ParseMode.HTML), action_desc="malumot")
    except Exception:
        logger.exception("/malumot xato")


async def referalim_command(update, context):
    try:
        g = await _cmd_gate(update, context)
        if not g:
            return
        uid, _is_group = g
        bot_username = await get_bot_username(context)
        if not bot_username:
            return
        price = await get_ref_price()
        paid, _total = await count_refs(uid)
        link = f"https://t.me/{bot_username}?start={uid}"
        text = (f"🚀 <b>Sizning taklif havolangiz:</b>\n<code>{link}</code>\n\n"
                f"💵 Har bir do'st: <b>{fmt_money(price)} so'm</b>\n👥 Taklif qilganlaringiz: <b>{paid}</b> ta")
        await api_call(lambda: update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=invite_keyboard(link)), action_desc="referalim")
    except Exception:
        logger.exception("/referalim xato")


# ========================================================
#  ADMIN: KUTILAYOTGAN TO'LOVLAR
# ========================================================

def pending_text(wid, uid, amount, ctype, cnum, created, name, uname):
    label = method_label(ctype, cnum)
    who = html.escape(name) if name else "ism saqlanmagan"
    un = f" @{html.escape(uname)}" if uname else ""
    return (f"💸 <b>Kutilayotgan to'lov #{wid}</b>\n{DIVIDER}\n"
            f"👤 <a href=\"tg://user?id={uid}\">{who}</a>{un}\n🆔 <code>{uid}</code>\n"
            f"💰 <b>{fmt_money(amount)} so'm</b>\n📡 {html.escape(label)}: <code>{html.escape(cnum)}</code>\n"
            f"🕒 {fmt_dt(created)} ({ago(created)} oldin)")


async def admin_pending_callback(query, context):
    total, summa = await count_pending_withdrawals()
    rows = await list_pending_withdrawals(15)
    await api_call(lambda: query.answer(), action_desc="pd_a")
    if not rows:
        await api_call(lambda: query.message.edit_text("✅ Kutilayotgan to'lovlar yo'q.", reply_markup=back_keyboard()), action_desc="pd_none")
        return True
    head = f"⏳ Kutilayotgan to'lovlar: {total} ta ({fmt_money(summa)} so'm)"
    if total > len(rows):
        head += f"\nEng eskilari {len(rows)} tasi pastda. Qolganini ko'rish uchun ularni hal qilgach tugmani qayta bosing."
    await api_call(lambda: query.message.edit_text(head, reply_markup=back_keyboard()), action_desc="pd_head")
    for wid, uid, amount, ctype, cnum, created, name, uname in rows:
        text = pending_text(wid, uid, amount, ctype, cnum, created, name, uname)
        await api_call(lambda t=text, w=wid: query.message.chat.send_message(
            t, parse_mode=ParseMode.HTML, reply_markup=admin_withdraw_keyboard(w)), action_desc="pd_item")
        await asyncio.sleep(0.05)
    return True


# ========================================================
#  PREMIUM EMOJI (Bot API 9.4: bot egasida Telegram Premium bo'lishi shart)
# ========================================================

_emoji_map = {}
_emoji_re = None
_emoji_enabled = False
_emoji_force = False
_emoji_fail = 0
_btn_icon_ok = None
_INLINE_FIELDS = ("url", "callback_data", "switch_inline_query", "switch_inline_query_current_chat",
                  "switch_inline_query_chosen_chat", "copy_text", "web_app", "login_url", "pay", "callback_game")
_REPLY_FIELDS = ("request_contact", "request_location", "request_poll", "request_users", "request_chat", "web_app")


def _norm_emoji(s):
    return s.replace("\ufe0f", "")


def _build_emoji_re(keys):
    if not keys:
        return None
    ks = sorted(keys, key=len, reverse=True)
    return re.compile("|".join("".join(re.escape(ch) + "\ufe0f?" for ch in k) for k in ks))


async def reload_emoji(bot):
    """Saqlangan emoji paketlaridan emoji -> custom_emoji_id xaritasini yuklaydi."""
    global _emoji_map, _emoji_re, _emoji_enabled
    try:
        names = json.loads((await get_setting("emoji_packs", "[]")) or "[]")
    except Exception:
        names = []
    new = {}
    for name in names:
        try:
            ss = await bot.get_sticker_set(name)
        except Exception as e:
            logger.warning("emoji paketi yuklanmadi %s: %s", name, e)
            continue
        for s in ss.stickers:
            cid = getattr(s, "custom_emoji_id", None)
            em = getattr(s, "emoji", None)
            if cid and em:
                k = _norm_emoji(em)
                if k and not k.isascii():
                    new.setdefault(k, cid)
    _emoji_map = new
    _emoji_re = _build_emoji_re(list(new))
    _emoji_enabled = (await get_setting("premium_emoji", "0")) == "1"
    return len(new)


async def reload_emoji_safe(bot):
    try:
        await reload_emoji(bot)
    except Exception:
        logger.exception("emoji yuklash xato")


def _emoji_sub(seg):
    def r(m):
        cid = _emoji_map.get(_norm_emoji(m.group(0)))
        return f'<tg-emoji emoji-id="{cid}">{m.group(0)}</tg-emoji>' if cid else m.group(0)
    return _emoji_re.sub(r, seg)


def apply_html(text):
    parts = re.split(r"(<[^>]*>)", text)
    out, skip = [], 0
    for p in parts:
        if len(p) > 1 and p[0] == "<" and p[-1] == ">":
            low = p.lower()
            if low.startswith(("<code", "<pre", "<tg-emoji")):
                skip += 1
            elif low.startswith(("</code", "</pre", "</tg-emoji")):
                skip = max(0, skip - 1)
            out.append(p)
        else:
            out.append(p if skip else _emoji_sub(p))
    return "".join(out)


def md_to_html(t):
    """Eski Markdown -> HTML. Biror noaniqlik bo'lsa None (o'zgartirmaymiz)."""
    out, i, n = [], 0, len(t)
    bold = ital = False
    while i < n:
        c = t[i]
        if c == "\\" and i + 1 < n:
            out.append(html.escape(t[i + 1]))
            i += 2
        elif c == "`":
            if t.startswith("```", i):
                j = t.find("```", i + 3)
                if j == -1:
                    return None
                out.append("<pre>" + html.escape(t[i + 3:j].strip("\n")) + "</pre>")
                i = j + 3
            else:
                j = t.find("`", i + 1)
                if j == -1:
                    return None
                out.append("<code>" + html.escape(t[i + 1:j]) + "</code>")
                i = j + 1
        elif c == "*":
            out.append("</b>" if bold else "<b>")
            bold = not bold
            i += 1
        elif c == "_":
            out.append("</i>" if ital else "<i>")
            ital = not ital
            i += 1
        elif c == "[":
            m = re.match(r"\[([^\]]*)\]\(([^)]*)\)", t[i:])
            if not m:
                return None
            out.append(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>')
            i += m.end()
        else:
            out.append(html.escape(c))
            i += 1
    if bold or ital:
        return None
    return "".join(out)


def entities_plain(text):
    ents, pos, off16 = [], 0, 0
    for m in _emoji_re.finditer(text):
        cid = _emoji_map.get(_norm_emoji(m.group(0)))
        if not cid:
            continue
        off16 += len(text[pos:m.start()].encode("utf-16-le")) // 2
        ln = len(m.group(0).encode("utf-16-le")) // 2
        ents.append(MessageEntity(type="custom_emoji", offset=off16, length=ln, custom_emoji_id=cid))
        off16 += ln
        pos = m.end()
    return ents


def _btn_icons_supported():
    global _btn_icon_ok
    if _btn_icon_ok is None:
        try:
            KeyboardButton("x", icon_custom_emoji_id="1")
            InlineKeyboardButton("x", callback_data="x", icon_custom_emoji_id="1")
            _btn_icon_ok = True
        except Exception:
            _btn_icon_ok = False
    return _btn_icon_ok


def _split_lead_emoji(text):
    m = _emoji_re.match(text or "")
    if not m:
        return None, text
    cid = _emoji_map.get(_norm_emoji(m.group(0)))
    rest = text[m.end():].lstrip()
    if not cid or not rest:
        return None, text
    return cid, rest


def premiumize_markup(rm):
    try:
        if not _btn_icons_supported():
            return rm
        if isinstance(rm, InlineKeyboardMarkup):
            rows, changed = [], False
            for row in rm.inline_keyboard:
                nr = []
                for b in row:
                    cid, rest = _split_lead_emoji(b.text)
                    if cid:
                        kw = {"text": rest, "icon_custom_emoji_id": cid}
                        for f in _INLINE_FIELDS:
                            v = getattr(b, f, None)
                            if v is not None and v is not False:
                                kw[f] = v
                        nr.append(InlineKeyboardButton(**kw))
                        changed = True
                    else:
                        nr.append(b)
                rows.append(nr)
            return InlineKeyboardMarkup(rows) if changed else rm
        if isinstance(rm, ReplyKeyboardMarkup):
            rows, changed = [], False
            for row in rm.keyboard:
                nr = []
                for b in row:
                    cid, rest = _split_lead_emoji(b.text)
                    if cid:
                        kw = {"text": rest, "icon_custom_emoji_id": cid}
                        for f in _REPLY_FIELDS:
                            v = getattr(b, f, None)
                            if v is not None and v is not False:
                                kw[f] = v
                        nr.append(KeyboardButton(**kw))
                        changed = True
                    else:
                        nr.append(b)
                rows.append(nr)
            if not changed:
                return rm
            return ReplyKeyboardMarkup(
                rows, resize_keyboard=getattr(rm, "resize_keyboard", None), one_time_keyboard=getattr(rm, "one_time_keyboard", None),
                selective=getattr(rm, "selective", None), input_field_placeholder=getattr(rm, "input_field_placeholder", None),
                is_persistent=getattr(rm, "is_persistent", None))
    except Exception:
        logger.exception("premium tugma xato")
    return rm


def premiumize_kwargs(kw):
    """send_message/edit_message_text argumentlarini premium emoji bilan almashtiradi (o'zgarmasa None)."""
    if not (_emoji_map and _emoji_re and (_emoji_enabled or _emoji_force)):
        return None
    k = dict(kw)
    changed = False
    text = k.get("text")
    if isinstance(text, str) and not k.get("entities") and _emoji_re.search(text):
        pm = k.get("parse_mode")
        pms = str(getattr(pm, "value", pm) or "").upper()
        if pms == "HTML":
            k["text"] = apply_html(text)
            changed = True
        elif pms == "MARKDOWN":
            h = md_to_html(text)
            if h is not None:
                k["text"] = apply_html(h)
                k["parse_mode"] = "HTML"
                changed = True
        elif not pms:
            ents = entities_plain(text)
            if ents:
                k["entities"] = ents
                changed = True
    rm = k.get("reply_markup")
    if rm is not None:
        rm2 = premiumize_markup(rm)
        if rm2 is not rm:
            k["reply_markup"] = rm2
            changed = True
    return k if changed else None


def _premium_ok():
    global _emoji_fail
    _emoji_fail = 0


def _premium_fail(e):
    global _emoji_fail, _emoji_enabled
    msg = str(e).lower()
    if not any(w in msg for w in ("emoji", "entit", "button", "icon", "parse")):
        return
    _emoji_fail += 1
    logger.warning("Premium emoji xatosi: %s", e)
    notify_owner(f"⚠️ Premium emoji xatosi: {e}\n(Bot egasida Telegram Premium borligini va emoji paketi to'g'riligini tekshiring.)",
                 key="pe-fail", ttl=3600)
    if _emoji_fail >= 5 and _emoji_enabled:
        _emoji_enabled = False
        spawn(set_setting("premium_emoji", "0"))
        notify_owner("🔕 Premium emoji ketma-ket 5 marta xato berdi va avtomatik O'CHIRILDI. Admin panel → ✨ Premium emoji.",
                     key="pe-off", ttl=60)


_STATIC_ALIASES = {}


def restore_label(text, state):
    """Premium ikonli tugma bosilganda Telegram emoji'siz matn yuboradi - eski yozuvga qaytaramiz."""
    if not _STATIC_ALIASES:
        for L in ("💰 Pul ishlash", "👤 Balans", "💸 Pul yechish", "🎁 Bonus", "🎟 Promokod", "💳 To'lov kanali",
                  "☎️ Murojaat", RULES_BUTTON, TRANSFER_BUTTON, "⚙️ Admin Panel"):
            _STATIC_ALIASES[re.sub(r"^[^A-Za-z0-9]+", "", L)] = L
    if text == re.sub(r"^[^A-Za-z0-9]+", "", CANCEL_TEXT):
        return CANCEL_TEXT
    if state:
        return text
    return _STATIC_ALIASES.get(text, text)


def parse_pack_name(text):
    t = (text or "").strip().split("?")[0].rstrip("/")
    name = t.split("/")[-1].lstrip("@")
    return name if re.fullmatch(r"[A-Za-z0-9_]{3,64}", name) else None


async def emoji_panel():
    try:
        names = json.loads((await get_setting("emoji_packs", "[]")) or "[]")
    except Exception:
        names = []
    on = (await get_setting("premium_emoji", "0")) == "1"
    text = (f"✨ Premium emoji\n{DIVIDER}\nHolat: {'✅ yoqilgan' if on and _emoji_map else '❌ o‘chirilgan'}\n"
            f"Paketlar: {', '.join(names) or 'yo‘q'}\nTanilgan emoji soni: {len(_emoji_map)}\n\n"
            "Ishlashi uchun:\n1) Bot egasida Telegram Premium bo'lishi shart.\n"
            "2) Premium emoji paketi qo'shing (havola: t.me/addemoji/NOM).\n"
            "3) «🧪 Sinov» bilan tekshiring.\n4) «Yoqish» ni bosing.\n\n"
            "Paketda yo'q emojilar oddiy holda qoladi. Bir nechta paket qo'shsa bo'ladi.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Paket qo'shish", callback_data="admin_emoji_add")],
        [InlineKeyboardButton("🧪 Sinov", callback_data="admin_emoji_test")],
        [InlineKeyboardButton("🔕 O'chirish" if on else "🔔 Yoqish", callback_data="admin_emoji_toggle")],
        [InlineKeyboardButton("🗑 Paketlarni o'chirish", callback_data="admin_emoji_clear")],
        [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
    ])
    return text, kb


async def admin_emoji_callback(query, context, user_id, data):
    global _emoji_force, _emoji_enabled
    if user_id != SUPER_ADMIN:
        await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="em_d")
        return True
    if data == "admin_emoji_add":
        context.user_data["state"] = "emoji_pack"
        await api_call(lambda: query.message.edit_text(
            "➕ Premium emoji paketining havolasini yuboring.\n\nMasalan: https://t.me/addemoji/PaketNomi\n\n"
            "Havolani topish: Telegramda premium emoji ustiga bosing → «Emoji paketini qo'shish» → paketni ulashing/havolani nusxalang."), action_desc="em_a")
        await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="em_k")
        return True
    if data == "admin_emoji_test":
        if not _emoji_map:
            await api_call(lambda: query.answer("Avval paket qo'shing", show_alert=True), action_desc="em_t0")
            return True
        await api_call(lambda: query.answer("🧪 Sinov yuborildi"), action_desc="em_t1")
        _emoji_force = True
        try:
            await context.bot.send_message(
                chat_id=SUPER_ADMIN, text="🧪 Sinov: 💰 👤 💸 🎁 ✅ ❌ 📱 🔥 🏆 🚀 ⚽ 🤖", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tugma sinovi", callback_data="admin_back")]]))
        except Exception:
            logger.exception("emoji sinov xato")
        finally:
            _emoji_force = False
        return True
    if data == "admin_emoji_toggle":
        on = (await get_setting("premium_emoji", "0")) == "1"
        if not on:
            if getattr(query.from_user, "is_premium", None) is not True:
                await api_call(lambda: query.answer(
                    "Sizning akkauntingizda Telegram Premium ko'rinmadi. Bot egasida Premium bo'lishi shart.", show_alert=True), action_desc="em_np")
                return True
            if not _emoji_map:
                await api_call(lambda: query.answer("Avval emoji paketi qo'shing", show_alert=True), action_desc="em_nm")
                return True
        await set_setting("premium_emoji", "0" if on else "1")
        _emoji_enabled = not on
    elif data == "admin_emoji_clear":
        await set_setting("emoji_packs", "[]")
        await reload_emoji(context.bot)
    text, kb = await emoji_panel()
    await api_call(lambda: query.message.edit_text(text, reply_markup=kb), action_desc="em_p")
    return True


async def handle_emoji_pack_text(update, context, user_id, text):
    context.user_data["state"] = None
    if user_id != SUPER_ADMIN:
        return True
    name = parse_pack_name(text)
    if not name:
        await api_call(lambda: update.message.reply_text("❌ Havola noto'g'ri. Masalan: https://t.me/addemoji/PaketNomi",
                                                         reply_markup=main_keyboard(user_id)), action_desc="ep_bad")
        return True
    try:
        ss = await context.bot.get_sticker_set(name)
    except Exception as e:
        await api_call(lambda: update.message.reply_text(f"❌ Paket topilmadi: {e}", reply_markup=main_keyboard(user_id)), action_desc="ep_nf")
        return True
    if str(getattr(ss, "sticker_type", "")) != "custom_emoji":
        await api_call(lambda: update.message.reply_text(
            "❌ Bu oddiy stiker to'plami. Premium EMOJI paketi kerak (havola t.me/addemoji/... bo'ladi).",
            reply_markup=main_keyboard(user_id)), action_desc="ep_type")
        return True
    try:
        names = json.loads((await get_setting("emoji_packs", "[]")) or "[]")
    except Exception:
        names = []
    if name not in names:
        names.append(name)
    await set_setting("emoji_packs", json.dumps(names))
    n = await reload_emoji(context.bot)
    await api_call(lambda: update.message.reply_text(
        f"✅ Paket qo'shildi: {name} ({len(ss.stickers)} ta emoji).\nJami tanilgan emoji: {n}\n\n"
        "Admin panel → ✨ Premium emoji → 🧪 Sinov bilan tekshiring, keyin yoqing.",
        reply_markup=main_keyboard(user_id)), action_desc="ep_ok")
    return True


# ========================================================
#  OMMAVIY XABAR (orqa fonda), ZAXIRA NUSXA
# ========================================================

async def _send_one(bot, uid, text, markdown=True, reply_markup=None):
    """'ok' | 'blocked' | 'failed'"""
    plain = not markdown
    for _ in range(4):
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode=None if plain else ParseMode.MARKDOWN, reply_markup=reply_markup)
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


async def run_broadcast(bot, admin_chat, user_ids, text, markdown=True, reply_markup=None):
    total = len(user_ids)
    counts = {"ok": 0, "blocked": 0, "failed": 0}
    progress = await api_call(lambda: bot.send_message(chat_id=admin_chat, text=f"📤 0/{total}"), action_desc="bc_start")
    last_edit = time.monotonic()
    BATCH = 20   # Telegram limiti ~30 xabar/soniya, biz 20/soniya yuboramiz
    done = 0
    for i in range(0, total, BATCH):
        t0 = time.monotonic()
        chunk = user_ids[i:i + BATCH]
        results = await asyncio.gather(*[_send_one(bot, u, text, markdown, reply_markup) for u in chunk], return_exceptions=True)
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
    global _BOT, _notify_q
    _BOT = app.bot
    _notify_q = asyncio.Queue(maxsize=300)
    spawn(_notify_worker())
    await start_health_server()   # Render 'Web Service' portni darhol ko'rishi kerak (yangi deploy 'live' bo'lishi uchun)
    try:
        await init_db()
    except Exception:
        logger.exception("BAZAGA ULANIB BO'LMADI (DATABASE_URL ni tekshiring)")
        await asyncio.sleep(3)
        raise
    await acquire_leadership()
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    try:
        await auto_migrate_from_sqlite(app.bot)
    except Exception:
        logger.exception("avto-ko'chirish xato")
    try:
        _cmds = [BotCommand("malumot", "📋 Mening ma'lumotlarim"), BotCommand("referalim", "🔗 Referal havolam")]
        await app.bot.set_my_commands([BotCommand("start", "🚀 Botni ishga tushirish")] + _cmds)
        await app.bot.set_my_commands(_cmds, scope=BotCommandScopeAllGroupChats())
    except Exception:
        logger.exception("buyruqlar ro'yxatini o'rnatib bo'lmadi")
    spawn(reload_emoji_safe(app.bot))
    spawn(startup_report(app.bot))
    spawn(backup_loop(app.bot))
    spawn(daily_loop(app.bot))
    logger.info("post_init tugadi: bot faol (%s, nusxa %s)", BOT_VERSION, INSTANCE_ID)


async def post_shutdown(app):
    await release_lease()
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
        for t in ("withdrawals", "support_messages", "promocodes", "promo_uses", "bonus_claims", "payment_channels",
                  "campaigns", "campaign_claims", "transfers"):
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
                     "create_promo", "set_payment_channel", "ban_user", "unban_user", "find_user", "daily_text",
                     "camp_link", "camp_amount", "camp_text", "emoji_pack"):
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
    if state == "emoji_pack":
        return await handle_emoji_pack_text(update, context, user_id, text)
    if state in ("camp_link", "camp_amount", "camp_text"):
        return await handle_camp_text(update, context, user_id, state, text)
    if state == "daily_text":
        context.user_data["state"] = None
        if user_id != SUPER_ADMIN:
            return True
        await set_setting("daily_text", text)
        await api_call(lambda: update.message.reply_text(
            "✅ Saqlandi. Har kuni 08:00 da shu matn yuboriladi.", reply_markup=main_keyboard(user_id)), action_desc="dl_s")
        return True
    if state == "find_user":
        context.user_data["state"] = None
        await handle_find_user_text(update, context, user_id, text)
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
        touch_profile(update.effective_user)
        text = restore_label(text, context.user_data.get("state"))
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
        if await handle_transfer_state(update, context, user_id, text):
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
        elif text == TRANSFER_BUTTON:
            await handle_transfer_start(update, context, user_id)
        elif text == "⚙️ Admin Panel" and is_admin:
            await open_admin_panel(update.message)
        else:
            await api_call(lambda: update.message.reply_text("🤔 Tanlang 👇", reply_markup=main_keyboard(user_id)), action_desc="un")
    except Exception:
        logger.exception("handle_buttons xato")


def _who(name, uname, uid):
    if name:
        return name
    if uname:
        return "@" + uname
    return str(uid)


def _short(s, n=18):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


async def build_user_card(uid, viewer_id):
    p = await get_user_profile(uid)
    if not p:
        return None, None
    paid, total = await count_refs(uid)
    ws = await withdraw_stats_user(uid)
    appr = ws.get("approved", (0, 0.0))
    pend = ws.get("pending", (0, 0.0))
    name = html.escape(p["full_name"]) if p["full_name"] else "ism hali saqlanmagan"
    uname = f" @{html.escape(p['username'])}" if p["username"] else ""
    if p["banned"]:
        status = "🚫 Bloklangan"
    elif not p["registered"]:
        status = "⏳ Ro'yxatdan o'tmoqda"
    else:
        status = "✅ Faol"
    inv = ""
    if p["referred_by"]:
        inv = f"\n🔗 Taklif qilgan: <code>{p['referred_by']}</code>"
    text = (
        f"👤 <a href=\"tg://user?id={uid}\">{name}</a>{uname}\n"
        f"🆔 <code>{uid}</code>\n"
        f"📱 Telefon: <code>{html.escape(p['phone'] or '—')}</code>\n{DIVIDER}\n"
        f"💰 Balans: <b>{fmt_money(p['balance'])} so'm</b>\n"
        f"👥 Taklif qilgan: <b>{paid}</b> ta (jami kelgan: {total})\n"
        f"💸 Yechgan: <b>{appr[0]}</b> marta — {fmt_money(appr[1])} so'm\n"
        f"⏳ Kutilayotgan: {pend[0]} ta ({fmt_money(pend[1])} so'm)\n{DIVIDER}\n"
        f"📅 Botga qo'shilgan: <b>{fmt_dt(p['joined_at'])}</b> ({ago(p['joined_at'])} oldin)\n"
        f"📌 Holat: {status}{inv}"
    )
    rows = []
    if total > 0:
        rows.append([InlineKeyboardButton(f"👥 Referallarini ko'rish ({total})", callback_data=f"admin_refs:{uid}:0")])
    if p["referred_by"]:
        rows.append([InlineKeyboardButton(f"⬆️ Taklif qilgan: {p['referred_by']}", callback_data=f"admin_user:{p['referred_by']}")])
    if viewer_id == SUPER_ADMIN and uid != SUPER_ADMIN:
        if p["banned"]:
            rows.append([InlineKeyboardButton("✅ Blokdan chiqarish", callback_data=f"admin_unban_id:{uid}")])
        else:
            rows.append([InlineKeyboardButton("🚫 Bloklash", callback_data=f"admin_ban_id:{uid}")])
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return text, InlineKeyboardMarkup(rows)


REF_PAGE = 8


async def build_refs_page(uid, page):
    _paid, total = await count_refs(uid)
    pages = max(1, (total + REF_PAGE - 1) // REF_PAGE)
    page = max(0, min(page, pages - 1))
    rows = await list_referrals(uid, REF_PAGE, page * REF_PAGE)
    lines = [f"👥 <b>Referallar</b> — <code>{uid}</code>\nJami: <b>{total}</b> · sahifa {page + 1}/{pages}\n{DIVIDER}"]
    btns = []
    for i, (rid, name, uname, registered, paid, joined) in enumerate(rows, start=page * REF_PAGE + 1):
        icon = "✅" if (registered and paid) else ("☑️" if registered else "⏳")
        label = _who(name, uname, rid)
        lines.append(f"{i}. {icon} {html.escape(label)} · <code>{rid}</code> · {fmt_dt(joined)}")
        btns.append([InlineKeyboardButton(f"{icon} {_short(label)} · {rid}", callback_data=f"admin_user:{rid}")])
    lines.append(f"\n✅ tasdiqlangan · ⏳ ro'yxatdan o'tmoqda · ☑️ pul berilmagan")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_refs:{uid}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_refs:{uid}:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton("‹ Foydalanuvchi kartasi", callback_data=f"admin_user:{uid}")])
    return "\n".join(lines), InlineKeyboardMarkup(btns)


async def build_top(limit=20):
    rows = await top_referrers(limit)
    if not rows:
        return "🏆 Hozircha takliflar yo'q.", back_keyboard()
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>Top referallar</b>\n{DIVIDER}"]
    btns, pair = [], []
    for i, (uid, cnt, name, uname, bal) in enumerate(rows, start=1):
        mark = medals[i - 1] if i <= 3 else f"{i}."
        label = _who(name, uname, uid)
        lines.append(f"{mark} {html.escape(label)}\n    🆔 <code>{uid}</code> · 👥 <b>{cnt}</b> ta · 💰 {fmt_money(bal or 0)}")
        pair.append(InlineKeyboardButton(f"{i}) {uid} · {cnt}", callback_data=f"admin_user:{uid}"))
        if len(pair) == 2:
            btns.append(pair)
            pair = []
    if pair:
        btns.append(pair)
    btns.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return "\n".join(lines), InlineKeyboardMarkup(btns)


async def admin_extra_callback(query, context, user_id, data):
    """v3 admin tugmalari. True qaytarsa - ishlandi."""
    if data == "admin_top":
        text, kb = await build_top(20)
        await api_call(lambda: query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="top")
        return True
    if data == "admin_find":
        context.user_data["state"] = "find_user"
        await api_call(lambda: query.message.edit_text(
            "🔎 <b>Foydalanuvchi qidirish</b>\n\nID, @username yoki telefon raqamini yuboring:", parse_mode=ParseMode.HTML), action_desc="find_p")
        await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="find_kb")
        return True
    if data.startswith("admin_user:"):
        try:
            uid = int(data.split(":", 1)[1])
        except ValueError:
            return True
        text, kb = await build_user_card(uid, user_id)
        if not text:
            await api_call(lambda: query.answer("❌ Topilmadi", show_alert=True), action_desc="card_nf")
        else:
            await api_call(lambda: query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="card")
        return True
    if data.startswith("admin_refs:"):
        try:
            _, uid, page = data.split(":")
            uid, page = int(uid), int(page)
        except ValueError:
            return True
        text, kb = await build_refs_page(uid, page)
        await api_call(lambda: query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="refs")
        return True
    if data.startswith("admin_ban_id:") or data.startswith("admin_unban_id:"):
        if user_id != SUPER_ADMIN:
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="bid_d")
            return True
        try:
            tid = int(data.split(":", 1)[1])
        except ValueError:
            return True
        ban = data.startswith("admin_ban_id:")
        if ban and tid == SUPER_ADMIN:
            return True
        await set_banned(tid, ban)
        await api_call(lambda: context.bot.send_message(
            chat_id=tid, text=BAN_TEXT if ban else "✅ Akkauntingiz blokdan chiqarildi. /start bosing."), action_desc="bid_n")
        text, kb = await build_user_card(tid, user_id)
        if text:
            await api_call(lambda: query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="bid_c")
        return True
    if data == "admin_pending":
        return await admin_pending_callback(query, context)
    if data.startswith("admin_emoji"):
        return await admin_emoji_callback(query, context, user_id, data)
    if data.startswith("admin_camp"):
        return await admin_camp_callback(query, context, user_id, data)
    if data.startswith("admin_daily"):
        if data == "admin_daily":
            if user_id != SUPER_ADMIN:
                await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="dl_d0")
                return True
            text, kb = await daily_panel()
            await api_call(lambda: query.message.edit_text(text, reply_markup=kb), action_desc="dl_open")
            return True
        return await admin_daily_callback(query, context, user_id, data)
    if data == "admin_toggle_events":
        if user_id != SUPER_ADMIN:
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="ev_d")
            return True
        cur = await get_setting("owner_events", "1")
        new = "0" if cur != "0" else "1"
        await set_setting("owner_events", new)
        msg = ("🔔 Bildirishnomalar YOQILDI: yangi foydalanuvchi, referal, to'lov va boshqa voqealar sizga keladi."
               if new == "1" else "🔕 Bildirishnomalar O'CHIRILDI. (Xatolar haqida xabar doim keladi.)")
        await api_call(lambda: query.message.edit_text(msg, reply_markup=back_keyboard()), action_desc="ev_t")
        return True
    return False


async def handle_find_user_text(update, context, user_id, text):
    q = text.strip()
    digits = "".join(ch for ch in q if ch.isdigit())
    tid = None
    if q.startswith("@") or (q and not digits):
        tid = await find_user_id_by_username(q)
    elif q.startswith("+") or (len(digits) == 12 and digits.startswith("998")):
        tid = await find_user_id_by_phone("+" + digits)
    elif digits and len(digits) <= 15:
        tid = int(digits)
    card, kb = (None, None)
    if tid:
        card, kb = await build_user_card(tid, user_id)
    if not card:
        await api_call(lambda: update.message.reply_text(
            "❌ Topilmadi. (Foydalanuvchi botga /start bosmagan bo'lishi mumkin.)", reply_markup=main_keyboard(user_id)), action_desc="find_nf")
        return
    await api_call(lambda: update.message.reply_text("🔎 Natija:", reply_markup=main_keyboard(user_id)), action_desc="find_h")
    await api_call(lambda: update.message.reply_text(card, parse_mode=ParseMode.HTML, reply_markup=kb), action_desc="find_c")


# ---------- diagnostika ----------

async def status_command(update, context):
    try:
        if not update.message or update.effective_user.id != SUPER_ADMIN:
            return
        s = await get_full_stats()
        up = int(time.time() - START_TIME)
        pend = s["wd"].get("pending", (0, 0.0))
        text = (
            f"🩺 Bot holati\n{DIVIDER}\n"
            f"📦 Versiya: {BOT_VERSION}\n🆔 Nusxa: {INSTANCE_ID}\n"
            f"⏱ Ishlash vaqti: {up // 3600} soat {(up % 3600) // 60} daqiqa\n"
            f"🗄 Baza: {'PostgreSQL ✅ (doimiy)' if USE_PG else 'SQLite ⚠️ (deploy da o‘chishi mumkin)'}\n"
            f"👥 Foydalanuvchilar: {s['users']} (bloklangan: {s['banned']})\n"
            f"💰 Jami balans: {fmt_money(s['balance'])} so'm\n"
            f"⏳ Kutilayotgan yechishlar: {pend[0]} ta ({fmt_money(pend[1])} so'm)\n"
            f"🔔 Bildirishnomalar: {'yoqilgan' if (await get_setting('owner_events', '1')) != '0' else 'o‘chirilgan'}\n"
            f"{_singleton_note}"
        )
        await api_call(lambda: update.message.reply_text(text), action_desc="holat")
    except Exception:
        logger.exception("holat xato")


async def startup_report(bot):
    await asyncio.sleep(3)
    try:
        me = await bot.get_me()
        users = (await get_stats())[0]
        lines = [
            "🟢 Bot ishga tushdi", f"🤖 @{me.username}", f"📦 Versiya: {BOT_VERSION}", f"🆔 Nusxa: {INSTANCE_ID}",
            "🗄 Baza: PostgreSQL ✅ (doimiy)" if USE_PG else "🗄 Baza: SQLite ⚠️ (deploy da o‘chishi mumkin!)",
            f"👥 Foydalanuvchilar: {users}",
        ]
        if _singleton_note:
            lines.append(_singleton_note.strip())
        chans = [(OTZIF_CHANNEL, "otzif")] + [(c, n) for c, n in await get_channels()]
        for cid, _n in chans:
            try:
                m = await bot.get_chat_member(cid, me.id)
                if m.status not in ("administrator", "creator"):
                    lines.append(f"❌ {cid} — bot ADMIN emas!")
                elif cid == OTZIF_CHANNEL and getattr(m, "can_post_messages", True) is False:
                    lines.append(f"❌ {cid} — botda xabar yuborish huquqi yo'q!")
                else:
                    lines.append(f"✅ {cid} — admin")
            except Exception as e:
                lines.append(f"❌ {cid} — tekshirib bo'lmadi: {e}")
        notify_owner("\n".join(lines), key="start-" + INSTANCE_ID, ttl=1)
    except Exception:
        logger.exception("startup_report xato")


LEASE_SECONDS = 60


def instance_info():
    e = os.environ
    return (f"{INSTANCE_ID} | xizmat: {e.get('RENDER_SERVICE_NAME') or '?'} | "
            f"commit: {(e.get('RENDER_GIT_COMMIT') or '?')[:7]} | host: {socket.gethostname()} | {BOT_VERSION}")


async def try_lease():
    now = time.time()
    n = await db_exec(
        'UPDATE bot_lock SET owner=?, expires_at=?, info=?, updated_at=? '
        'WHERE id=1 AND (owner IS NULL OR owner=? OR expires_at < ?)',
        (INSTANCE_ID, now + LEASE_SECONDS, instance_info(), now, INSTANCE_ID, now))
    return n > 0


async def get_lease_holder():
    return await db_one('SELECT owner, expires_at, info FROM bot_lock WHERE id=1')


async def release_lease():
    try:
        await db_exec('UPDATE bot_lock SET expires_at=0 WHERE id=1 AND owner=?', (INSTANCE_ID,))
    except Exception:
        pass


async def lease_renew_loop():
    while True:
        await asyncio.sleep(20)
        try:
            if not await try_lease():
                holder = await get_lease_holder()
                notify_owner("🔴 Bu nusxa (" + INSTANCE_ID + ") yetakchilikni yo'qotdi va to'xtatildi.\nYangi faol nusxa: "
                             + (holder[2] if holder else "?"), key="lost-" + INSTANCE_ID, ttl=1)
                await asyncio.sleep(3)
                os._exit(3)
        except Exception:
            logger.exception("lease yangilash xato")


async def acquire_leadership():
    """Faqat bitta nusxa ishlaydi. Ikkinchisi faol nusxa to'xtaguncha kutadi (Telegram bilan to'qnashmaydi)."""
    told = False
    since = time.monotonic()
    last_log = 0.0
    while True:
        try:
            if await try_lease():
                spawn(lease_renew_loop())
                return
            holder = await get_lease_holder()
            if time.monotonic() - last_log > 30:
                last_log = time.monotonic()
                logger.info("Boshqa nusxa faol, kutilmoqda: %s", holder[2] if holder and holder[2] else "?")
            if not told and time.monotonic() - since > 60:
                told = True
                notify_owner(
                    "🟡 Bu nusxa KUTISH rejimida (ishlamayapti):\n" + instance_info() +
                    "\n\nFaol nusxa:\n" + (holder[2] if holder and holder[2] else "?") +
                    "\n\nSabab: bir xil bot ikki joyda ishga tushirilgan. 'xizmat:' nomi orqali Render'da "
                    "ortiqcha xizmatni toping va Suspend qiling.", key="standby-" + INSTANCE_ID, ttl=3600)
        except Exception:
            logger.exception("lease xato")
        await asyncio.sleep(10)


async def start_health_server():
    """Render 'Web Service' bo'lsa port ochiq bo'lishi kerak (PORT muhit o'zgaruvchisi bo'lsa ishga tushadi)."""
    port = os.environ.get("PORT")
    if not port:
        return

    async def handle(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), 2)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
    try:
        server = await asyncio.start_server(handle, "0.0.0.0", int(port))
        spawn(server.serve_forever())
        logger.info("Health server: port %s", port)
    except Exception:
        logger.exception("health server xato")


async def error_handler(update, context):
    err = context.error
    if isinstance(err, Conflict):
        notify_owner(
            "⚠️ CONFLICT: shu bot tokeni bilan BOSHQA nusxa ham ishlayapti!\n\n"
            f"Bu nusxa: {instance_info()}\n\n"
            "Boshqa nusxa yangi kod emas (yangi versiyalar bir-birini avtomatik to'xtatadi). U ESKI versiya yoki "
            "DATABASE_URL ulanmagan nusxa, yoki boshqa joyda ishlayapti.\n\n"
            "Qanday topish:\n"
            "1) Render → Dashboard → BARCHA xizmatlar ro'yxati (Web Service va Background Worker). Hozirgisidan boshqasini "
            "oching → Settings → Suspend.\n"
            "2) Render tepasidagi 'My Workspace' ni bosib, boshqa Workspace/Team bormi, tekshiring.\n"
            "3) Boshqa hosting (Railway, Replit, PythonAnywhere...), kompyuter yoki telefon (Termux) da ishga tushirilmaganini tekshiring.",
            key="conflict", ttl=900)
        return
    if isinstance(err, (TimedOut, NetworkError)):
        return
    who = ""
    try:
        if isinstance(update, Update):
            u = update.effective_user
            act = (update.callback_query.data if update.callback_query else
                   (update.message.text if update.message and update.message.text else "?"))
            who = f"\nFoydalanuvchi: {u.id if u else '?'}\nAmal: {str(act)[:100]}"
    except Exception:
        pass
    logger.error("Xato: %s: %s%s", type(err).__name__, err, who, exc_info=err)   # egaga ham yuboriladi




def build_application():
    request = HTTPXRequest(connection_pool_size=256, connect_timeout=20.0, read_timeout=20.0,
                           write_timeout=20.0, pool_timeout=30.0)
    get_updates_request = HTTPXRequest(connection_pool_size=4, connect_timeout=20.0, read_timeout=30.0,
                                       write_timeout=20.0, pool_timeout=20.0)
    try:
        class SafeBot(ExtBot):
            """Markdown xato bo'lsa xabarni yo'qotmaydi; premium emoji yoqilgan bo'lsa emoji'larni almashtiradi."""
            async def send_message(self, *args, **kwargs):
                if not args:
                    try:
                        k2 = premiumize_kwargs(kwargs)
                    except Exception:
                        k2 = None
                    if k2 is not None:
                        try:
                            res = await super().send_message(**k2)
                            _premium_ok()
                            return res
                        except BadRequest as e:
                            _premium_fail(e)
                try:
                    return await super().send_message(*args, **kwargs)
                except BadRequest as e:
                    if kwargs.get("parse_mode") and "parse entities" in str(e).lower():
                        kwargs["parse_mode"] = None
                        return await super().send_message(*args, **kwargs)
                    raise

            async def edit_message_text(self, *args, **kwargs):
                if not args:
                    try:
                        k2 = premiumize_kwargs(kwargs)
                    except Exception:
                        k2 = None
                    if k2 is not None:
                        try:
                            res = await super().edit_message_text(**k2)
                            _premium_ok()
                            return res
                        except BadRequest as e:
                            _premium_fail(e)
                try:
                    return await super().edit_message_text(*args, **kwargs)
                except BadRequest as e:
                    if kwargs.get("parse_mode") and "parse entities" in str(e).lower():
                        kwargs["parse_mode"] = None
                        return await super().edit_message_text(*args, **kwargs)
                    raise

        bot = SafeBot(token=TOKEN, request=request, get_updates_request=get_updates_request)
        builder = ApplicationBuilder().bot(bot)
    except Exception:
        logger.exception("SafeBot yaratib bo'lmadi, oddiy rejim")
        builder = ApplicationBuilder().token(TOKEN).request(request).get_updates_request(get_updates_request)
    return (builder.concurrent_updates(True)          # foydalanuvchilar bir-birini kutmaydi
            .post_init(post_init).post_shutdown(post_shutdown).build())


def main():
    if not TOKEN:
        print("❌ BOT_TOKEN yo'q!")
        return
    # Python 3.14 da avtomatik event loop yaratilmaydi - o'zimiz yaratamiz (Render xatosi shu edi)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    _h = OwnerLogHandler(level=logging.ERROR)
    _h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logging.getLogger().addHandler(_h)
    app = build_application()
    _private = filters.ChatType.PRIVATE   # bot GURUHLARDA hech qachon o'zi xabar yozmaydi
    app.add_handler(CommandHandler("start", start, filters=_private))
    app.add_handler(CommandHandler("backup", backup_command, filters=_private))
    app.add_handler(CommandHandler("holat", status_command, filters=_private))
    app.add_handler(CommandHandler("malumot", malumot_command))       # shaxsiy chatda ham, guruhda ham
    app.add_handler(CommandHandler("referalim", referalim_command))   # shaxsiy chatda ham, guruhda ham
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(captcha_callback, pattern="^cap:"))
    app.add_handler(CallbackQueryHandler(refresh_ref_callback, pattern="^refresh_ref$"))
    app.add_handler(CallbackQueryHandler(support_answer_callback, pattern="^sup_answer:"))
    app.add_handler(CallbackQueryHandler(camp_user_callback, pattern="^camp_done:"))
    app.add_handler(CallbackQueryHandler(camp_decide_callback, pattern="^(camp_ok:|camp_no:)"))
    app.add_handler(CallbackQueryHandler(transfer_callback, pattern="^tr_"))
    app.add_handler(CallbackQueryHandler(withdraw_type_callback, pattern="^(wd_type:|wd_method:|wd_amt:|wd_cancel$)"))
    app.add_handler(CallbackQueryHandler(withdraw_admin_decision_callback, pattern="^(wdok:|wdno:)"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^(admin_|maint_|bonus_|rmch:|rmadm:|delpromo:|set_pay_channel|leave_)"))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(user_id=SUPER_ADMIN) & _private, restore_document))
    app.add_handler(MessageHandler(filters.CONTACT & _private, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & _private, handle_buttons))
    app.add_error_handler(error_handler)
    print("🚀 Bot ishga tushdi!")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == '__main__':
    main()
