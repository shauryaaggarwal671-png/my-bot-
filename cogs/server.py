from __future__ import annotations
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import PURPLE, PURPLE_DARK, ORANGE, GREEN, BLUE, GREY
import utils.embeds as E
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.server')

# ═══════════════════════════════════════════════════════════════════════════════
#  server.py — ONE cog, "sab kuch" server-utility toolkit:
#    • Welcome / Leave messages
#    • Announcements
#    • Tournaments / Events (with a Join/RSVP button + live participant count)
#    • Giveaways (timer + automatic winner pick + reroll)
#    • Polls (reaction voting, optional timer + auto-tally)
#    • Reaction/self-assign role panels (buttons, not raw reactions)
#    • Suggestions (with approve/deny + DM the suggester)
#    • Auto-responder triggers
#    • Server logging (message edits/deletes, member joins/leaves)
#    • Leveling / XP system with rank cards + leaderboard
#    • Autorole (auto-assign a role on join)
#    • Starboard
#
#  Everything lives in this file's own tables — nothing here reads from or
#  writes to tier_test.py's tables or the ticket system's tables. This is
#  the ONLY server-utility cog — cogs/server_events.py should be removed
#  once this is in place (see setup notes at the bottom of this file).
# ═══════════════════════════════════════════════════════════════════════════════

WELCOME_PLACEHOLDER_HELP = (
    'Placeholders you can use:\n'
    '`{member}` — mentions the member\n'
    '`{member_name}` — their display name (no ping)\n'
    '`{server}` — this server\'s name\n'
    '`{membercount}` — total member count'
)

DEFAULT_WELCOME_MESSAGE = 'Welcome {member} to **{server}**! 🎉 You are member #{membercount}.'
DEFAULT_LEAVE_MESSAGE = '👋 **{member_name}** has left **{server}**. We now have {membercount} members.'

NUMBER_EMOJIS = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']

SUGGESTION_STATUS_COLOR = {'approved': GREEN, 'denied': ORANGE, 'pending': GREY}
SUGGESTION_STATUS_ICON = {'approved': '✅', 'denied': '❌', 'pending': '⏳'}

DEFAULT_STARBOARD_EMOJI = '⭐'
DEFAULT_STARBOARD_THRESHOLD = 3

# XP awarded per eligible message, and the per-user cooldown between XP
# awards (classic Mee6-style: random 15-25 xp, once per 60s).
XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN_SECONDS = 60


def _xp_for_level(level: int) -> int:
    """Total XP required to REACH this level. Same curve popularized by
    Mee6, gives a smooth, slowly-increasing grind."""
    return 5 * (level ** 2) + 50 * level + 100


def _level_from_xp(xp: int) -> int:
    level = 0
    while xp >= _xp_for_level(level + 1):
        level += 1
    return level


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE
# ═══════════════════════════════════════════════════════════════════════════════

def _connect(bot):
    """Same locking behaviour as the rest of the bot (WAL + busy_timeout) so
    a save here never collides with a write happening from tickets/tier_test
    at the same moment."""
    return aiosqlite.connect(bot.db.db_path, timeout=10)


async def _apply_pragmas(db):
    await db.execute('PRAGMA journal_mode=WAL')
    await db.execute('PRAGMA busy_timeout=10000')


async def _ensure_table(bot):
    if getattr(bot, '_server_table_ready', False):
        return
    async with _connect(bot) as db:
        await _apply_pragmas(db)

        await db.execute('''
            CREATE TABLE IF NOT EXISTS server_event_settings (
                guild_id                  INTEGER PRIMARY KEY,
                welcome_enabled           INTEGER DEFAULT 0,
                welcome_channel_id        INTEGER,
                welcome_message           TEXT,
                welcome_banner_url        TEXT,
                leave_enabled             INTEGER DEFAULT 0,
                leave_channel_id          INTEGER,
                leave_message             TEXT,
                announcement_channel_id   INTEGER,
                announcement_ping_role_id INTEGER,
                event_log_channel_id      INTEGER,
                suggestion_channel_id     INTEGER,
                mod_log_channel_id        INTEGER,
                autorole_id               INTEGER,
                leveling_enabled          INTEGER DEFAULT 1,
                levelup_channel_id        INTEGER,
                starboard_channel_id      INTEGER,
                starboard_threshold       INTEGER DEFAULT 3,
                starboard_emoji           TEXT DEFAULT '⭐'
            )
        ''')
        # Migration safety net for servers already running an earlier
        # version of this cog before these columns existed.
        for col, coltype in (
            ('leave_enabled', 'INTEGER DEFAULT 0'),
            ('leave_channel_id', 'INTEGER'),
            ('leave_message', 'TEXT'),
            ('suggestion_channel_id', 'INTEGER'),
            ('mod_log_channel_id', 'INTEGER'),
            ('autorole_id', 'INTEGER'),
            ('leveling_enabled', 'INTEGER DEFAULT 1'),
            ('levelup_channel_id', 'INTEGER'),
            ('starboard_channel_id', 'INTEGER'),
            ('starboard_threshold', 'INTEGER DEFAULT 3'),
            ('starboard_emoji', "TEXT DEFAULT '⭐'"),
        ):
            try:
                await db.execute(f'ALTER TABLE server_event_settings ADD COLUMN {col} {coltype}')
            except Exception:
                pass  # column already exists

        # One row per posted tournament/event/giveaway message — powers the
        # Join button, live participant count, /listevents and /cancelevent.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS event_posts (
                message_id INTEGER PRIMARY KEY,
                guild_id   INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                name       TEXT NOT NULL,
                event_type TEXT NOT NULL,
                host_id    INTEGER,
                created_at TEXT NOT NULL
            )
        ''')
        # One row per member who has hit "Join"/"Enter" on an
        # event/tournament/giveaway post. Shared across all three since it's
        # always keyed by the (globally unique) message_id.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS event_signups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(message_id, user_id)
            )
        ''')

        # ── Giveaways ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS giveaways (
                message_id    INTEGER PRIMARY KEY,
                guild_id      INTEGER NOT NULL,
                channel_id    INTEGER NOT NULL,
                winners_count INTEGER NOT NULL,
                ends_at       TEXT NOT NULL,
                ended         INTEGER DEFAULT 0
            )
        ''')

        # ── Polls ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS polls (
                message_id INTEGER PRIMARY KEY,
                guild_id   INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                question   TEXT NOT NULL,
                options    TEXT NOT NULL,
                ends_at    TEXT,
                ended      INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        ''')

        # ── Self-assign role panels ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS role_panels (
                message_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                title       TEXT NOT NULL,
                description TEXT,
                created_at  TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS role_panel_buttons (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                role_id    INTEGER NOT NULL,
                emoji      TEXT,
                label      TEXT NOT NULL,
                style      TEXT DEFAULT 'secondary',
                UNIQUE(message_id, role_id)
            )
        ''')

        # ── Suggestions ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS suggestions (
                message_id INTEGER PRIMARY KEY,
                guild_id   INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')

        # ── Triggers / auto-responder ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS triggers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER NOT NULL,
                trigger_text  TEXT NOT NULL,
                response_text TEXT NOT NULL,
                match_type    TEXT DEFAULT 'contains',
                created_at    TEXT NOT NULL,
                UNIQUE(guild_id, trigger_text)
            )
        ''')

        # ── Leveling / XP ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS levels (
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                xp          INTEGER DEFAULT 0,
                last_xp_at  TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')

        # ── Starboard ──
        await db.execute('''
            CREATE TABLE IF NOT EXISTS starboard_posts (
                message_id           INTEGER PRIMARY KEY,
                starboard_message_id INTEGER NOT NULL,
                guild_id             INTEGER NOT NULL,
                channel_id           INTEGER NOT NULL,
                created_at           TEXT NOT NULL
            )
        ''')

        await db.commit()
    bot._server_table_ready = True


async def get_settings(bot, guild_id: int) -> dict:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM server_event_settings WHERE guild_id = ?', (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        await db.execute('INSERT OR IGNORE INTO server_event_settings (guild_id) VALUES (?)', (guild_id,))
        await db.commit()
    return {
        'guild_id': guild_id,
        'welcome_enabled': 0, 'welcome_channel_id': None, 'welcome_message': None, 'welcome_banner_url': None,
        'leave_enabled': 0, 'leave_channel_id': None, 'leave_message': None,
        'announcement_channel_id': None, 'announcement_ping_role_id': None,
        'event_log_channel_id': None, 'suggestion_channel_id': None,
        'mod_log_channel_id': None, 'autorole_id': None,
        'leveling_enabled': 1, 'levelup_channel_id': None,
        'starboard_channel_id': None, 'starboard_threshold': DEFAULT_STARBOARD_THRESHOLD,
        'starboard_emoji': DEFAULT_STARBOARD_EMOJI,
    }


async def update_setting(bot, guild_id: int, field: str, value):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('INSERT OR IGNORE INTO server_event_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE server_event_settings SET {field} = ? WHERE guild_id = ?', (value, guild_id))
        await db.commit()


# ── event_posts / event_signups helpers (tournaments, events, giveaways) ────

async def record_event_post(bot, message_id: int, guild_id: int, channel_id: int,
                             name: str, event_type: str, host_id: int | None):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT OR REPLACE INTO event_posts
               (message_id, guild_id, channel_id, name, event_type, host_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (message_id, guild_id, channel_id, name, event_type, host_id,
             datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def get_event_post(bot, message_id: int) -> dict | None:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM event_posts WHERE message_id = ?', (message_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_event_post(bot, message_id: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('DELETE FROM event_posts WHERE message_id = ?', (message_id,))
        await db.execute('DELETE FROM event_signups WHERE message_id = ?', (message_id,))
        await db.commit()


async def toggle_signup(bot, message_id: int, user_id: int) -> tuple[bool, int]:
    """Adds the user's signup if not present, else removes it.
    Returns (joined: bool, total_count: int) after the toggle."""
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        async with db.execute(
            'SELECT 1 FROM event_signups WHERE message_id = ? AND user_id = ?', (message_id, user_id)
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            await db.execute(
                'DELETE FROM event_signups WHERE message_id = ? AND user_id = ?', (message_id, user_id))
            joined = False
        else:
            await db.execute(
                'INSERT OR IGNORE INTO event_signups (message_id, user_id, created_at) VALUES (?, ?, ?)',
                (message_id, user_id, datetime.now(timezone.utc).isoformat())
            )
            joined = True
        await db.commit()

        async with db.execute(
            'SELECT COUNT(*) FROM event_signups WHERE message_id = ?', (message_id,)
        ) as cur:
            count_row = await cur.fetchone()
    return joined, (count_row[0] if count_row else 0)


async def get_signup_user_ids(bot, message_id: int) -> list[int]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        async with db.execute(
            'SELECT user_id FROM event_signups WHERE message_id = ? ORDER BY created_at ASC', (message_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_signup_count(bot, message_id: int) -> int:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        async with db.execute(
            'SELECT COUNT(*) FROM event_signups WHERE message_id = ?', (message_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def get_recent_event_posts(bot, guild_id: int, limit: int = 10) -> list[dict]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM event_posts WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?',
            (guild_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── giveaways ─────────────────────────────────────────────────────────────

async def create_giveaway(bot, message_id: int, guild_id: int, channel_id: int,
                           winners_count: int, ends_at_iso: str):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT OR REPLACE INTO giveaways (message_id, guild_id, channel_id, winners_count, ends_at, ended)
               VALUES (?, ?, ?, ?, ?, 0)''',
            (message_id, guild_id, channel_id, winners_count, ends_at_iso)
        )
        await db.commit()


async def get_giveaway(bot, message_id: int) -> dict | None:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM giveaways WHERE message_id = ?', (message_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_active_giveaways(bot) -> list[dict]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM giveaways WHERE ended = 0') as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_giveaway_ended(bot, message_id: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('UPDATE giveaways SET ended = 1 WHERE message_id = ?', (message_id,))
        await db.commit()


# ── polls ─────────────────────────────────────────────────────────────────

async def create_poll(bot, message_id: int, guild_id: int, channel_id: int,
                       question: str, options_json: str, ends_at_iso: str | None):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT OR REPLACE INTO polls
               (message_id, guild_id, channel_id, question, options, ends_at, ended, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)''',
            (message_id, guild_id, channel_id, question, options_json, ends_at_iso,
             datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def get_poll(bot, message_id: int) -> dict | None:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM polls WHERE message_id = ?', (message_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_active_timed_polls(bot) -> list[dict]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM polls WHERE ended = 0 AND ends_at IS NOT NULL'
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_poll_ended(bot, message_id: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('UPDATE polls SET ended = 1 WHERE message_id = ?', (message_id,))
        await db.commit()


# ── self-assign role panels ──────────────────────────────────────────────

async def create_role_panel(bot, message_id: int, guild_id: int, channel_id: int, title: str, description: str):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT OR REPLACE INTO role_panels (message_id, guild_id, channel_id, title, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (message_id, guild_id, channel_id, title, description, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def get_role_panel(bot, message_id: int) -> dict | None:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM role_panels WHERE message_id = ?', (message_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_role_panel_ids(bot) -> list[int]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        async with db.execute('SELECT message_id FROM role_panels') as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def delete_role_panel(bot, message_id: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('DELETE FROM role_panels WHERE message_id = ?', (message_id,))
        await db.execute('DELETE FROM role_panel_buttons WHERE message_id = ?', (message_id,))
        await db.commit()


async def add_role_button(bot, message_id: int, role_id: int, emoji: str | None, label: str, style: str = 'secondary'):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT INTO role_panel_buttons (message_id, role_id, emoji, label, style) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(message_id, role_id) DO UPDATE SET emoji = excluded.emoji, label = excluded.label, style = excluded.style''',
            (message_id, role_id, emoji, label, style)
        )
        await db.commit()


async def remove_role_button(bot, message_id: int, role_id: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('DELETE FROM role_panel_buttons WHERE message_id = ? AND role_id = ?', (message_id, role_id))
        await db.commit()


async def get_role_panel_buttons(bot, message_id: int) -> list[dict]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM role_panel_buttons WHERE message_id = ? ORDER BY id ASC', (message_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── suggestions ───────────────────────────────────────────────────────────

async def create_suggestion(bot, message_id: int, guild_id: int, channel_id: int, user_id: int, content: str):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT OR REPLACE INTO suggestions (message_id, guild_id, channel_id, user_id, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (message_id, guild_id, channel_id, user_id, content, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def get_suggestion(bot, message_id: int) -> dict | None:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM suggestions WHERE message_id = ?', (message_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ── triggers / auto-responder ────────────────────────────────────────────
# Small in-memory cache per guild so a message doesn't have to hit the
# database on every single message sent in the server.

async def add_trigger(bot, guild_id: int, trigger_text: str, response_text: str, match_type: str = 'contains'):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT INTO triggers (guild_id, trigger_text, response_text, match_type, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, trigger_text) DO UPDATE SET
                   response_text = excluded.response_text, match_type = excluded.match_type''',
            (guild_id, trigger_text, response_text, match_type, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
    _invalidate_trigger_cache(bot, guild_id)


async def remove_trigger(bot, guild_id: int, trigger_text: str) -> bool:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        cursor = await db.execute(
            'DELETE FROM triggers WHERE guild_id = ? AND LOWER(trigger_text) = LOWER(?)',
            (guild_id, trigger_text)
        )
        await db.commit()
        deleted = cursor.rowcount > 0
    _invalidate_trigger_cache(bot, guild_id)
    return deleted


async def get_triggers(bot, guild_id: int) -> list[dict]:
    cache = getattr(bot, '_trigger_cache', None)
    if cache is None:
        cache = {}
        bot._trigger_cache = cache
    if guild_id in cache:
        return cache[guild_id]

    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM triggers WHERE guild_id = ? ORDER BY id ASC', (guild_id,)) as cur:
            rows = await cur.fetchall()
    result = [dict(r) for r in rows]
    cache[guild_id] = result
    return result


def _invalidate_trigger_cache(bot, guild_id: int):
    cache = getattr(bot, '_trigger_cache', None)
    if cache is not None:
        cache.pop(guild_id, None)


# ── leveling / XP ─────────────────────────────────────────────────────────

async def get_level_row(bot, guild_id: int, user_id: int) -> dict:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM levels WHERE guild_id = ? AND user_id = ?', (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
    if row:
        return dict(row)
    return {'guild_id': guild_id, 'user_id': user_id, 'xp': 0, 'last_xp_at': None}


async def add_xp(bot, guild_id: int, user_id: int, amount: int) -> int:
    """Adds XP and returns the new total."""
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT INTO levels (guild_id, user_id, xp, last_xp_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                   xp = xp + excluded.xp, last_xp_at = excluded.last_xp_at''',
            (guild_id, user_id, amount, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()
        async with db.execute(
            'SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?', (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else amount


async def set_xp(bot, guild_id: int, user_id: int, amount: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT INTO levels (guild_id, user_id, xp, last_xp_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp''',
            (guild_id, user_id, max(0, amount), datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def get_leaderboard(bot, guild_id: int, limit: int = 10) -> list[dict]:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM levels WHERE guild_id = ? ORDER BY xp DESC LIMIT ?', (guild_id, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_rank_position(bot, guild_id: int, user_id: int) -> int:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        async with db.execute(
            '''SELECT COUNT(*) + 1 FROM levels
               WHERE guild_id = ? AND xp > (SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?)''',
            (guild_id, guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 1


# ── starboard ─────────────────────────────────────────────────────────────

async def get_starboard_post(bot, message_id: int) -> dict | None:
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM starboard_posts WHERE message_id = ?', (message_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def create_starboard_post(bot, message_id: int, starboard_message_id: int, guild_id: int, channel_id: int):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute(
            '''INSERT OR REPLACE INTO starboard_posts (message_id, starboard_message_id, guild_id, channel_id, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (message_id, starboard_message_id, guild_id, channel_id, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _fill_placeholders(template: str, guild: discord.Guild,
                        member: discord.Member | discord.User | None = None,
                        display_name: str | None = None) -> str:
    mention = member.mention if member else (display_name or 'Someone')
    name = display_name or (member.display_name if isinstance(member, discord.Member) else (member.name if member else 'Someone'))
    return (
        template
        .replace('{member}', mention)
        .replace('{member_name}', name)
        .replace('{server}', guild.name)
        .replace('{membercount}', str(guild.member_count))
    )


async def _log_action(bot, guild: discord.Guild, settings: dict, embed: discord.Embed):
    """Posts a copy of whatever just happened (announcement/tournament/
    event/giveaway/poll) to the configured 'server log' channel."""
    log_channel_id = settings.get('event_log_channel_id')
    if not log_channel_id:
        return
    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        return
    try:
        await log_channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning(f'[server] Could not post to log channel {log_channel_id} in guild {guild.id}.')


def _resolve_channel(interaction: discord.Interaction, settings: dict,
                      channel: discord.TextChannel | None) -> discord.TextChannel | None:
    if channel:
        return channel
    default_id = settings.get('announcement_channel_id')
    if default_id:
        resolved = interaction.guild.get_channel(default_id)
        if resolved:
            return resolved
    return interaction.channel if hasattr(interaction.channel, 'send') else None


def _signup_footer(base_text: str, count: int) -> str:
    plural = 'person is' if count == 1 else 'people are'
    return f'{base_text}  •  👥 {count} {plural} joining'


def _parse_duration(text: str) -> int | None:
    """'10m' -> 600, '1h30m' -> 5400, '2d' -> 172800. Returns None if the
    string has no recognizable d/h/m/s parts."""
    text = text.strip().lower()
    matches = re.findall(r'(\d+)\s*(d|h|m|s)', text)
    if not matches:
        return None
    seconds = 0
    for value, unit in matches:
        value = int(value)
        if unit == 'd':
            seconds += value * 86400
        elif unit == 'h':
            seconds += value * 3600
        elif unit == 'm':
            seconds += value * 60
        elif unit == 's':
            seconds += value
    return seconds


# ═══════════════════════════════════════════════════════════════════════════════
#  PERSISTENT VIEWS
#  custom_id is fixed (not per-message) for Join/Enter buttons, so the exact
#  event/giveaway is always looked up from interaction.message.id at click
#  time — that's what lets these views survive a bot restart via
#  bot.add_view() in setup() below instead of breaking on every reboot.
# ═══════════════════════════════════════════════════════════════════════════════

class EventJoinView(discord.ui.View):
    """Used on tournament/event posts."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Join', emoji='✅', style=discord.ButtonStyle.success, custom_id='server_events:join')
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        post = await get_event_post(self.bot, interaction.message.id)
        if not post:
            return await interaction.response.send_message(
                embed=E.error('This event could not be found — it may have been cancelled.'), ephemeral=True)

        joined, count = await toggle_signup(self.bot, interaction.message.id, interaction.user.id)
        verb = 'joined' if joined else 'left'
        await interaction.response.send_message(
            embed=E.success(f'You have **{verb}** "{post["name"]}"!'), ephemeral=True)

        try:
            embed = interaction.message.embeds[0]
            base = embed.footer.text.split('  •  👥')[0] if embed.footer and embed.footer.text else ''
            embed.set_footer(text=_signup_footer(base, count))
            await interaction.message.edit(embed=embed)
        except (IndexError, discord.HTTPException):
            pass

    @discord.ui.button(label='Participants', emoji='👥', style=discord.ButtonStyle.secondary, custom_id='server_events:participants')
    async def participants_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        post = await get_event_post(self.bot, interaction.message.id)
        if not post:
            return await interaction.response.send_message(
                embed=E.error('This event could not be found — it may have been cancelled.'), ephemeral=True)

        user_ids = await get_signup_user_ids(self.bot, interaction.message.id)
        if not user_ids:
            return await interaction.response.send_message(
                embed=E.base(f'👥  Participants — {post["name"]}', 'No one has joined yet.', color=GREY),
                ephemeral=True)

        description = '\n'.join(f'<@{uid}>' for uid in user_ids)
        if len(description) > 3900:
            description = description[:3900] + '\n… and more'
        embed = E.base(f'👥  Participants — {post["name"]}', description, color=PURPLE)
        embed.set_footer(text=f'{len(user_ids)} total')
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GiveawayJoinView(discord.ui.View):
    """Used on giveaway posts."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Enter Giveaway', emoji='🎉', style=discord.ButtonStyle.success, custom_id='server_events:giveaway_join')
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaway = await get_giveaway(self.bot, interaction.message.id)
        if not giveaway:
            return await interaction.response.send_message(
                embed=E.error('This giveaway could not be found.'), ephemeral=True)
        if giveaway['ended']:
            return await interaction.response.send_message(
                embed=E.error('This giveaway has already ended.'), ephemeral=True)

        joined, count = await toggle_signup(self.bot, interaction.message.id, interaction.user.id)
        verb = 'entered' if joined else 'left'
        await interaction.response.send_message(embed=E.success(f'You have **{verb}** the giveaway!'), ephemeral=True)

        try:
            embed = interaction.message.embeds[0]
            base = embed.footer.text.split('  •  👥')[0] if embed.footer and embed.footer.text else ''
            embed.set_footer(text=_signup_footer(base, count))
            await interaction.message.edit(embed=embed)
        except (IndexError, discord.HTTPException):
            pass


class RoleToggleButton(discord.ui.Button):
    """One button per role on a self-assign role panel. Toggling works from
    ANY panel a role appears on, since it only depends on role_id + member,
    not which panel/message triggered it."""
    def __init__(self, bot, role_id: int, label: str, emoji, style: discord.ButtonStyle):
        super().__init__(label=label[:80], emoji=emoji, style=style, custom_id=f'server_events:role:{role_id}')
        self.bot = bot
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message(
                embed=E.error('That role no longer exists on this server.'), ephemeral=True)

        me = interaction.guild.me
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            return await interaction.response.send_message(
                embed=E.error(f"I can't manage {role.mention} — my role needs to be positioned above it."),
                ephemeral=True)

        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason='Self-role panel toggle')
                await interaction.response.send_message(embed=E.success(f'Removed {role.mention}.'), ephemeral=True)
            else:
                await member.add_roles(role, reason='Self-role panel toggle')
                await interaction.response.send_message(embed=E.success(f'Added {role.mention}!'), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=E.error("I don't have permission to edit your roles."), ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=E.error('Something went wrong updating your roles. Try again.'), ephemeral=True)


def build_role_panel_view(bot, buttons_data: list[dict]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for b in buttons_data:
        style = getattr(discord.ButtonStyle, b.get('style') or 'secondary', discord.ButtonStyle.secondary)
        emoji = None
        if b.get('emoji'):
            try:
                emoji = discord.PartialEmoji.from_str(b['emoji'])
            except Exception:
                emoji = None
        view.add_item(RoleToggleButton(bot, b['role_id'], b.get('label') or 'Role', emoji, style))
    return view


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class ServerEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.periodic_check.start()

    def cog_unload(self):
        self.periodic_check.cancel()

    # ── background loop: auto-end giveaways + timed polls ─────────────────
    @tasks.loop(seconds=30)
    async def periodic_check(self):
        try:
            await self._check_giveaways()
        except Exception:
            logger.exception('[server] error while checking giveaways')
        try:
            await self._check_polls()
        except Exception:
            logger.exception('[server] error while checking polls')

    @periodic_check.before_loop
    async def before_periodic_check(self):
        await self.bot.wait_until_ready()

    async def _check_giveaways(self):
        now = datetime.now(timezone.utc)
        for giveaway in await get_active_giveaways(self.bot):
            try:
                ends_at = datetime.fromisoformat(giveaway['ends_at'])
            except ValueError:
                continue
            if ends_at <= now:
                await self._end_giveaway(giveaway)

    async def _end_giveaway(self, giveaway: dict):
        message_id = giveaway['message_id']
        await mark_giveaway_ended(self.bot, message_id)
        post = await get_event_post(self.bot, message_id)
        guild = self.bot.get_guild(giveaway['guild_id'])
        if not guild:
            return
        channel = guild.get_channel(giveaway['channel_id'])
        entries = await get_signup_user_ids(self.bot, message_id)
        winners_count = giveaway['winners_count']
        name = post['name'] if post else 'Giveaway'

        if not entries:
            winners = []
            result_text = 'No valid entries — no winner could be chosen. 😔'
        else:
            winners = random.sample(entries, min(winners_count, len(entries)))
            result_text = f"Congratulations {' '.join(f'<@{w}>' for w in winners)}! 🎉"

        result_embed = discord.Embed(
            title=f'🎉  Giveaway Ended: {name}',
            description=result_text,
            color=GREEN if winners else GREY,
            timestamp=datetime.now(timezone.utc)
        )
        if channel:
            try:
                await channel.send(embed=result_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
            try:
                msg = await channel.fetch_message(message_id)
                if msg.embeds:
                    old_embed = msg.embeds[0]
                    old_embed.title = f'🎉  [ENDED] {name}'
                    old_embed.color = GREY
                    await msg.edit(embed=old_embed, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def _tally_poll(self, message_id: int):
        poll = await get_poll(self.bot, message_id)
        if not poll:
            return None
        guild = self.bot.get_guild(poll['guild_id'])
        channel = guild.get_channel(poll['channel_id']) if guild else None
        if not channel:
            return None
        try:
            msg = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        options = json.loads(poll['options'])
        counts = []
        for i in range(len(options)):
            reaction = discord.utils.get(msg.reactions, emoji=NUMBER_EMOJIS[i])
            counts.append(max(0, reaction.count - 1) if reaction else 0)
        return poll, options, counts

    async def _finalize_poll(self, message_id: int, mark_ended: bool):
        result = await self._tally_poll(message_id)
        if not result:
            return None
        poll, options, counts = result
        if mark_ended:
            await mark_poll_ended(self.bot, message_id)

        lines = [f'{NUMBER_EMOJIS[i]}  {opt} — **{counts[i]}** vote(s)' for i, opt in enumerate(options)]
        embed = discord.Embed(
            title=f'📊  Poll Results: {poll["question"]}',
            description='\n'.join(lines),
            color=PURPLE_DARK,
            timestamp=datetime.now(timezone.utc)
        )
        if counts and max(counts) > 0:
            top = max(counts)
            winners = [options[i] for i, c in enumerate(counts) if c == top]
            embed.add_field(name='🏆 Leading', value=', '.join(winners), inline=False)
        return poll, embed

    async def _check_polls(self):
        now = datetime.now(timezone.utc)
        for poll in await get_active_timed_polls(self.bot):
            try:
                ends_at = datetime.fromisoformat(poll['ends_at'])
            except (ValueError, TypeError):
                continue
            if ends_at <= now:
                result = await self._finalize_poll(poll['message_id'], mark_ended=True)
                if not result:
                    continue
                poll_row, embed = result
                guild = self.bot.get_guild(poll_row['guild_id'])
                channel = guild.get_channel(poll_row['channel_id']) if guild else None
                if channel:
                    try:
                        await channel.send(embed=embed)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

    # ══════════════════════════════════════════════════════════════════════
    #  WELCOME / LEAVE
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        settings = await get_settings(self.bot, member.guild.id)

        # ── Autorole ──
        autorole_id = settings.get('autorole_id')
        if autorole_id:
            role = member.guild.get_role(autorole_id)
            if role:
                try:
                    await member.add_roles(role, reason='Autorole on join')
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning(f'[server] Could not assign autorole in guild {member.guild.id}.')

        # ── Join log (separate from the welcome message — goes to the mod log) ──
        mod_log_id = settings.get('mod_log_channel_id')
        if mod_log_id:
            mod_log = member.guild.get_channel(mod_log_id)
            if mod_log:
                account_age_days = (datetime.now(timezone.utc) - member.created_at).days
                log_embed = discord.Embed(
                    title='📥  Member Joined', color=GREEN, timestamp=datetime.now(timezone.utc),
                    description=f'{member.mention} (`{member.id}`)'
                )
                log_embed.set_thumbnail(url=member.display_avatar.url)
                log_embed.add_field(name='Account Created', value=f'<t:{int(member.created_at.timestamp())}:R>', inline=True)
                log_embed.add_field(name='Member Count', value=str(member.guild.member_count), inline=True)
                if account_age_days < 7:
                    log_embed.add_field(name='⚠️ Note', value=f'Account is only {account_age_days} day(s) old.', inline=False)
                try:
                    await mod_log.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # ── Welcome message ──
        if not settings.get('welcome_enabled') or not settings.get('welcome_channel_id'):
            return
        channel = member.guild.get_channel(settings['welcome_channel_id'])
        if not channel:
            return

        template = settings.get('welcome_message') or DEFAULT_WELCOME_MESSAGE
        text = _fill_placeholders(template, member.guild, member=member)

        embed = discord.Embed(title='👋  New Member!', description=text, color=GREEN,
                               timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        if settings.get('welcome_banner_url'):
            embed.set_image(url=settings['welcome_banner_url'])
        embed.set_footer(text=f'Member #{member.guild.member_count}')

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(f'[server] Could not send welcome message in guild {member.guild.id}.')

    # ══════════════════════════════════════════════════════════════════════
    #  TRIGGERS / AUTO-RESPONDER
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        triggers = await get_triggers(self.bot, message.guild.id)
        if not triggers:
            return

        content_lower = message.content.lower()
        for trig in triggers:
            t = trig['trigger_text'].lower()
            match_type = trig['match_type']
            matched = (
                (match_type == 'exact' and content_lower == t) or
                (match_type == 'startswith' and content_lower.startswith(t)) or
                (match_type == 'contains' and t in content_lower)
            )
            if not matched:
                continue

            response = (
                trig['response_text']
                .replace('{user}', message.author.mention)
                .replace('{user_name}', message.author.display_name)
                .replace('{server}', message.guild.name)
            )
            try:
                await message.channel.send(response)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(f'[server] Could not send trigger response in guild {message.guild.id}.')
            break  # only fire the first matching trigger per message

    @app_commands.command(name='triggeradd', description='(Admin/Owner only) Add an auto-responder — bot replies automatically when the trigger text appears.')
    @app_commands.describe(
        trigger='Text to watch for (case-insensitive)',
        response='What the bot replies with — placeholders: {user}, {user_name}, {server}',
        match_type='How the trigger text should match a message',
    )
    @app_commands.choices(match_type=[
        app_commands.Choice(name='Contains (anywhere in the message)', value='contains'),
        app_commands.Choice(name='Exact match (whole message)', value='exact'),
        app_commands.Choice(name='Starts with', value='startswith'),
    ])
    async def triggeradd(self, interaction: discord.Interaction, trigger: str, response: str, match_type: str = 'contains'):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await add_trigger(self.bot, interaction.guild_id, trigger, response, match_type)
        await interaction.response.send_message(
            embed=E.success(f'Trigger added!\n\n**Trigger ({match_type}):** {trigger}\n**Response:** {response}'),
            ephemeral=True)

    @app_commands.command(name='triggerremove', description='(Admin/Owner only) Remove an auto-responder trigger.')
    @app_commands.describe(trigger='The exact trigger text to remove (case-insensitive)')
    async def triggerremove(self, interaction: discord.Interaction, trigger: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        deleted = await remove_trigger(self.bot, interaction.guild_id, trigger)
        if deleted:
            await interaction.response.send_message(embed=E.success(f'Removed trigger "{trigger}".'), ephemeral=True)
        else:
            await interaction.response.send_message(embed=E.error(f'No trigger found matching "{trigger}".'), ephemeral=True)

    @app_commands.command(name='triggerlist', description='(Admin/Owner only) List all auto-responder triggers on this server.')
    async def triggerlist(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        triggers = await get_triggers(self.bot, interaction.guild_id)
        if not triggers:
            return await interaction.response.send_message(
                embed=E.base('🤖  Auto-Responder Triggers', 'No triggers set up yet. Add one with `/triggeradd`.', color=PURPLE_DARK),
                ephemeral=True)

        lines = [f"**{t['trigger_text']}** ({t['match_type']}) → {t['response_text']}" for t in triggers]
        description = '\n'.join(lines)
        if len(description) > 3900:
            description = description[:3900] + '\n… and more'
        embed = E.base('🤖  Auto-Responder Triggers', description, color=PURPLE_DARK)
        embed.set_footer(text=f'{len(triggers)} total')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        settings = await get_settings(self.bot, member.guild.id)

        mod_log_id = settings.get('mod_log_channel_id')
        if mod_log_id:
            mod_log = member.guild.get_channel(mod_log_id)
            if mod_log:
                role_names = ', '.join(r.mention for r in member.roles if r.name != '@everyone') or 'None'
                log_embed = discord.Embed(
                    title='📤  Member Left', color=ORANGE, timestamp=datetime.now(timezone.utc),
                    description=f'{member} (`{member.id}`)'
                )
                log_embed.set_thumbnail(url=member.display_avatar.url)
                log_embed.add_field(name='Joined At', value=f'<t:{int(member.joined_at.timestamp())}:R>' if member.joined_at else 'Unknown', inline=True)
                log_embed.add_field(name='Member Count', value=str(member.guild.member_count), inline=True)
                log_embed.add_field(name='Roles', value=role_names[:1024], inline=False)
                try:
                    await mod_log.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        if not settings.get('leave_enabled') or not settings.get('leave_channel_id'):
            return
        channel = member.guild.get_channel(settings['leave_channel_id'])
        if not channel:
            return

        template = settings.get('leave_message') or DEFAULT_LEAVE_MESSAGE
        text = _fill_placeholders(template, member.guild, member=member, display_name=member.display_name)

        embed = discord.Embed(title='👋  Member Left', description=text, color=GREY,
                               timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f'Now {member.guild.member_count} members')

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(f'[server] Could not send leave message in guild {member.guild.id}.')

    # ── Message edit/delete logging ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild or before.content == after.content:
            return
        settings = await get_settings(self.bot, before.guild.id)
        mod_log_id = settings.get('mod_log_channel_id')
        if not mod_log_id:
            return
        mod_log = before.guild.get_channel(mod_log_id)
        if not mod_log:
            return

        embed = discord.Embed(title='✏️  Message Edited', color=BLUE, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        embed.add_field(name='Channel', value=before.channel.mention, inline=False)
        embed.add_field(name='Before', value=(before.content or '*[empty]*')[:1024], inline=False)
        embed.add_field(name='After', value=(after.content or '*[empty]*')[:1024], inline=False)
        embed.add_field(name='Jump', value=f'[Go to message]({after.jump_url})', inline=False)
        try:
            await mod_log.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        settings = await get_settings(self.bot, message.guild.id)
        mod_log_id = settings.get('mod_log_channel_id')
        if not mod_log_id:
            return
        mod_log = message.guild.get_channel(mod_log_id)
        if not mod_log:
            return

        embed = discord.Embed(title='🗑️  Message Deleted', color=ORANGE, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name='Channel', value=message.channel.mention, inline=False)
        embed.add_field(name='Content', value=(message.content or '*[no text content]*')[:1024], inline=False)
        if message.attachments:
            embed.add_field(name='Attachments', value='\n'.join(a.url for a in message.attachments)[:1024], inline=False)
        try:
            await mod_log.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── Leveling / XP gain ──────────────────────────────────────────────────

    @commands.Cog.listener(name='on_message')
    async def on_message_xp(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        settings = await get_settings(self.bot, message.guild.id)
        if not settings.get('leveling_enabled', 1):
            return

        cooldowns = getattr(self.bot, '_xp_cooldowns', None)
        if cooldowns is None:
            cooldowns = {}
            self.bot._xp_cooldowns = cooldowns
        key = (message.guild.id, message.author.id)
        now = datetime.now(timezone.utc).timestamp()
        if now - cooldowns.get(key, 0) < XP_COOLDOWN_SECONDS:
            return
        cooldowns[key] = now

        row = await get_level_row(self.bot, message.guild.id, message.author.id)
        old_level = _level_from_xp(row['xp'])
        gained = random.randint(XP_MIN, XP_MAX)
        new_xp = await add_xp(self.bot, message.guild.id, message.author.id, gained)
        new_level = _level_from_xp(new_xp)

        if new_level > old_level:
            target_channel = message.channel
            levelup_channel_id = settings.get('levelup_channel_id')
            if levelup_channel_id:
                configured = message.guild.get_channel(levelup_channel_id)
                if configured:
                    target_channel = configured
            embed = discord.Embed(
                title='🎉  Level Up!',
                description=f'{message.author.mention} just reached **Level {new_level}**!',
                color=PURPLE, timestamp=datetime.now(timezone.utc)
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            try:
                await target_channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ── Starboard ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_star_change(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_star_change(payload)

    async def _handle_star_change(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        settings = await get_settings(self.bot, payload.guild_id)
        starboard_channel_id = settings.get('starboard_channel_id')
        if not starboard_channel_id:
            return
        emoji = settings.get('starboard_emoji') or DEFAULT_STARBOARD_EMOJI
        if str(payload.emoji) != emoji:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        channel = guild.get_channel(payload.channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        reaction = discord.utils.get(message.reactions, emoji=emoji)
        count = reaction.count if reaction else 0
        threshold = settings.get('starboard_threshold') or DEFAULT_STARBOARD_THRESHOLD

        starboard_channel = guild.get_channel(starboard_channel_id)
        if not starboard_channel or starboard_channel.id == channel.id:
            return

        existing = await get_starboard_post(self.bot, message.id)
        star_line = f'{emoji} **{count}** | {channel.mention}'

        if count >= threshold:
            if existing:
                try:
                    sb_msg = await starboard_channel.fetch_message(existing['starboard_message_id'])
                    await sb_msg.edit(content=star_line)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            else:
                embed = discord.Embed(
                    description=message.content or '*[no text content]*',
                    color=ORANGE, timestamp=message.created_at
                )
                embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
                if message.attachments:
                    embed.set_image(url=message.attachments[0].url)
                embed.add_field(name='Source', value=f'[Jump to message]({message.jump_url})', inline=False)
                try:
                    sb_sent = await starboard_channel.send(content=star_line, embed=embed)
                    await create_starboard_post(self.bot, message.id, sb_sent.id, payload.guild_id, channel.id)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        elif existing:
            try:
                sb_msg = await starboard_channel.fetch_message(existing['starboard_message_id'])
                await sb_msg.edit(content=star_line)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    @app_commands.command(name='setwelcomechannel', description='(Admin/Owner only) Set the channel where new-member welcome messages are sent.')
    @app_commands.describe(channel='The channel to post welcome messages in')
    async def setwelcomechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_channel_id', channel.id)
        await update_setting(self.bot, interaction.guild_id, 'welcome_enabled', 1)
        await interaction.response.send_message(embed=E.success(f'Welcome messages will now be sent in {channel.mention}.'), ephemeral=True)

    @app_commands.command(name='setwelcomemessage', description='(Admin/Owner only) Customize the welcome message text.')
    @app_commands.describe(message='Your custom message — see placeholders like {member}, {server}, {membercount}')
    async def setwelcomemessage(self, interaction: discord.Interaction, message: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_message', message)
        await interaction.response.send_message(
            embed=E.success(f'Welcome message updated!\n\n**Template:**\n{message}\n\n{WELCOME_PLACEHOLDER_HELP}'), ephemeral=True)

    @app_commands.command(name='setwelcomebanner', description='(Admin/Owner only) Set an image/banner shown on the welcome embed.')
    @app_commands.describe(image_url='Direct image URL (leave empty to remove the banner)')
    async def setwelcomebanner(self, interaction: discord.Interaction, image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_banner_url', image_url)
        await interaction.response.send_message(embed=E.success('Welcome banner updated!' if image_url else 'Welcome banner removed.'), ephemeral=True)

    @app_commands.command(name='togglewelcome', description='(Admin/Owner only) Turn welcome messages on or off.')
    @app_commands.choices(state=[app_commands.Choice(name='On', value='on'), app_commands.Choice(name='Off', value='off')])
    async def togglewelcome(self, interaction: discord.Interaction, state: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_enabled', 1 if state == 'on' else 0)
        await interaction.response.send_message(embed=E.success(f'Welcome messages turned **{state.upper()}**.'), ephemeral=True)

    @app_commands.command(name='testwelcome', description='(Admin/Owner only) Preview the welcome message as if you just joined.')
    async def testwelcome(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        channel_id = settings.get('welcome_channel_id')
        if not channel_id:
            return await interaction.response.send_message(embed=E.error('No welcome channel is set yet. Set one first with `/setwelcomechannel`.'), ephemeral=True)
        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(embed=E.error('The configured welcome channel no longer exists.'), ephemeral=True)

        template = settings.get('welcome_message') or DEFAULT_WELCOME_MESSAGE
        text = _fill_placeholders(template, interaction.guild, member=interaction.user)
        embed = discord.Embed(title='👋  New Member! (Test Preview)', description=text, color=GREEN, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        if settings.get('welcome_banner_url'):
            embed.set_image(url=settings['welcome_banner_url'])
        embed.set_footer(text=f'Member #{interaction.guild.member_count}  •  Test preview')
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message(embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        await interaction.response.send_message(embed=E.success(f'Test welcome message sent in {channel.mention}.'), ephemeral=True)

    @app_commands.command(name='setleavechannel', description='(Admin/Owner only) Set the channel where member-left messages are sent.')
    @app_commands.describe(channel='The channel to post leave messages in')
    async def setleavechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'leave_channel_id', channel.id)
        await update_setting(self.bot, interaction.guild_id, 'leave_enabled', 1)
        await interaction.response.send_message(embed=E.success(f'Leave messages will now be sent in {channel.mention}.'), ephemeral=True)

    @app_commands.command(name='setleavemessage', description='(Admin/Owner only) Customize the leave message text.')
    @app_commands.describe(message='Your custom message — see placeholders like {member_name}, {server}, {membercount}')
    async def setleavemessage(self, interaction: discord.Interaction, message: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'leave_message', message)
        await interaction.response.send_message(
            embed=E.success(f'Leave message updated!\n\n**Template:**\n{message}\n\n{WELCOME_PLACEHOLDER_HELP}'), ephemeral=True)

    @app_commands.command(name='toggleleave', description='(Admin/Owner only) Turn leave messages on or off.')
    @app_commands.choices(state=[app_commands.Choice(name='On', value='on'), app_commands.Choice(name='Off', value='off')])
    async def toggleleave(self, interaction: discord.Interaction, state: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'leave_enabled', 1 if state == 'on' else 0)
        await interaction.response.send_message(embed=E.success(f'Leave messages turned **{state.upper()}**.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  ANNOUNCEMENT SETTINGS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='setannouncechannel', description='(Admin/Owner only) Set the default channel used by /announce, /tournament and /event.')
    @app_commands.describe(channel='Default channel for announcements/tournaments/events')
    async def setannouncechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'announcement_channel_id', channel.id)
        await interaction.response.send_message(embed=E.success(f'Default announcement channel set to {channel.mention}.'), ephemeral=True)

    @app_commands.command(name='setannouncerole', description='(Admin/Owner only) Set a default role to ping on announcements/tournaments/events.')
    @app_commands.describe(role='Role to ping by default (leave empty to clear it)')
    async def setannouncerole(self, interaction: discord.Interaction, role: discord.Role | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'announcement_ping_role_id', role.id if role else None)
        await interaction.response.send_message(embed=E.success(f'Default ping role set to {role.mention}.' if role else 'Default ping role cleared.'), ephemeral=True)

    @app_commands.command(name='seteventlog', description='(Admin/Owner only) Set the log channel for a copy of every announcement/tournament/event/giveaway/poll.')
    @app_commands.describe(channel='Log channel (leave empty to disable logging)')
    async def seteventlog(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'event_log_channel_id', channel.id if channel else None)
        await interaction.response.send_message(embed=E.success(f'Event log channel set to {channel.mention}.' if channel else 'Event log channel disabled.'), ephemeral=True)

    @app_commands.command(name='announce', description='(Admin/Owner only) Post a server announcement.')
    @app_commands.describe(
        title='Announcement title', message='Announcement body',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)',
        image_url='Optional image/banner URL',
    )
    async def announce(self, interaction: discord.Interaction, title: str, message: str,
                        channel: discord.TextChannel | None = None, ping_role: discord.Role | None = None,
                        image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(embed=E.error('No valid channel to post in — pick one or run this in a text channel.'), ephemeral=True)

        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        embed = discord.Embed(title=f'📢  {title}', description=message, color=BLUE, timestamp=datetime.now(timezone.utc))
        if image_url:
            embed.set_image(url=image_url)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=f'Announced by {interaction.user.display_name}')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await target.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the announcement.'), ephemeral=True)

        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'Announcement posted in {target.mention}.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  TOURNAMENTS / EVENTS
    # ══════════════════════════════════════════════════════════════════════

    async def _post_signup_embed(self, interaction, event_type, emoji_title, color, name, description,
                                  date, time, prize, channel, ping_role, image_url):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(embed=E.error('No valid channel to post in — pick one or run this in a text channel.'), ephemeral=True)

        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        lines = [description, '']
        if date:
            lines.append(f'📅 **Date:** {date}')
        if time:
            lines.append(f'⏰ **Time:** {time}')
        if prize:
            lines.append(f'🏆 **Prize:** {prize}')

        embed = discord.Embed(title=f'{emoji_title}  {name}', description='\n'.join(lines), color=color, timestamp=datetime.now(timezone.utc))
        if image_url:
            embed.set_image(url=image_url)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=_signup_footer(f'Posted by {interaction.user.display_name}', 0))

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sent = await target.send(content=content, embed=embed, view=EventJoinView(self.bot), allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error(f'Failed to post the {event_type}.'), ephemeral=True)

        await record_event_post(self.bot, sent.id, interaction.guild_id, target.id, name, event_type, interaction.user.id)
        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'{event_type.capitalize()} posted in {target.mention} — members can hit **Join** to sign up.'), ephemeral=True)

    @app_commands.command(name='tournament', description='(Admin/Owner only) Post a tournament announcement with a Join button.')
    @app_commands.describe(
        name='Tournament name', description='Tournament details (format, rules, how to join, etc.)',
        date='Date of the tournament (e.g. "Sept 14, 2026")', time='Time of the tournament (e.g. "8 PM EST")',
        prize='Prize / reward (optional)', channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)', image_url='Optional image/banner URL',
    )
    async def tournament(self, interaction: discord.Interaction, name: str, description: str,
                          date: str | None = None, time: str | None = None, prize: str | None = None,
                          channel: discord.TextChannel | None = None, ping_role: discord.Role | None = None,
                          image_url: str | None = None):
        await self._post_signup_embed(interaction, 'tournament', '🏆  Tournament:', ORANGE, name, description, date, time, prize, channel, ping_role, image_url)

    @app_commands.command(name='event', description='(Admin/Owner only) Post a server event announcement with a Join button.')
    @app_commands.describe(
        name='Event name', description='Event details',
        date='Date of the event (e.g. "Sept 14, 2026")', time='Time of the event (e.g. "8 PM EST")',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)', image_url='Optional image/banner URL',
    )
    async def event(self, interaction: discord.Interaction, name: str, description: str,
                     date: str | None = None, time: str | None = None,
                     channel: discord.TextChannel | None = None, ping_role: discord.Role | None = None,
                     image_url: str | None = None):
        await self._post_signup_embed(interaction, 'event', '🎉  Event:', PURPLE, name, description, date, time, None, channel, ping_role, image_url)

    @app_commands.command(name='listevents', description='(Admin/Owner only) List recent tournaments/events and how many people joined.')
    async def listevents(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        posts = await get_recent_event_posts(self.bot, interaction.guild_id, limit=10)
        if not posts:
            return await interaction.response.send_message(embed=E.base('📋  Recent Events', 'No tournaments or events have been posted yet.', color=PURPLE_DARK), ephemeral=True)

        lines = []
        for post in posts:
            if post['event_type'] == 'giveaway':
                continue
            count = await get_signup_count(self.bot, post['message_id'])
            kind = '🏆' if post['event_type'] == 'tournament' else '🎉'
            jump = f"https://discord.com/channels/{post['guild_id']}/{post['channel_id']}/{post['message_id']}"
            lines.append(f"{kind} **{post['name']}** — {count} joined — [Jump]({jump})")

        if not lines:
            lines = ['No tournaments or events have been posted yet.']
        embed = E.base('📋  Recent Tournaments & Events', '\n'.join(lines), color=PURPLE_DARK)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='eventparticipants', description='(Admin/Owner only) List everyone who joined a specific event/tournament post.')
    @app_commands.describe(message_id='The message ID of the tournament/event post')
    async def eventparticipants(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        post = await get_event_post(self.bot, mid)
        if not post:
            return await interaction.response.send_message(embed=E.error('No tournament/event found for that message ID.'), ephemeral=True)

        user_ids = await get_signup_user_ids(self.bot, mid)
        if not user_ids:
            return await interaction.response.send_message(embed=E.base(f'👥  Participants — {post["name"]}', 'No one has joined yet.', color=GREY), ephemeral=True)

        description = '\n'.join(f'<@{uid}>' for uid in user_ids)
        embed = E.base(f'👥  Participants — {post["name"]}', description, color=PURPLE)
        embed.set_footer(text=f'{len(user_ids)} total')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='cancelevent', description='(Admin/Owner only) Cancel a tournament/event — stops tracking joins and deletes the post.')
    @app_commands.describe(message_id='The message ID of the tournament/event post to cancel')
    async def cancelevent(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        post = await get_event_post(self.bot, mid)
        if not post:
            return await interaction.response.send_message(embed=E.error('No tournament/event found for that message ID.'), ephemeral=True)

        channel = interaction.guild.get_channel(post['channel_id'])
        if channel:
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await delete_event_post(self.bot, mid)
        await interaction.response.send_message(embed=E.success(f'"{post["name"]}" has been cancelled and its signups cleared.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  GIVEAWAYS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='giveaway', description='(Admin/Owner only) Start a giveaway.')
    @app_commands.describe(
        prize='What is being given away', winners='How many winners to pick',
        duration='How long it runs — e.g. 10m, 1h, 2d, 1h30m',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)', image_url='Optional image/banner URL',
    )
    async def giveaway(self, interaction: discord.Interaction, prize: str, winners: int, duration: str,
                        channel: discord.TextChannel | None = None, ping_role: discord.Role | None = None,
                        image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        if winners < 1:
            return await interaction.response.send_message(embed=E.error('Winners must be at least 1.'), ephemeral=True)
        seconds = _parse_duration(duration)
        if not seconds:
            return await interaction.response.send_message(embed=E.error('Invalid duration. Use something like `10m`, `1h`, `2d`, or `1h30m`.'), ephemeral=True)

        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(embed=E.error('No valid channel to post in — pick one or run this in a text channel.'), ephemeral=True)

        ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        embed = discord.Embed(
            title=f'🎉  Giveaway: {prize}',
            description=(
                f'Click **🎉 Enter Giveaway** below to join!\n\n'
                f'**Winners:** {winners}\n'
                f'**Ends:** <t:{int(ends_at.timestamp())}:R> (<t:{int(ends_at.timestamp())}:f>)'
            ),
            color=ORANGE, timestamp=datetime.now(timezone.utc)
        )
        if image_url:
            embed.set_image(url=image_url)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=_signup_footer(f'Hosted by {interaction.user.display_name}', 0))

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sent = await target.send(content=content, embed=embed, view=GiveawayJoinView(self.bot), allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the giveaway.'), ephemeral=True)

        await record_event_post(self.bot, sent.id, interaction.guild_id, target.id, prize, 'giveaway', interaction.user.id)
        await create_giveaway(self.bot, sent.id, interaction.guild_id, target.id, winners, ends_at.isoformat())
        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'Giveaway posted in {target.mention}!'), ephemeral=True)

    @app_commands.command(name='giveawayend', description='(Admin/Owner only) End a giveaway early and pick winners now.')
    @app_commands.describe(message_id='The message ID of the giveaway post')
    async def giveawayend(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)
        giveaway = await get_giveaway(self.bot, mid)
        if not giveaway:
            return await interaction.response.send_message(embed=E.error('No giveaway found for that message ID.'), ephemeral=True)
        if giveaway['ended']:
            return await interaction.response.send_message(embed=E.error('That giveaway has already ended.'), ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._end_giveaway(giveaway)
        await interaction.followup.send(embed=E.success('Giveaway ended and winners announced.'), ephemeral=True)

    @app_commands.command(name='giveawayreroll', description='(Admin/Owner only) Reroll new winner(s) for an already-ended giveaway.')
    @app_commands.describe(message_id='The message ID of the giveaway post')
    async def giveawayreroll(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)
        giveaway = await get_giveaway(self.bot, mid)
        if not giveaway:
            return await interaction.response.send_message(embed=E.error('No giveaway found for that message ID.'), ephemeral=True)
        if not giveaway['ended']:
            return await interaction.response.send_message(embed=E.error('That giveaway hasn\'t ended yet — use `/giveawayend` first.'), ephemeral=True)

        post = await get_event_post(self.bot, mid)
        entries = await get_signup_user_ids(self.bot, mid)
        if not entries:
            return await interaction.response.send_message(embed=E.error('There are no valid entries to reroll from.'), ephemeral=True)

        winners = random.sample(entries, min(giveaway['winners_count'], len(entries)))
        channel = interaction.guild.get_channel(giveaway['channel_id'])
        embed = discord.Embed(
            title=f"🎉  Giveaway Reroll: {post['name'] if post else 'Giveaway'}",
            description=f"New winner(s): {' '.join(f'<@{w}>' for w in winners)} 🎉",
            color=GREEN, timestamp=datetime.now(timezone.utc)
        )
        if channel:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.response.send_message(embed=E.success('Rerolled — new winner(s) announced.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  POLLS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='poll', description='(Admin/Owner only) Post a reaction poll.')
    @app_commands.describe(
        question='The poll question', options='2 to 10 options, separated by | (e.g. "Yes | No | Maybe")',
        duration='Optional auto-close timer — e.g. 1h, 30m (leave empty to close manually with /pollend)',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)',
    )
    async def poll(self, interaction: discord.Interaction, question: str, options: str,
                   duration: str | None = None, channel: discord.TextChannel | None = None,
                   ping_role: discord.Role | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        opts = [o.strip() for o in options.split('|') if o.strip()]
        if len(opts) < 2:
            return await interaction.response.send_message(embed=E.error('Give at least 2 options, separated by `|`.'), ephemeral=True)
        if len(opts) > 10:
            return await interaction.response.send_message(embed=E.error('Max 10 options allowed.'), ephemeral=True)

        seconds = None
        if duration:
            seconds = _parse_duration(duration)
            if not seconds:
                return await interaction.response.send_message(embed=E.error('Invalid duration. Use something like `10m`, `1h`, `2d`.'), ephemeral=True)

        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(embed=E.error('No valid channel to post in — pick one or run this in a text channel.'), ephemeral=True)

        ends_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)) if seconds else None
        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        lines = [f'{NUMBER_EMOJIS[i]}  {opt}' for i, opt in enumerate(opts)]
        description = '\n'.join(lines)
        if ends_at:
            description += f'\n\n⏰ Closes <t:{int(ends_at.timestamp())}:R>'

        embed = discord.Embed(title=f'📊  {question}', description=description, color=BLUE, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f'Poll by {interaction.user.display_name}  •  React to vote')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sent = await target.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the poll.'), ephemeral=True)

        for i in range(len(opts)):
            try:
                await sent.add_reaction(NUMBER_EMOJIS[i])
            except (discord.Forbidden, discord.HTTPException):
                pass

        await create_poll(self.bot, sent.id, interaction.guild_id, target.id, question, json.dumps(opts), ends_at.isoformat() if ends_at else None)
        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'Poll posted in {target.mention}!'), ephemeral=True)

    @app_commands.command(name='pollresults', description='(Admin/Owner only) Show current votes for a poll without closing it.')
    @app_commands.describe(message_id='The message ID of the poll')
    async def pollresults(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        result = await self._finalize_poll(mid, mark_ended=False)
        if not result:
            return await interaction.response.send_message(embed=E.error('No poll found for that message ID (or the message was deleted).'), ephemeral=True)
        _, embed = result
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='pollend', description='(Admin/Owner only) Close a poll now and announce the results.')
    @app_commands.describe(message_id='The message ID of the poll')
    async def pollend(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        poll_row = await get_poll(self.bot, mid)
        if not poll_row:
            return await interaction.response.send_message(embed=E.error('No poll found for that message ID.'), ephemeral=True)
        if poll_row['ended']:
            return await interaction.response.send_message(embed=E.error('That poll is already closed.'), ephemeral=True)

        result = await self._finalize_poll(mid, mark_ended=True)
        if not result:
            return await interaction.response.send_message(embed=E.error('Could not fetch that poll message (deleted?).'), ephemeral=True)
        _, embed = result

        channel = interaction.guild.get_channel(poll_row['channel_id'])
        if channel:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.response.send_message(embed=E.success('Poll closed — results announced.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  SELF-ASSIGN ROLE PANELS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='rolepanelcreate', description='(Admin/Owner only) Create a self-assign role panel (add roles to it after with /rolepaneladd).')
    @app_commands.describe(title='Panel title', description='Panel description', channel='Channel to post in (defaults to here)')
    async def rolepanelcreate(self, interaction: discord.Interaction, title: str, description: str,
                               channel: discord.TextChannel | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        target = channel or (interaction.channel if hasattr(interaction.channel, 'send') else None)
        if not target:
            return await interaction.response.send_message(embed=E.error('No valid channel to post in.'), ephemeral=True)

        embed = discord.Embed(title=f'🎭  {title}', description=description, color=PURPLE, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text='Click a button below to add/remove that role — no roles added yet')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sent = await target.send(embed=embed)
        except discord.Forbidden:
            return await interaction.followup.send(embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the role panel.'), ephemeral=True)

        await create_role_panel(self.bot, sent.id, interaction.guild_id, target.id, title, description)
        await interaction.followup.send(
            embed=E.success(f'Role panel posted in {target.mention}.\nMessage ID: `{sent.id}` — use `/rolepaneladd` with this ID to add roles.'),
            ephemeral=True)

    @app_commands.command(name='rolepaneladd', description='(Admin/Owner only) Add a role button to an existing role panel.')
    @app_commands.describe(message_id='The message ID of the role panel', role='Role to add', emoji='Optional emoji for the button', label='Optional button label (defaults to the role name)')
    async def rolepaneladd(self, interaction: discord.Interaction, message_id: str, role: discord.Role,
                            emoji: str | None = None, label: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        panel = await get_role_panel(self.bot, mid)
        if not panel:
            return await interaction.response.send_message(embed=E.error('No role panel found for that message ID.'), ephemeral=True)

        me = interaction.guild.me
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            return await interaction.response.send_message(embed=E.error(f"I can't manage {role.mention} — my role needs to be positioned above it."), ephemeral=True)

        existing = await get_role_panel_buttons(self.bot, mid)
        if len(existing) >= 25 and not any(b['role_id'] == role.id for b in existing):
            return await interaction.response.send_message(embed=E.error('This panel already has the max of 25 roles.'), ephemeral=True)

        await add_role_button(self.bot, mid, role.id, emoji, label or role.name)
        buttons = await get_role_panel_buttons(self.bot, mid)
        view = build_role_panel_view(self.bot, buttons)

        channel = interaction.guild.get_channel(panel['channel_id'])
        if not channel:
            return await interaction.response.send_message(embed=E.error('The panel\'s channel no longer exists.'), ephemeral=True)
        try:
            msg = await channel.fetch_message(mid)
            embed = msg.embeds[0] if msg.embeds else discord.Embed(title=panel['title'], description=panel['description'], color=PURPLE)
            embed.set_footer(text=f'Click a button below to add/remove that role  •  {len(buttons)} role(s)')
            await msg.edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(embed=E.error('Could not update the panel message — it may have been deleted.'), ephemeral=True)

        await interaction.response.send_message(embed=E.success(f'Added {role.mention} to the panel.'), ephemeral=True)

    @app_commands.command(name='rolepanelremove', description='(Admin/Owner only) Remove a role button from a role panel.')
    @app_commands.describe(message_id='The message ID of the role panel', role='Role to remove')
    async def rolepanelremove(self, interaction: discord.Interaction, message_id: str, role: discord.Role):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        panel = await get_role_panel(self.bot, mid)
        if not panel:
            return await interaction.response.send_message(embed=E.error('No role panel found for that message ID.'), ephemeral=True)

        await remove_role_button(self.bot, mid, role.id)
        buttons = await get_role_panel_buttons(self.bot, mid)
        view = build_role_panel_view(self.bot, buttons) if buttons else None

        channel = interaction.guild.get_channel(panel['channel_id'])
        if channel:
            try:
                msg = await channel.fetch_message(mid)
                embed = msg.embeds[0] if msg.embeds else discord.Embed(title=panel['title'], description=panel['description'], color=PURPLE)
                embed.set_footer(text=f'Click a button below to add/remove that role  •  {len(buttons)} role(s)')
                await msg.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message(embed=E.success(f'Removed {role.mention} from the panel.'), ephemeral=True)

    @app_commands.command(name='rolepaneldelete', description='(Admin/Owner only) Delete a role panel entirely.')
    @app_commands.describe(message_id='The message ID of the role panel to delete')
    async def rolepaneldelete(self, interaction: discord.Interaction, message_id: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        panel = await get_role_panel(self.bot, mid)
        if not panel:
            return await interaction.response.send_message(embed=E.error('No role panel found for that message ID.'), ephemeral=True)

        channel = interaction.guild.get_channel(panel['channel_id'])
        if channel:
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await delete_role_panel(self.bot, mid)
        await interaction.response.send_message(embed=E.success('Role panel deleted.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  SUGGESTIONS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='setsuggestchannel', description='(Admin/Owner only) Set the channel where /suggest posts go.')
    @app_commands.describe(channel='Suggestions channel')
    async def setsuggestchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'suggestion_channel_id', channel.id)
        await interaction.response.send_message(embed=E.success(f'Suggestions will now be posted in {channel.mention}.'), ephemeral=True)

    @app_commands.command(name='suggest', description='Submit a suggestion for the server.')
    @app_commands.describe(content='Your suggestion')
    async def suggest(self, interaction: discord.Interaction, content: str):
        settings = await get_settings(self.bot, interaction.guild_id)
        channel_id = settings.get('suggestion_channel_id')
        if not channel_id:
            return await interaction.response.send_message(embed=E.error('Suggestions aren\'t set up on this server yet. Ask an admin to run `/setsuggestchannel`.'), ephemeral=True)
        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(embed=E.error('The configured suggestions channel no longer exists.'), ephemeral=True)

        embed = discord.Embed(title='💡  New Suggestion', description=content, color=SUGGESTION_STATUS_COLOR['pending'], timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f'{SUGGESTION_STATUS_ICON["pending"]} Pending')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sent = await channel.send(embed=embed)
            await sent.add_reaction('👍')
            await sent.add_reaction('👎')
        except discord.Forbidden:
            return await interaction.followup.send(embed=E.error("I don't have permission to post in the suggestions channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post your suggestion.'), ephemeral=True)

        await create_suggestion(self.bot, sent.id, interaction.guild_id, channel.id, interaction.user.id, content)
        await interaction.followup.send(embed=E.success(f'Your suggestion was posted in {channel.mention}!'), ephemeral=True)

    @app_commands.command(name='suggestionstatus', description='(Admin/Owner only) Approve/deny/reset a suggestion — the suggester gets DM\'d.')
    @app_commands.describe(message_id='The message ID of the suggestion', status='New status', reason='Optional reason shown to the suggester')
    @app_commands.choices(status=[
        app_commands.Choice(name='Approved', value='approved'),
        app_commands.Choice(name='Denied', value='denied'),
        app_commands.Choice(name='Pending', value='pending'),
    ])
    async def suggestionstatus(self, interaction: discord.Interaction, message_id: str, status: str, reason: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(embed=E.error("That doesn't look like a valid message ID."), ephemeral=True)

        suggestion = await get_suggestion(self.bot, mid)
        if not suggestion:
            return await interaction.response.send_message(embed=E.error('No suggestion found for that message ID.'), ephemeral=True)

        channel = interaction.guild.get_channel(suggestion['channel_id'])
        if not channel:
            return await interaction.response.send_message(embed=E.error('The suggestions channel no longer exists.'), ephemeral=True)
        try:
            msg = await channel.fetch_message(mid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(embed=E.error('Could not find that suggestion message — it may have been deleted.'), ephemeral=True)

        embed = msg.embeds[0] if msg.embeds else discord.Embed(title='💡  Suggestion', description=suggestion['content'])
        embed.color = SUGGESTION_STATUS_COLOR.get(status, GREY)
        footer = f'{SUGGESTION_STATUS_ICON.get(status, "")} {status.capitalize()} by {interaction.user.display_name}'
        if reason:
            footer += f' — {reason}'
        embed.set_footer(text=footer)
        try:
            await msg.edit(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

        member = interaction.guild.get_member(suggestion['user_id'])
        if member:
            dm_desc = suggestion['content'] + (f'\n\n**Reason:** {reason}' if reason else '')
            try:
                await member.send(embed=E.base(f'{SUGGESTION_STATUS_ICON.get(status, "")} Your suggestion was {status}!', dm_desc, color=embed.color))
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message(embed=E.success(f'Suggestion marked as **{status}**.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  SERVER LOGGING
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='setmodlog', description='(Admin/Owner only) Set the log channel for message edits/deletes and member joins/leaves.')
    @app_commands.describe(channel='Log channel (leave empty to disable)')
    async def setmodlog(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'mod_log_channel_id', channel.id if channel else None)
        await interaction.response.send_message(
            embed=E.success(f'Server logs will now be posted in {channel.mention}.' if channel else 'Server logging disabled.'),
            ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  AUTOROLE
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='setautorole', description='(Admin/Owner only) Automatically give new members this role when they join.')
    @app_commands.describe(role='Role to auto-assign on join')
    async def setautorole(self, interaction: discord.Interaction, role: discord.Role):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        me = interaction.guild.me
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            return await interaction.response.send_message(
                embed=E.error(f"I can't manage {role.mention} — my role needs to be positioned above it."), ephemeral=True)
        await update_setting(self.bot, interaction.guild_id, 'autorole_id', role.id)
        await interaction.response.send_message(embed=E.success(f'New members will automatically get {role.mention}.'), ephemeral=True)

    @app_commands.command(name='removeautorole', description='(Admin/Owner only) Turn off autorole.')
    async def removeautorole(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'autorole_id', None)
        await interaction.response.send_message(embed=E.success('Autorole disabled.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  LEVELING / XP
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='togglelevels', description='(Admin/Owner only) Turn the XP/leveling system on or off.')
    @app_commands.choices(state=[app_commands.Choice(name='On', value='on'), app_commands.Choice(name='Off', value='off')])
    async def togglelevels(self, interaction: discord.Interaction, state: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'leveling_enabled', 1 if state == 'on' else 0)
        await interaction.response.send_message(embed=E.success(f'Leveling system turned **{state.upper()}**.'), ephemeral=True)

    @app_commands.command(name='setlevelchannel', description='(Admin/Owner only) Set where level-up announcements are sent (defaults to wherever the message was sent).')
    @app_commands.describe(channel='Level-up announcement channel (leave empty to announce in the same channel as the message)')
    async def setlevelchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'levelup_channel_id', channel.id if channel else None)
        await interaction.response.send_message(
            embed=E.success(f'Level-up messages will now be posted in {channel.mention}.' if channel else 'Level-up messages will be posted in the channel where the message was sent.'),
            ephemeral=True)

    @app_commands.command(name='setxp', description='(Admin/Owner only) Manually set a member\'s XP.')
    @app_commands.describe(user='Member to update', amount='New total XP')
    async def setxp(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await set_xp(self.bot, interaction.guild_id, user.id, amount)
        level = _level_from_xp(max(0, amount))
        await interaction.response.send_message(embed=E.success(f'{user.mention} is now at **{amount} XP** (Level {level}).'), ephemeral=True)

    @app_commands.command(name='rank', description='Check your (or someone else\'s) level and XP.')
    @app_commands.describe(user='Member to check (defaults to yourself)')
    async def rank(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        row = await get_level_row(self.bot, interaction.guild_id, target.id)
        xp = row['xp']
        level = _level_from_xp(xp)
        current_floor = _xp_for_level(level)
        next_floor = _xp_for_level(level + 1)
        progress = xp - current_floor
        needed = next_floor - current_floor
        position = await get_rank_position(self.bot, interaction.guild_id, target.id)

        bar_len = 20
        filled = int(bar_len * progress / needed) if needed else 0
        bar = '█' * filled + '░' * (bar_len - filled)

        embed = discord.Embed(title=f'📊  Rank — {target.display_name}', color=PURPLE, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name='Level', value=str(level), inline=True)
        embed.add_field(name='Server Rank', value=f'#{position}', inline=True)
        embed.add_field(name='Total XP', value=str(xp), inline=True)
        embed.add_field(name='Progress to Next Level', value=f'`{bar}`\n{progress} / {needed} XP', inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='leaderboard', description='Show the top XP earners on this server.')
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await get_leaderboard(self.bot, interaction.guild_id, limit=10)
        if not rows:
            return await interaction.response.send_message(
                embed=E.base('🏆  XP Leaderboard', 'No one has earned XP yet — start chatting!', color=PURPLE_DARK))

        medals = ['🥇', '🥈', '🥉']
        lines = []
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f'#{i + 1}'
            level = _level_from_xp(row['xp'])
            lines.append(f"{prefix}  <@{row['user_id']}> — **{row['xp']} XP** (Level {level})")

        embed = E.base('🏆  XP Leaderboard', '\n'.join(lines), color=PURPLE_DARK)
        await interaction.response.send_message(embed=embed)

    # ══════════════════════════════════════════════════════════════════════
    #  STARBOARD
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='setstarboard', description='(Admin/Owner only) Enable/configure the starboard.')
    @app_commands.describe(
        channel='Channel where starred messages get reposted',
        threshold='How many reactions are needed (default 3)',
        emoji='Which emoji triggers the starboard (default ⭐)',
    )
    async def setstarboard(self, interaction: discord.Interaction, channel: discord.TextChannel,
                            threshold: int = DEFAULT_STARBOARD_THRESHOLD, emoji: str = DEFAULT_STARBOARD_EMOJI):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        if threshold < 1:
            return await interaction.response.send_message(embed=E.error('Threshold must be at least 1.'), ephemeral=True)
        await update_setting(self.bot, interaction.guild_id, 'starboard_channel_id', channel.id)
        await update_setting(self.bot, interaction.guild_id, 'starboard_threshold', threshold)
        await update_setting(self.bot, interaction.guild_id, 'starboard_emoji', emoji)
        await interaction.response.send_message(
            embed=E.success(f'Starboard set up in {channel.mention} — messages need **{threshold}x {emoji}** to get posted.'),
            ephemeral=True)

    @app_commands.command(name='starboarddisable', description='(Admin/Owner only) Turn off the starboard.')
    async def starboarddisable(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'starboard_channel_id', None)
        await interaction.response.send_message(embed=E.success('Starboard disabled.'), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    #  OVERVIEW
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name='serversettings', description='(Admin/Owner only) View the current welcome/leave/announcement/suggestion settings.')
    async def serversettings(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)

        def ch(field):
            cid = settings.get(field)
            return f'<#{cid}>' if cid else '*Not set*'

        def rl(field):
            rid = settings.get(field)
            return f'<@&{rid}>' if rid else '*Not set*'

        embed = discord.Embed(title='⚙️  Server Events Settings', color=PURPLE_DARK, timestamp=datetime.now(timezone.utc))
        embed.add_field(name='👋 Welcome', value=(
            f"**Enabled:** {'Yes' if settings.get('welcome_enabled') else 'No'}\n"
            f"**Channel:** {ch('welcome_channel_id')}\n"
            f"**Message:** {settings.get('welcome_message') or '*Default*'}"
        ), inline=False)
        embed.add_field(name='🚪 Leave', value=(
            f"**Enabled:** {'Yes' if settings.get('leave_enabled') else 'No'}\n"
            f"**Channel:** {ch('leave_channel_id')}\n"
            f"**Message:** {settings.get('leave_message') or '*Default*'}"
        ), inline=False)
        embed.add_field(name='📢 Announcements / Tournaments / Events / Giveaways / Polls', value=(
            f"**Default Channel:** {ch('announcement_channel_id')}\n"
            f"**Default Ping Role:** {rl('announcement_ping_role_id')}\n"
            f"**Event Log Channel:** {ch('event_log_channel_id')}"
        ), inline=False)
        embed.add_field(name='💡 Suggestions', value=f"**Channel:** {ch('suggestion_channel_id')}", inline=False)
        trigger_count = len(await get_triggers(self.bot, interaction.guild_id))
        embed.add_field(name='🤖 Auto-Responder Triggers', value=f'**Active:** {trigger_count}', inline=False)
        embed.add_field(name='📋 Server Logging', value=f"**Channel:** {ch('mod_log_channel_id')}", inline=False)
        autorole_id = settings.get('autorole_id')
        embed.add_field(name='🎭 Autorole', value=f"**Role:** {f'<@&{autorole_id}>' if autorole_id else '*Not set*'}", inline=False)
        embed.add_field(name='📈 Leveling', value=(
            f"**Enabled:** {'Yes' if settings.get('leveling_enabled', 1) else 'No'}\n"
            f"**Level-up Channel:** {ch('levelup_channel_id') if settings.get('levelup_channel_id') else '*Same as message*'}"
        ), inline=False)
        embed.add_field(name='⭐ Starboard', value=(
            f"**Channel:** {ch('starboard_channel_id')}\n"
            f"**Threshold:** {settings.get('starboard_threshold') or DEFAULT_STARBOARD_THRESHOLD}\n"
            f"**Emoji:** {settings.get('starboard_emoji') or DEFAULT_STARBOARD_EMOJI}"
        ), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ServerEvents(bot))

    # Re-register persistent views so buttons keep working after a restart.
    bot.add_view(EventJoinView(bot))
    bot.add_view(GiveawayJoinView(bot))

    await _ensure_table(bot)
    for message_id in await get_all_role_panel_ids(bot):
        buttons = await get_role_panel_buttons(bot, message_id)
        if buttons:
            bot.add_view(build_role_panel_view(bot, buttons), message_id=message_id)
