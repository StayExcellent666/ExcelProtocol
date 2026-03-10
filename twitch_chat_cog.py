import discord
from discord import app_commands
import logging
from config import BOT_OWNER_ID

logger = logging.getLogger(__name__)

COMMAND_LIMIT = 75
VALID_PERMISSIONS = ["everyone", "subscriber", "mod", "broadcaster"]


# ─────────────────────────────────────────────────────────────
# Modal — Add / Edit command
# ─────────────────────────────────────────────────────────────

class CmdModal(discord.ui.Modal):
    def __init__(self, db, channel_name: str, existing: dict = None):
        is_edit = existing is not None
        super().__init__(title="Edit Command" if is_edit else "New Command")
        self.db = db
        self.channel_name = channel_name
        self.existing = existing

        self.command_input = discord.ui.TextInput(
            label="Command name",
            placeholder="e.g. !lurk  (! added automatically if missing)",
            default=existing["command_name"] if is_edit else "",
            max_length=50,
        )
        self.response_input = discord.ui.TextInput(
            label="Response",
            placeholder="Use $user, $game, $uptime, $viewers, $count, $channel",
            default=existing["response"] if is_edit else "",
            style=discord.TextStyle.paragraph,
            max_length=400,
        )
        self.permission_input = discord.ui.TextInput(
            label="Permission (optional)",
            placeholder="everyone / subscriber / mod / broadcaster",
            default=existing["permission"] if is_edit else "everyone",
            required=False,
            max_length=20,
        )
        self.cooldown_input = discord.ui.TextInput(
            label="Cooldown in seconds (optional)",
            placeholder="0",
            default=str(existing["cooldown_seconds"]) if is_edit else "0",
            required=False,
            max_length=6,
        )

        self.add_item(self.command_input)
        self.add_item(self.response_input)
        self.add_item(self.permission_input)
        self.add_item(self.cooldown_input)

    async def on_submit(self, interaction: discord.Interaction):
        command = self.command_input.value.lower().strip()
        if not command.startswith("!"):
            command = "!" + command

        response = self.response_input.value.strip()

        permission = self.permission_input.value.lower().strip() or "everyone"
        if permission not in VALID_PERMISSIONS:
            await interaction.response.send_message(
                f"❌ Invalid permission `{permission}`. Must be: {', '.join(VALID_PERMISSIONS)}",
                ephemeral=True
            )
            return

        try:
            cooldown = max(0, min(int(self.cooldown_input.value or 0), 3600))
        except ValueError:
            cooldown = 0

        existing_cmds = self.db.get_twitch_commands(self.channel_name)
        current_cmd = self.db.get_twitch_command(self.channel_name, command)

        if not current_cmd and len(existing_cmds) >= COMMAND_LIMIT:
            await interaction.response.send_message(
                f"❌ You've reached the {COMMAND_LIMIT} command limit. Remove one first.",
                ephemeral=True
            )
            return

        self.db.add_twitch_command(self.channel_name, command, response, permission, cooldown)
        action = "Updated" if (current_cmd or self.existing) else "Added"

        await interaction.response.send_message(
            f"✅ **{action}** `{command}`\n"
            f"**Response:** {response}\n"
            f"**Permission:** {permission} | **Cooldown:** {cooldown}s",
            ephemeral=True
        )


# ─────────────────────────────────────────────────────────────
# Dropdown — pick existing command to edit or create new
# ─────────────────────────────────────────────────────────────

class CmdSelect(discord.ui.Select):
    def __init__(self, db, channel_name: str, cmds: list):
        self.db = db
        self.channel_name = channel_name

        options = [discord.SelectOption(label="➕ New Command", value="__new__", description="Create a brand new command")]
        for cmd in cmds[:24]:
            cd = f" | {cmd['cooldown_seconds']}s cd" if cmd["cooldown_seconds"] > 0 else ""
            options.append(discord.SelectOption(
                label=cmd["command_name"],
                description=f"{cmd['permission']}{cd} | {cmd['response'][:50]}",
                value=cmd["command_name"]
            ))

        super().__init__(placeholder="Choose a command to edit, or create new...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__new__":
            modal = CmdModal(self.db, self.channel_name)
        else:
            existing = self.db.get_twitch_command(self.channel_name, self.values[0])
            modal = CmdModal(self.db, self.channel_name, existing=existing)
        await interaction.response.send_modal(modal)


class CmdView(discord.ui.View):
    def __init__(self, db, channel_name: str, cmds: list):
        super().__init__(timeout=60)
        self.add_item(CmdSelect(db, channel_name, cmds))


# ─────────────────────────────────────────────────────────────
# Dropdown — pick command to remove
# ─────────────────────────────────────────────────────────────

class CmdRemoveSelect(discord.ui.Select):
    def __init__(self, db, channel_name: str, cmds: list):
        self.db = db
        self.channel_name = channel_name

        options = [
            discord.SelectOption(
                label=cmd["command_name"],
                description=f"{cmd['permission']} | {cmd['response'][:60]}",
                value=cmd["command_name"]
            )
            for cmd in cmds[:25]
        ]
        super().__init__(placeholder="Choose a command to remove...", options=options)

    async def callback(self, interaction: discord.Interaction):
        command = self.values[0]
        self.db.remove_twitch_command(self.channel_name, command)
        await interaction.response.edit_message(
            content=f"🗑️ Removed `{command}`.",
            view=None
        )


class CmdRemoveView(discord.ui.View):
    def __init__(self, db, channel_name: str, cmds: list):
        super().__init__(timeout=60)
        self.add_item(CmdRemoveSelect(db, channel_name, cmds))


# ─────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────

async def setup(discord_bot, twitch_chat_bot):
    """Register all Twitch slash commands directly on the bot tree"""

    # ------------------------------------------------------------------
    # /twitchset
    # ------------------------------------------------------------------
    @app_commands.default_permissions(manage_guild=True)
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
    @app_commands.default_permissions(manage_guild=True)
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
        embed_color = discord_bot.db.get_embed_color(interaction.guild_id)

        embed = discord.Embed(title="🟣 Twitch Chat Bot Status", color=embed_color)
        embed.add_field(name="Channel", value=f"twitch.tv/{channel_name}", inline=True)
        embed.add_field(name="Custom Commands", value=f"{cmd_count} / {COMMAND_LIMIT}", inline=True)
        embed.add_field(name="Default Commands", value="`!commands` — lists all active commands in chat", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /twitchstats (owner only)
    # ------------------------------------------------------------------
    @app_commands.default_permissions(administrator=True)
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
    # /cmd — unified add/edit with dropdown + modal
    # ------------------------------------------------------------------
    @app_commands.default_permissions(manage_guild=True)
    @discord_bot.tree.command(name="cmd", description="Add or edit a custom Twitch chat command")
    async def cmd(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked. Use `/twitchset` first.", ephemeral=True)
            return

        channel_name = row["twitch_channel"]
        cmds = discord_bot.db.get_twitch_commands(channel_name)

        view = CmdView(discord_bot.db, channel_name, cmds)
        await interaction.response.send_message(
            f"📋 **{len(cmds)}/{COMMAND_LIMIT}** commands set for **#{channel_name}**\n"
            f"Select an existing command to view/edit, or choose **➕ New Command**:",
            view=view,
            ephemeral=True
        )

    # ------------------------------------------------------------------
    # /cmdremove — dropdown to pick which command to delete
    # ------------------------------------------------------------------
    @app_commands.default_permissions(manage_guild=True)
    @discord_bot.tree.command(name="cmdremove", description="Remove a custom Twitch chat command")
    async def cmd_remove(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
            return

        row = discord_bot.db.get_twitch_channel(interaction.guild_id)
        if not row:
            await interaction.response.send_message("❌ No Twitch channel linked.", ephemeral=True)
            return

        channel_name = row["twitch_channel"]
        cmds = discord_bot.db.get_twitch_commands(channel_name)

        if not cmds:
            await interaction.response.send_message("📋 No commands to remove.", ephemeral=True)
            return

        view = CmdRemoveView(discord_bot.db, channel_name, cmds)
        await interaction.response.send_message(
            "Select a command to remove:",
            view=view,
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
        embed_color = discord_bot.db.get_embed_color(interaction.guild_id)

        if not cmds:
            await interaction.response.send_message("📋 No custom commands yet. Add one with `/cmd`!", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 Commands for #{channel_name}",
            description=f"{len(cmds)} / {COMMAND_LIMIT} commands used",
            color=embed_color
        )

        lines = []
        for cmd_row in cmds:
            cd = f"{cmd_row['cooldown_seconds']}s cd" if cmd_row["cooldown_seconds"] > 0 else "no cd"
            uses = cmd_row.get("use_count", 0)
            lines.append(f"`{cmd_row['command_name']}` — {cmd_row['permission']} | {cd} | {uses} uses")

        # Split into multiple fields if over 1024 chars
        current_field = []
        current_length = 0
        field_num = 1
        for line in lines:
            if current_length + len(line) + 1 > 1000 and current_field:
                label = "Commands" if field_num == 1 else f"Commands (cont. {field_num})"
                embed.add_field(name=label, value="\n".join(current_field), inline=False)
                current_field = [line]
                current_length = len(line)
                field_num += 1
            else:
                current_field.append(line)
                current_length += len(line) + 1
        if current_field:
            label = "Commands" if field_num == 1 else f"Commands (cont. {field_num})"
            embed.add_field(name=label, value="\n".join(current_field), inline=False)

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

        embed_color = discord_bot.db.get_embed_color(interaction.guild_id)
        embed = discord.Embed(title=f"Command: {command}", color=embed_color)
        embed.add_field(name="Response", value=cmd["response"], inline=False)
        embed.add_field(name="Permission", value=cmd["permission"], inline=True)
        embed.add_field(name="Cooldown", value=f"{cmd['cooldown_seconds']}s", inline=True)
        embed.add_field(name="Times Used", value=str(cmd.get("use_count", 0)), inline=True)
        embed.add_field(name="Channel", value=f"#{row['twitch_channel']}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    logger.info("Twitch chat commands registered")
