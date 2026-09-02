from __future__ import annotations
import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE, PURPLE_DARK, ORANGE, GREEN, BLUE
import utils.embeds as E
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.server_events')

# ═══════════════════════════════════════════════════════════════════════════════
#  Ek hi cog me: Announcements, Tournaments, Events, aur Welcome messages.
#  Sab kuch is file ke apne table (server_event_settings) me store hota hai —
#  tier_test.py ya ticket system ki kisi bhi table ko yeh file touch nahi
#  karti, bilkul alag/self-contained hai (same pattern jaisa tier_test.py
#  me use hua hai).
# ═══════════════════════════════════════════════════════════════════════════════

WELCOME_PLACEHOLDER_HELP = (
    'Placeholders you can use in the message:\n'
    '`{member}` — mentions the new member\n'
    '`{member_name}` — their display name (no ping)\n'
    '`{server}` — this server\'s name\n'
    '`{membercount}` — total member count after they joined'
)

DEFAULT_WELCOME_MESSAGE = 'Welcome {member} to **{server}**! 🎉 You are member #{membercount}.'


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
    if getattr(bot, '_server_events_table_ready', False):
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
                announcement_channel_id   INTEGER,
                announcement_ping_role_id INTEGER,
                event_log_channel_id      INTEGER
            )
        ''')
        await db.commit()
    bot._server_events_table_ready = True


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
        'welcome_enabled': 0,
        'welcome_channel_id': None,
        'welcome_message': None,
        'welcome_banner_url': None,
        'announcement_channel_id': None,
        'announcement_ping_role_id': None,
        'event_log_channel_id': None,
    }


async def update_setting(bot, guild_id: int, field: str, value):
    await _ensure_table(bot)
    async with _connect(bot) as db:
        await _apply_pragmas(db)
        await db.execute('INSERT OR IGNORE INTO server_event_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE server_event_settings SET {field} = ? WHERE guild_id = ?', (value, guild_id))
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _fill_welcome_placeholders(template: str, member: discord.Member) -> str:
    return (
        template
        .replace('{member}', member.mention)
        .replace('{member_name}', member.display_name)
        .replace('{server}', member.guild.name)
        .replace('{membercount}', str(member.guild.member_count))
    )


async def _log_action(bot, guild: discord.Guild, settings: dict, embed: discord.Embed):
    """Posts a copy of whatever announcement/tournament/event just went out
    to the configured 'server log' channel — same idea as tier_test.py's
    log channel, just for this cog's own actions."""
    log_channel_id = settings.get('event_log_channel_id')
    if not log_channel_id:
        return
    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        return
    try:
        await log_channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning(f'[server_events] Could not post to log channel {log_channel_id} in guild {guild.id}.')


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


class ServerEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── Welcome message on join ───────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        settings = await get_settings(self.bot, member.guild.id)
        if not settings.get('welcome_enabled') or not settings.get('welcome_channel_id'):
            return
        channel = member.guild.get_channel(settings['welcome_channel_id'])
        if not channel:
            return

        template = settings.get('welcome_message') or DEFAULT_WELCOME_MESSAGE
        text = _fill_welcome_placeholders(template, member)

        embed = discord.Embed(
            title='👋  New Member!',
            description=text,
            color=GREEN,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        banner_url = settings.get('welcome_banner_url')
        if banner_url:
            embed.set_image(url=banner_url)
        embed.set_footer(text=f'Member #{member.guild.member_count}')

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(f'[server_events] Could not send welcome message in guild {member.guild.id}.')

    # ── /setwelcomechannel ────────────────────────────────────────────────
    @app_commands.command(name='setwelcomechannel', description='(Admin/Owner only) Set the channel where new-member welcome messages are sent.')
    @app_commands.describe(channel='The channel to post welcome messages in')
    async def setwelcomechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_channel_id', channel.id)
        await update_setting(self.bot, interaction.guild_id, 'welcome_enabled', 1)
        await interaction.response.send_message(
            embed=E.success(f'Welcome messages will now be sent in {channel.mention}.'), ephemeral=True)

    # ── /setwelcomemessage ────────────────────────────────────────────────
    @app_commands.command(name='setwelcomemessage', description='(Admin/Owner only) Customize the welcome message text.')
    @app_commands.describe(message='Your custom message — see placeholders like {member}, {server}, {membercount}')
    async def setwelcomemessage(self, interaction: discord.Interaction, message: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_message', message)
        await interaction.response.send_message(
            embed=E.success(f'Welcome message updated!\n\n**Preview template:**\n{message}\n\n{WELCOME_PLACEHOLDER_HELP}'),
            ephemeral=True)

    # ── /setwelcomebanner ─────────────────────────────────────────────────
    @app_commands.command(name='setwelcomebanner', description='(Admin/Owner only) Set an image/banner shown on the welcome embed.')
    @app_commands.describe(image_url='Direct image URL (leave empty to remove the banner)')
    async def setwelcomebanner(self, interaction: discord.Interaction, image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_banner_url', image_url)
        msg = 'Welcome banner updated!' if image_url else 'Welcome banner removed.'
        await interaction.response.send_message(embed=E.success(msg), ephemeral=True)

    # ── /togglewelcome ────────────────────────────────────────────────────
    @app_commands.command(name='togglewelcome', description='(Admin/Owner only) Turn welcome messages on or off.')
    @app_commands.choices(state=[
        app_commands.Choice(name='On', value='on'),
        app_commands.Choice(name='Off', value='off'),
    ])
    async def togglewelcome(self, interaction: discord.Interaction, state: str):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'welcome_enabled', 1 if state == 'on' else 0)
        await interaction.response.send_message(
            embed=E.success(f'Welcome messages turned **{state.upper()}**.'), ephemeral=True)

    # ── /testwelcome ──────────────────────────────────────────────────────
    @app_commands.command(name='testwelcome', description='(Admin/Owner only) Preview the welcome message as if you just joined.')
    async def testwelcome(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        channel_id = settings.get('welcome_channel_id')
        if not channel_id:
            return await interaction.response.send_message(
                embed=E.error('No welcome channel is set yet. Set one first with `/setwelcomechannel`.'),
                ephemeral=True)
        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(
                embed=E.error('The configured welcome channel no longer exists.'), ephemeral=True)

        template = settings.get('welcome_message') or DEFAULT_WELCOME_MESSAGE
        text = _fill_welcome_placeholders(template, interaction.user)
        embed = discord.Embed(
            title='👋  New Member! (Test Preview)',
            description=text,
            color=GREEN,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        if settings.get('welcome_banner_url'):
            embed.set_image(url=settings['welcome_banner_url'])
        embed.set_footer(text=f'Member #{interaction.guild.member_count}  •  Test preview')
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message(
                embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        await interaction.response.send_message(embed=E.success(f'Test welcome message sent in {channel.mention}.'), ephemeral=True)

    # ── /setannouncechannel ───────────────────────────────────────────────
    @app_commands.command(name='setannouncechannel', description='(Admin/Owner only) Set the default channel used by /announce, /tournament and /event.')
    @app_commands.describe(channel='Default channel for announcements/tournaments/events')
    async def setannouncechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'announcement_channel_id', channel.id)
        await interaction.response.send_message(
            embed=E.success(f'Default announcement channel set to {channel.mention}.'), ephemeral=True)

    # ── /setannouncerole ──────────────────────────────────────────────────
    @app_commands.command(name='setannouncerole', description='(Admin/Owner only) Set a default role to ping on announcements/tournaments/events.')
    @app_commands.describe(role='Role to ping by default (leave empty to clear it)')
    async def setannouncerole(self, interaction: discord.Interaction, role: discord.Role | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'announcement_ping_role_id', role.id if role else None)
        msg = f'Default ping role set to {role.mention}.' if role else 'Default ping role cleared.'
        await interaction.response.send_message(embed=E.success(msg), ephemeral=True)

    # ── /seteventlog ──────────────────────────────────────────────────────
    @app_commands.command(name='seteventlog', description='(Admin/Owner only) Set the log channel where a copy of every announcement/tournament/event is posted.')
    @app_commands.describe(channel='Log channel (leave empty to disable logging)')
    async def seteventlog(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await update_setting(self.bot, interaction.guild_id, 'event_log_channel_id', channel.id if channel else None)
        msg = f'Event log channel set to {channel.mention}.' if channel else 'Event log channel disabled.'
        await interaction.response.send_message(embed=E.success(msg), ephemeral=True)

    # ── /announce ─────────────────────────────────────────────────────────
    @app_commands.command(name='announce', description='(Admin/Owner only) Post a server announcement.')
    @app_commands.describe(
        title='Announcement title',
        message='Announcement body',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)',
        image_url='Optional image/banner URL',
    )
    async def announce(self, interaction: discord.Interaction, title: str, message: str,
                        channel: discord.TextChannel | None = None,
                        ping_role: discord.Role | None = None,
                        image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(
                embed=E.error('No valid channel to post in — pick one or run this in a text channel.'),
                ephemeral=True)

        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        embed = discord.Embed(
            title=f'📢  {title}',
            description=message,
            color=BLUE,
            timestamp=datetime.now(timezone.utc)
        )
        if image_url:
            embed.set_image(url=image_url)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=f'Announced by {interaction.user.display_name}')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await target.send(content=content, embed=embed,
                               allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the announcement.'), ephemeral=True)

        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'Announcement posted in {target.mention}.'), ephemeral=True)

    # ── /tournament ───────────────────────────────────────────────────────
    @app_commands.command(name='tournament', description='(Admin/Owner only) Post a tournament announcement.')
    @app_commands.describe(
        name='Tournament name',
        description='Tournament details (format, rules, how to join, etc.)',
        date='Date of the tournament (e.g. "Sept 14, 2026")',
        time='Time of the tournament (e.g. "8 PM EST")',
        prize='Prize / reward (optional)',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)',
        image_url='Optional image/banner URL',
    )
    async def tournament(self, interaction: discord.Interaction, name: str, description: str,
                          date: str | None = None, time: str | None = None, prize: str | None = None,
                          channel: discord.TextChannel | None = None,
                          ping_role: discord.Role | None = None,
                          image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(
                embed=E.error('No valid channel to post in — pick one or run this in a text channel.'),
                ephemeral=True)

        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        lines = [description, '']
        if date:
            lines.append(f'📅 **Date:** {date}')
        if time:
            lines.append(f'⏰ **Time:** {time}')
        if prize:
            lines.append(f'🏆 **Prize:** {prize}')

        embed = discord.Embed(
            title=f'🏆  Tournament: {name}',
            description='\n'.join(lines),
            color=ORANGE,
            timestamp=datetime.now(timezone.utc)
        )
        if image_url:
            embed.set_image(url=image_url)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=f'Posted by {interaction.user.display_name}')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await target.send(content=content, embed=embed,
                               allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the tournament announcement.'), ephemeral=True)

        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'Tournament announcement posted in {target.mention}.'), ephemeral=True)

    # ── /event ────────────────────────────────────────────────────────────
    @app_commands.command(name='event', description='(Admin/Owner only) Post a server event announcement.')
    @app_commands.describe(
        name='Event name',
        description='Event details',
        date='Date of the event (e.g. "Sept 14, 2026")',
        time='Time of the event (e.g. "8 PM EST")',
        channel='Channel to post in (defaults to the configured announcement channel, else here)',
        ping_role='Role to ping (defaults to the configured default ping role, if any)',
        image_url='Optional image/banner URL',
    )
    async def event(self, interaction: discord.Interaction, name: str, description: str,
                     date: str | None = None, time: str | None = None,
                     channel: discord.TextChannel | None = None,
                     ping_role: discord.Role | None = None,
                     image_url: str | None = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_settings(self.bot, interaction.guild_id)
        target = _resolve_channel(interaction, settings, channel)
        if not target:
            return await interaction.response.send_message(
                embed=E.error('No valid channel to post in — pick one or run this in a text channel.'),
                ephemeral=True)

        role_id = ping_role.id if ping_role else settings.get('announcement_ping_role_id')
        content = f'<@&{role_id}>' if role_id else None

        lines = [description, '']
        if date:
            lines.append(f'📅 **Date:** {date}')
        if time:
            lines.append(f'⏰ **Time:** {time}')

        embed = discord.Embed(
            title=f'🎉  Event: {name}',
            description='\n'.join(lines),
            color=PURPLE,
            timestamp=datetime.now(timezone.utc)
        )
        if image_url:
            embed.set_image(url=image_url)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=f'Posted by {interaction.user.display_name}')

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await target.send(content=content, embed=embed,
                               allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=E.error("I don't have permission to send messages in that channel."), ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send(embed=E.error('Failed to post the event announcement.'), ephemeral=True)

        await _log_action(self.bot, interaction.guild, settings, embed.copy())
        await interaction.followup.send(embed=E.success(f'Event announcement posted in {target.mention}.'), ephemeral=True)

    # ── /serversettings — quick overview of everything configured here ────
    @app_commands.command(name='serversettings', description='(Admin/Owner only) View the current welcome/announcement settings.')
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

        embed = discord.Embed(
            title='⚙️  Server Events Settings',
            color=PURPLE_DARK,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name='👋 Welcome',
            value=(
                f"**Enabled:** {'Yes' if settings.get('welcome_enabled') else 'No'}\n"
                f"**Channel:** {ch('welcome_channel_id')}\n"
                f"**Message:** {settings.get('welcome_message') or '*Default*'}"
            ),
            inline=False
        )
        embed.add_field(
            name='📢 Announcements / Tournaments / Events',
            value=(
                f"**Default Channel:** {ch('announcement_channel_id')}\n"
                f"**Default Ping Role:** {rl('announcement_ping_role_id')}\n"
                f"**Event Log Channel:** {ch('event_log_channel_id')}"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ServerEvents(bot))
