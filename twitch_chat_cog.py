import discord
from discord import app_commands
from discord.ext import commands
import logging
from config import BOT_OWNER_ID

logger = logging.getLogger(__name__)

COMMAND_LIMIT = 75

VALID_PERMISSIONS = ["everyone", "subscriber", "mod", "broadcaster"]


class TwitchChatCog(commands.Cog):
    """Discord slash commands for managing the Twitch chat bot"""

    def __init__(self, discord_bot, twitch_chat_bot):
        self.bot = discord_bot          # The Discord bot instance
        self.twitch = twitch_chat_bot   # The twitchio bot instance

    # ------------------------------------------------------------------
    # /twitch group
    # ------------------------------------------------------------------

    twitch_group = app_commands.Group(
        name="twitch",
        description="Manage your Twitch chat bot"
    )

    @twitch_group.command(name="setchannel", description="Link this Discord server to your Twitch channel")
    @app_commands.describe(channel="Your Twitch channel name (e.g. ninja)")
    async def twitch_setchannel(self, interaction: discord.Interaction, channel: str):
        """Set the Twitch channel for this Discord server. Manage Server required."""
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need 'Manage Server' permission to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        channel_name = channel.lower().strip().lstrip("@")

        # Verify the channel exists on Twitch
        user = await self.bot.twitch.get_user(channel_name)
        if not user:
            await interaction.followup.send(
                f"❌ Could not find Twitch channel **{channel_name}**. Check the spelling.",
                ephemeral=True
            )
            return

        # Save to DB
        self.bot.db.set_twitch_channel(interaction.guild_id, channel_name)

        # Bot joins the channel live
        await self.twitch.join_channel(channel_name)

        await interaction.followup.send(
            f"✅ Linked to Twitch channel **{user['display_name']}** (twitch.tv/{channel_name})\n"
            f"The chat bot is now active in that channel!",
            ephemeral=True
        )

    @twitch_group.command(name="removechannel", description="Unlink this server from its Twitch channel")
    async def twitch_removechannel(self, interaction: discord.Interaction):
        """Remove the Twitch channel link for this server."""
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need 'Manage Server' permission to use this command.",
                ephemeral=True
            )
            return

        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel is linked to this server.",
                ephemeral=True
            )
            return

        channel_name = row["twitch_channel"]
        self.bot.db.remove_twitch_channel(interaction.guild_id)

        # Check if any other server still uses this channel before leaving
        others = self.bot.db.get_guilds_for_twitch_channel(channel_name)
        if not others:
            await self.twitch.leave_channel(channel_name)

        await interaction.response.send_message(
            f"✅ Unlinked from **{channel_name}**. The chat bot has left that channel.",
            ephemeral=True
        )

    @twitch_group.command(name="status", description="Show the linked Twitch channel for this server")
    async def twitch_status(self, interaction: discord.Interaction):
        """Show which Twitch channel this server is linked to."""
        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel linked. Use `/twitch setchannel` to link one.",
                ephemeral=True
            )
            return

        channel_name = row["twitch_channel"]
        cmd_count = len(self.bot.db.get_twitch_commands(channel_name))

        embed = discord.Embed(
            title="🟣 Twitch Chat Bot Status",
            color=0x9146FF
        )
        embed.add_field(name="Channel", value=f"twitch.tv/{channel_name}", inline=True)
        embed.add_field(name="Custom Commands", value=f"{cmd_count} / {COMMAND_LIMIT}", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @twitch_group.command(name="stats", description="[Owner only] View all Twitch channels using the bot")
    async def twitch_stats(self, interaction: discord.Interaction):
        """Owner-only: See all channels using the Twitch bot and their command counts."""
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner.",
                ephemeral=True
            )
            return

        channels = self.bot.db.get_all_twitch_channels()

        if not channels:
            await interaction.response.send_message(
                "📊 No Twitch channels are currently registered.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📊 Twitch Bot Stats",
            description=f"**{len(channels)}** channel(s) using the Twitch bot",
            color=0x9146FF
        )

        total_commands = 0
        lines = []
        for row in channels:
            ch = row["twitch_channel"]
            cmds = self.bot.db.get_twitch_commands(ch)
            count = len(cmds)
            total_commands += count
            warning = " ⚠️" if count >= COMMAND_LIMIT * 0.9 else ""
            lines.append(f"• **{ch}** — {count}/{COMMAND_LIMIT} commands{warning}")

        # Discord field limit is 1024 chars — chunk if needed
        chunk = ""
        field_num = 1
        for line in lines:
            if len(chunk) + len(line) + 1 > 1000:
                embed.add_field(
                    name=f"Channels" if field_num == 1 else f"Channels (cont.)",
                    value=chunk,
                    inline=False
                )
                chunk = line + "\n"
                field_num += 1
            else:
                chunk += line + "\n"

        if chunk:
            embed.add_field(
                name="Channels" if field_num == 1 else "Channels (cont.)",
                value=chunk,
                inline=False
            )

        embed.set_footer(text=f"Total commands across all channels: {total_commands}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /cmd group
    # ------------------------------------------------------------------

    cmd_group = app_commands.Group(
        name="cmd",
        description="Manage Twitch chat commands"
    )

    @cmd_group.command(name="add", description="Add a custom Twitch chat command")
    @app_commands.describe(
        command="Command name (include the ! e.g. !lurk)",
        response="What the bot says. Use $user, $uptime, $game, $viewers, $count, $channel",
        permission="Who can trigger it (default: everyone)",
        cooldown="Cooldown in seconds between uses (default: 0)"
    )
    @app_commands.choices(permission=[
        app_commands.Choice(name="Everyone", value="everyone"),
        app_commands.Choice(name="Subscribers & above", value="subscriber"),
        app_commands.Choice(name="Mods & above", value="mod"),
        app_commands.Choice(name="Broadcaster only", value="broadcaster"),
    ])
    async def cmd_add(
        self,
        interaction: discord.Interaction,
        command: str,
        response: str,
        permission: str = "everyone",
        cooldown: int = 0
    ):
        """Add a custom Twitch chat command for this server's linked channel."""
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need 'Manage Server' permission to use this command.",
                ephemeral=True
            )
            return

        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel linked. Use `/twitch setchannel` first.",
                ephemeral=True
            )
            return

        channel_name = row["twitch_channel"]

        # Normalise command name
        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        # Check command count limit
        existing = self.bot.db.get_twitch_commands(channel_name)
        current_cmd = self.bot.db.get_twitch_command(channel_name, command)
        if not current_cmd and len(existing) >= COMMAND_LIMIT:
            await interaction.response.send_message(
                f"❌ You've reached the limit of **{COMMAND_LIMIT}** custom commands.\n"
                f"Remove one with `/cmd remove` before adding more.",
                ephemeral=True
            )
            return

        # Validate cooldown
        cooldown = max(0, min(cooldown, 3600))

        self.bot.db.add_twitch_command(channel_name, command, response, permission, cooldown)

        action = "Updated" if current_cmd else "Added"
        await interaction.response.send_message(
            f"✅ **{action}** `{command}` for #{channel_name}\n"
            f"**Response:** {response}\n"
            f"**Permission:** {permission} | **Cooldown:** {cooldown}s",
            ephemeral=True
        )

    @cmd_group.command(name="remove", description="Remove a custom Twitch chat command")
    @app_commands.describe(command="Command name to remove (e.g. !lurk)")
    async def cmd_remove(self, interaction: discord.Interaction, command: str):
        """Remove a custom Twitch command."""
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need 'Manage Server' permission to use this command.",
                ephemeral=True
            )
            return

        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel linked.",
                ephemeral=True
            )
            return

        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        removed = self.bot.db.remove_twitch_command(row["twitch_channel"], command)
        if removed:
            await interaction.response.send_message(
                f"✅ Removed command `{command}`",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ Command `{command}` not found.",
                ephemeral=True
            )

    @cmd_group.command(name="edit", description="Edit an existing Twitch chat command")
    @app_commands.describe(
        command="Command to edit (e.g. !lurk)",
        response="New response text",
        permission="New permission level",
        cooldown="New cooldown in seconds"
    )
    @app_commands.choices(permission=[
        app_commands.Choice(name="Everyone", value="everyone"),
        app_commands.Choice(name="Subscribers & above", value="subscriber"),
        app_commands.Choice(name="Mods & above", value="mod"),
        app_commands.Choice(name="Broadcaster only", value="broadcaster"),
    ])
    async def cmd_edit(
        self,
        interaction: discord.Interaction,
        command: str,
        response: str = None,
        permission: str = None,
        cooldown: int = None
    ):
        """Edit an existing command. Only provide the fields you want to change."""
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ You need 'Manage Server' permission to use this command.",
                ephemeral=True
            )
            return

        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel linked.",
                ephemeral=True
            )
            return

        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        existing = self.bot.db.get_twitch_command(row["twitch_channel"], command)
        if not existing:
            await interaction.response.send_message(
                f"❌ Command `{command}` not found. Use `/cmd add` to create it.",
                ephemeral=True
            )
            return

        # Use existing values for anything not provided
        new_response = response if response is not None else existing["response"]
        new_permission = permission if permission is not None else existing["permission"]
        new_cooldown = cooldown if cooldown is not None else existing["cooldown_seconds"]
        new_cooldown = max(0, min(new_cooldown, 3600))

        self.bot.db.add_twitch_command(row["twitch_channel"], command, new_response, new_permission, new_cooldown)

        await interaction.response.send_message(
            f"✅ Updated `{command}`\n"
            f"**Response:** {new_response}\n"
            f"**Permission:** {new_permission} | **Cooldown:** {new_cooldown}s",
            ephemeral=True
        )

    @cmd_group.command(name="list", description="List all custom Twitch chat commands")
    async def cmd_list(self, interaction: discord.Interaction):
        """Show all custom commands for this server's Twitch channel."""
        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel linked. Use `/twitch setchannel` first.",
                ephemeral=True
            )
            return

        channel_name = row["twitch_channel"]
        cmds = self.bot.db.get_twitch_commands(channel_name)

        if not cmds:
            await interaction.response.send_message(
                f"📋 No custom commands for **#{channel_name}** yet.\n"
                f"Add one with `/cmd add`!",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📋 Commands for #{channel_name}",
            description=f"{len(cmds)} / {COMMAND_LIMIT} commands used",
            color=0x9146FF
        )

        lines = []
        for cmd in cmds:
            perm = cmd["permission"]
            cd = f"{cmd['cooldown_seconds']}s cd" if cmd["cooldown_seconds"] > 0 else "no cd"
            uses = cmd.get("use_count", 0)
            lines.append(f"`{cmd['command_name']}` — {perm} | {cd} | {uses} uses")

        # Chunk into fields
        chunk = ""
        field_num = 1
        for line in lines:
            if len(chunk) + len(line) + 1 > 1000:
                embed.add_field(
                    name="Commands" if field_num == 1 else "Commands (cont.)",
                    value=chunk,
                    inline=False
                )
                chunk = line + "\n"
                field_num += 1
            else:
                chunk += line + "\n"

        if chunk:
            embed.add_field(
                name="Commands" if field_num == 1 else "Commands (cont.)",
                value=chunk,
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @cmd_group.command(name="info", description="Show details about a specific command")
    @app_commands.describe(command="Command name (e.g. !lurk)")
    async def cmd_info(self, interaction: discord.Interaction, command: str):
        """Show full details of a single command including response text."""
        row = self.bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                "❌ No Twitch channel linked.",
                ephemeral=True
            )
            return

        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        cmd = self.bot.db.get_twitch_command(row["twitch_channel"], command)
        if not cmd:
            await interaction.response.send_message(
                f"❌ Command `{command}` not found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Command: {command}",
            color=0x9146FF
        )
        embed.add_field(name="Response", value=cmd["response"], inline=False)
        embed.add_field(name="Permission", value=cmd["permission"], inline=True)
        embed.add_field(name="Cooldown", value=f"{cmd['cooldown_seconds']}s", inline=True)
        embed.add_field(name="Times Used", value=str(cmd.get("use_count", 0)), inline=True)
        embed.add_field(name="Channel", value=f"#{row['twitch_channel']}", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(discord_bot, twitch_chat_bot):
    """Add the cog to the Discord bot and register app command groups"""
    cog = TwitchChatCog(discord_bot, twitch_chat_bot)
    await discord_bot.add_cog(cog)
    discord_bot.tree.add_command(cog.twitch_group)
    discord_bot.tree.add_command(cog.cmd_group)
    logger.info("TwitchChatCog loaded")
