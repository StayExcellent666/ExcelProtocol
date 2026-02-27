import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)


class BirthdaySetModal(discord.ui.Modal, title="Set Birthday"):
    def __init__(self, target_user: discord.Member, db):
        super().__init__()
        self.target_user = target_user
        self.db = db

    day = discord.ui.TextInput(label="Day", placeholder="e.g. 15", min_length=1, max_length=2)
    month = discord.ui.TextInput(label="Month (number)", placeholder="e.g. 6 for June", min_length=1, max_length=2)
    year = discord.ui.TextInput(label="Year of birth", placeholder="e.g. 1995", min_length=4, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            day = int(self.day.value)
            month = int(self.month.value)
            year = int(self.year.value)
            birthday = datetime(year=year, month=month, day=day)
        except ValueError:
            await interaction.response.send_message("❌ Invalid date. Please check the day, month, and year.", ephemeral=True)
            return

        now = datetime.now()
        age = now.year - year - ((now.month, now.day) < (month, day))
        if age < 0 or age > 130:
            await interaction.response.send_message("❌ That doesn't look like a valid birth year.", ephemeral=True)
            return

        self.db.set_birthday(guild_id=interaction.guild.id, user_id=self.target_user.id, day=day, month=month, year=year)

        if self.target_user.id == interaction.user.id:
            msg = f"🎂 Your birthday has been set to **{birthday.strftime('%B %d, %Y')}**!"
        else:
            msg = f"🎂 Birthday for {self.target_user.mention} set to **{birthday.strftime('%B %d, %Y')}**!"

        await interaction.response.send_message(msg, ephemeral=True)


class BirthdayChecker:
    """Handles the birthday check loop. Works with plain discord.Client."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._last_birthday_date: date | None = None

    def start(self):
        self._loop.start()

    @tasks.loop(hours=1)
    async def _loop(self):
        now = datetime.utcnow()
        today = now.date()
        if now.hour == 6 and self._last_birthday_date != today:
            await self._send_notifications(today)

    @_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()
        now = datetime.utcnow()
        today = now.date()
        if now.hour == 6 and self._last_birthday_date != today:
            logger.info("Bot started during birthday window — running startup catch-up")
            await self._send_notifications(today)

    async def _send_notifications(self, today: date):
        for guild in self.bot.guilds:
            channel_id = self.db.get_birthday_channel(guild.id)
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            birthdays = self.db.get_birthdays_on(guild_id=guild.id, month=today.month, day=today.day)
            for b in birthdays:
                member = guild.get_member(b["user_id"])
                if not member:
                    continue
                age = today.year - b["year"]
                try:
                    await channel.send(
                        f"🎂 It's {member.mention}'s birthday today! "
                        f"They are turning **{age}** years old! Happy Birthday! 🎉"
                    )
                except Exception as e:
                    logger.error(f"Failed to send birthday message in guild {guild.id}: {e}")
        self._last_birthday_date = today
        logger.info(f"Birthday notifications sent for {today}")


def _is_mod_or_admin(member: discord.Member) -> bool:
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or any(r.permissions.manage_messages for r in member.roles)
    )


async def setup(discord_bot):
    checker = BirthdayChecker(discord_bot)
    checker.start()

    @discord_bot.tree.command(name="birthday", description="Set a birthday — yours, or another user's (mods/admins only)")
    @app_commands.describe(user="The user whose birthday to set (mods/admins only)")
    async def birthday(interaction: discord.Interaction, user: discord.Member = None):
        if user is None or user.id == interaction.user.id:
            target = interaction.user
        else:
            if not _is_mod_or_admin(interaction.user):
                await interaction.response.send_message("❌ Only moderators and admins can set another user's birthday.", ephemeral=True)
                return
            target = user
        modal = BirthdaySetModal(target_user=target, db=discord_bot.db)
        await interaction.response.send_modal(modal)

    @discord_bot.tree.command(name="birthdayremove", description="Remove a birthday entry (yours, or another user's if mod/admin)")
    @app_commands.describe(user="The user whose birthday to remove (mods/admins only)")
    async def birthdayremove(interaction: discord.Interaction, user: discord.Member = None):
        if user is None or user.id == interaction.user.id:
            target = interaction.user
        else:
            if not _is_mod_or_admin(interaction.user):
                await interaction.response.send_message("❌ Only moderators and admins can remove another user's birthday.", ephemeral=True)
                return
            target = user
        discord_bot.db.remove_birthday(guild_id=interaction.guild.id, user_id=target.id)
        await interaction.response.send_message(f"🗑️ Birthday for **{target.display_name}** has been removed.", ephemeral=True)

    @discord_bot.tree.command(name="birthdaylist", description="View all birthdays in this server (mods/admins only)")
    async def birthdaylist(interaction: discord.Interaction):
        if not _is_mod_or_admin(interaction.user):
            await interaction.response.send_message("❌ Only moderators and admins can view the birthday list.", ephemeral=True)
            return
        birthdays = discord_bot.db.get_all_birthdays(guild_id=interaction.guild.id)
        if not birthdays:
            await interaction.response.send_message("No birthdays have been set yet.", ephemeral=True)
            return
        birthdays.sort(key=lambda b: (b["month"], b["day"]))
        lines = []
        for b in birthdays:
            member = interaction.guild.get_member(b["user_id"])
            name = member.display_name if member else f"Unknown ({b['user_id']})"
            dt = datetime(year=b["year"], month=b["month"], day=b["day"])
            lines.append(f"**{name}** — {dt.strftime('%B %d, %Y')}")
        embed = discord.Embed(title="🎂 Server Birthdays", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    logger.info("Birthday commands registered")
