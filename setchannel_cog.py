import discord
from discord import app_commands
from discord.ext import commands
import logging

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Registry: add new channel types here — nothing else to change
# ──────────────────────────────────────────────────────────────
CHANNEL_TYPES = {
    "stream_notifications": {
        "label": "📺 Stream Notifications",
        "description": "Where live stream alerts are posted",
        "get": lambda db, guild_id: db.get_notification_channel(guild_id),
        "set": lambda db, guild_id, channel_id: db.set_notification_channel(guild_id, channel_id),
        "success_msg": "✅ Stream notifications will now be sent to {channel}.",
    },
    "birthdays": {
        "label": "🎂 Birthday Announcements",
        "description": "Where birthday messages are posted at midnight",
        "get": lambda db, guild_id: db.get_birthday_channel(guild_id),
        "set": lambda db, guild_id, channel_id: db.set_birthday_channel(guild_id, channel_id),
        "success_msg": "✅ Birthday announcements will now be sent to {channel}.",
    },
    # Future example — just uncomment and fill in:
    # "welcome": {
    #     "label": "👋 Welcome Messages",
    #     "description": "Where new member greetings are posted",
    #     "get": lambda db, guild_id: db.get_welcome_channel(guild_id),
    #     "set": lambda db, guild_id, channel_id: db.set_welcome_channel(guild_id, channel_id),
    #     "success_msg": "✅ Welcome messages will now be sent to {channel}.",
    # },
}


class ChannelTypeSelect(discord.ui.Select):
    def __init__(self, db):
        self.db = db
        options = [
            discord.SelectOption(
                label=info["label"],
                value=key,
                description=info["description"],
            )
            for key, info in CHANNEL_TYPES.items()
        ]
        super().__init__(
            placeholder="What do you want to configure?",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_key = self.values[0]
        config = CHANNEL_TYPES[selected_key]

        # Show current channel if already set
        current_id = config["get"](self.db, interaction.guild_id)
        current_str = f"<#{current_id}>" if current_id else "Not set"

        view = ChannelPickerView(self.db, selected_key, config)
        await interaction.response.edit_message(
            content=(
                f"**{config['label']}**\n"
                f"Currently set to: {current_str}\n\n"
                f"Pick a channel below:"
            ),
            view=view,
        )


class ChannelPickerView(discord.ui.View):
    def __init__(self, db, type_key: str, config: dict):
        super().__init__(timeout=60)
        self.db = db
        self.type_key = type_key
        self.config = config
        self.add_item(ChannelSelect(db, type_key, config))

        back_btn = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        back_btn.callback = self.go_back
        self.add_item(back_btn)

    async def go_back(self, interaction: discord.Interaction):
        view = SetChannelView(self.db)
        await interaction.response.edit_message(
            content="Which channel would you like to configure?",
            view=view,
        )


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, db, type_key: str, config: dict):
        super().__init__(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.db = db
        self.type_key = type_key
        self.config = config

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        self.config["set"](self.db, interaction.guild_id, channel.id)

        success = self.config["success_msg"].format(channel=channel.mention)
        await interaction.response.edit_message(
            content=f"{success}\n\nRun `/setchannel` again to configure another channel.",
            view=None,
        )
        logger.info(
            f"[setchannel] {self.type_key} set to #{channel.name} "
            f"(ID: {channel.id}) in guild {interaction.guild_id} "
            f"by {interaction.user} ({interaction.user.id})"
        )


class SetChannelView(discord.ui.View):
    def __init__(self, db):
        super().__init__(timeout=60)
        self.add_item(ChannelTypeSelect(db))


class SetChannelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="setchannel",
        description="Configure notification channels (stream alerts, birthdays, and more)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def setchannel(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need 'Manage Server' permission to use this command.",
                ephemeral=True,
            )
            return

        view = SetChannelView(self.bot.db)
        await interaction.response.send_message(
            "Which channel would you like to configure?",
            view=view,
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(SetChannelCog(bot))
