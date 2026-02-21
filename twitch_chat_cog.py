import discord
from discord import app_commands
import logging
from config import BOT_OWNER_ID

logger = logging.getLogger(__name__)

COMMAND_LIMIT = 75


async def setup(discord_bot, twitch_chat_bot):
    """Register all Twitch slash commands directly on the bot tree"""

    # ------------------------------------------------------------------
    # /twitch setchannel
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="twitchset", description="Link this Discord server to your Twitch channel")
    @app_commands.describe(channel="Your Twitch channel name (e.g. ninja)")
    async def twitch_setchannel(interaction: discord.Interaction, channel: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        channel_name = channel.lower().strip().lstrip("@")

        user = await discord_bot.twitch.get_user(channel_name)
        if not user:
            await interaction.followup.send(f"❌ Could not find Twitch channel **{channel_name}**. Check the spelling.", ephemeral=True)
            return

        discord_bot.db.set_twitch_channel(interaction.guild_id, channel_name)

        if twitch_chat_bot:
            await twitch_chat_bot.join_channel(channel_name)

        await interaction.followup.send(
            f"✅ Linked to **{user['display_name']}** (twitch.tv/{channel_name})\n"
            f"The chat bot is now active in that channel!",
            ephemeral=True
        )

    # ------------------------------------------------------------------
    # /twitchremove
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="twitchremove", description="Unlink this server from its Twitch channel")
    async def twitch_removechannel(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked.", ephemeral=True)
            return

        channel_name = row["twitch_channel"]
        discord_bot.db.remove_twitch_channel(interaction.guild_id)

        others = discord_bot.db.get_guilds_for_twitch_channel(channel_name)
        if not others and twitch_chat_bot:
            await twitch_chat_bot.leave_channel(channel_name)

        await interaction.response.send_message(f"✅ Unlinked from **{channel_name}**.", ephemeral=True)

    # ------------------------------------------------------------------
    # /twitchstatus
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="twitchstatus", description="Show the linked Twitch channel for this server")
    async def twitch_status(interaction: discord.Interaction):
        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked. Use `/twitchset` to link one.", ephemeral=True)
            return

        channel_name = row["twitch_channel"]
        cmd_count = len(discord_bot.db.get_twitch_commands(channel_name))

        embed = discord.Embed(title="🟣 Twitch Chat Bot Status", color=0x9146FF)
        embed.add_field(name="Channel", value=f"twitch.tv/{channel_name}", inline=True)
        embed.add_field(name="Custom Commands", value=f"{cmd_count} / {COMMAND_LIMIT}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /twitchstats (owner only)
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="twitchstats", description="[Owner only] View all Twitch channels using the bot")
    async def twitch_stats(interaction: discord.Interaction):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ This command is restricted to the bot owner.", ephemeral=True)
            return

        channels = discord_bot.db.get_all_twitch_channels()
        if not channels:
            await interaction.response.send_message("📊 No Twitch channels registered.", ephemeral=True)
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
            cmds = discord_bot.db.get_twitch_commands(ch)
            count = len(cmds)
            total_commands += count
            warning = " ⚠️" if count >= COMMAND_LIMIT * 0.9 else ""
            lines.append(f"• **{ch}** — {count}/{COMMAND_LIMIT} commands{warning}")

        embed.add_field(name="Channels", value="\n".join(lines[:20]) or "None", inline=False)
        embed.set_footer(text=f"Total commands: {total_commands}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /cmdadd
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="cmdadd", description="Add a custom Twitch chat command")
    @app_commands.describe(
        command="Command name (include the ! e.g. !lurk)",
        response="Response text. Use $user, $game, $uptime, $viewers, $count, $channel",
        permission="Who can trigger it",
        cooldown="Cooldown in seconds (default 0)"
    )
    @app_commands.choices(permission=[
        app_commands.Choice(name="Everyone", value="everyone"),
        app_commands.Choice(name="Subscribers & above", value="subscriber"),
        app_commands.Choice(name="Mods & above", value="mod"),
        app_commands.Choice(name="Broadcaster only", value="broadcaster"),
    ])
    async def cmd_add(interaction: discord.Interaction, command: str, response: str, permission: str = "everyone", cooldown: int = 0):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked. Use `/twitchset` first.", ephemeral=True)
            return

        channel_name = row["twitch_channel"]
        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        existing_cmds = discord_bot.db.get_twitch_commands(channel_name)
        current_cmd = discord_bot.db.get_twitch_command(channel_name, command)
        if not current_cmd and len(existing_cmds) >= COMMAND_LIMIT:
            await interaction.response.send_message(f"❌ You've reached the {COMMAND_LIMIT} command limit. Remove one first.", ephemeral=True)
            return

        cooldown = max(0, min(cooldown, 3600))
        discord_bot.db.add_twitch_command(channel_name, command, response, permission, cooldown)

        action = "Updated" if current_cmd else "Added"
        await interaction.response.send_message(
            f"✅ **{action}** `{command}`\n**Response:** {response}\n**Permission:** {permission} | **Cooldown:** {cooldown}s",
            ephemeral=True
        )

    # ------------------------------------------------------------------
    # /cmdremove
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="cmdremove", description="Remove a custom Twitch chat command")
    @app_commands.describe(command="Command name to remove (e.g. !lurk)")
    async def cmd_remove(interaction: discord.Interaction, command: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked.", ephemeral=True)
            return

        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        removed = discord_bot.db.remove_twitch_command(row["twitch_channel"], command)
        if removed:
            await interaction.response.send_message(f"✅ Removed `{command}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Command `{command}` not found.", ephemeral=True)

    # ------------------------------------------------------------------
    # /cmdedit
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="cmdedit", description="Edit an existing Twitch chat command")
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
    async def cmd_edit(interaction: discord.Interaction, command: str, response: str = None, permission: str = None, cooldown: int = None):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked.", ephemeral=True)
            return

        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        existing = discord_bot.db.get_twitch_command(row["twitch_channel"], command)
        if not existing:
            await interaction.response.send_message(f"❌ Command `{command}` not found. Use `/cmdadd` to create it.", ephemeral=True)
            return

        new_response = response if response is not None else existing["response"]
        new_permission = permission if permission is not None else existing["permission"]
        new_cooldown = cooldown if cooldown is not None else existing["cooldown_seconds"]
        new_cooldown = max(0, min(new_cooldown, 3600))

        discord_bot.db.add_twitch_command(row["twitch_channel"], command, new_response, new_permission, new_cooldown)
        await interaction.response.send_message(
            f"✅ Updated `{command}`\n**Response:** {new_response}\n**Permission:** {new_permission} | **Cooldown:** {new_cooldown}s",
            ephemeral=True
        )

    # ------------------------------------------------------------------
    # /cmdlist
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="cmdlist", description="List all custom Twitch chat commands")
    async def cmd_list(interaction: discord.Interaction):
        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked. Use `/twitchset` first.", ephemeral=True)
            return

        channel_name = row["twitch_channel"]
        cmds = discord_bot.db.get_twitch_commands(channel_name)

        if not cmds:
            await interaction.response.send_message(f"📋 No custom commands yet. Add one with `/cmdadd`!", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 Commands for #{channel_name}",
            description=f"{len(cmds)} / {COMMAND_LIMIT} commands used",
            color=0x9146FF
        )

        lines = []
        for cmd in cmds:
            cd = f"{cmd['cooldown_seconds']}s cd" if cmd["cooldown_seconds"] > 0 else "no cd"
            uses = cmd.get("use_count", 0)
            lines.append(f"`{cmd['command_name']}` — {cmd['permission']} | {cd} | {uses} uses")

        embed.add_field(name="Commands", value="\n".join(lines) or "None", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /cmdinfo
    # ------------------------------------------------------------------
    @discord_bot.tree.command(name="cmdinfo", description="Show details about a specific Twitch command")
    @app_commands.describe(command="Command name (e.g. !lurk)")
    async def cmd_info(interaction: discord.Interaction, command: str):
        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked.", ephemeral=True)
            return

        if not command.startswith("!"):
            command = "!" + command
        command = command.lower().strip()

        cmd = discord_bot.db.get_twitch_command(row["twitch_channel"], command)
        if not cmd:
            await interaction.response.send_message(f"❌ Command `{command}` not found.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Command: {command}", color=0x9146FF)
        embed.add_field(name="Response", value=cmd["response"], inline=False)
        embed.add_field(name="Permission", value=cmd["permission"], inline=True)
        embed.add_field(name="Cooldown", value=f"{cmd['cooldown_seconds']}s", inline=True)
        embed.add_field(name="Times Used", value=str(cmd.get("use_count", 0)), inline=True)
        embed.add_field(name="Channel", value=f"#{row['twitch_channel']}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    logger.info("Twitch chat commands registered")
