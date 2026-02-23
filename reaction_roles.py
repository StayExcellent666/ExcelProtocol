import discord
from discord import app_commands
import json
import os
import logging

logger = logging.getLogger(__name__)

RR_DATA_PATH = "/data/reaction_roles.json"

# {user_id: {guild_id, channel_id, title, type, only_add, max_roles, roles, editing_message_id}}
_sessions: dict[int, dict] = {}


# ------------------------------------------------------------------
# Data persistence
# ------------------------------------------------------------------

def _load_data() -> dict:
    if os.path.exists(RR_DATA_PATH):
        try:
            with open(RR_DATA_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_data(data: dict):
    os.makedirs(os.path.dirname(RR_DATA_PATH), exist_ok=True)
    with open(RR_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ------------------------------------------------------------------
# Modals
# ------------------------------------------------------------------

class CreateSettingsModal(discord.ui.Modal, title="Create Reaction Role"):
    rr_title = discord.ui.TextInput(
        label="Title",
        placeholder="e.g. Choose the games you play",
        max_length=100
    )
    only_add = discord.ui.TextInput(
        label="Only Add? (true/false)",
        placeholder="false",
        default="false",
        max_length=5,
        required=False
    )
    max_roles = discord.ui.TextInput(
        label="Max roles a user can pick (0 = unlimited)",
        placeholder="0",
        default="0",
        max_length=2,
        required=False
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        only_add_val = self.only_add.value.strip().lower() == "true"
        try:
            max_val = int(self.max_roles.value.strip())
            max_val = max_val if max_val > 0 else None
        except ValueError:
            max_val = None

        _sessions[interaction.user.id] = {
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "title": self.rr_title.value.strip(),
            "type": "dropdown",  # default, user picks next
            "only_add": only_add_val,
            "max_roles": max_val,
            "roles": [],
            "editing_message_id": None
        }

        # Ask for type
        view = discord.ui.View()
        dropdown_btn = discord.ui.Button(label="Dropdown", style=discord.ButtonStyle.primary, emoji="📋")
        buttons_btn = discord.ui.Button(label="Buttons", style=discord.ButtonStyle.secondary, emoji="🔘")

        async def set_dropdown(i: discord.Interaction):
            _sessions[interaction.user.id]["type"] = "dropdown"
            await i.response.edit_message(
                content=f"✅ **Session started!**\n**Title:** {self.rr_title.value}\n**Type:** Dropdown | **Only Add:** {only_add_val} | **Max:** {max_val or 'unlimited'}\n\nNow use `/rr addrole` to add roles, then `/rr publish` when ready.",
                view=None
            )

        async def set_buttons(i: discord.Interaction):
            _sessions[interaction.user.id]["type"] = "buttons"
            await i.response.edit_message(
                content=f"✅ **Session started!**\n**Title:** {self.rr_title.value}\n**Type:** Buttons | **Only Add:** {only_add_val} | **Max:** {max_val or 'unlimited'}\n\nNow use `/rr addrole` to add roles, then `/rr publish` when ready.",
                view=None
            )

        dropdown_btn.callback = set_dropdown
        buttons_btn.callback = set_buttons
        view.add_item(dropdown_btn)
        view.add_item(buttons_btn)

        await interaction.response.send_message(
            f"✅ Settings saved! Now choose the selector type:",
            view=view,
            ephemeral=True
        )


class EditSettingsModal(discord.ui.Modal, title="Edit Reaction Role"):
    rr_title = discord.ui.TextInput(label="Title", max_length=100)
    only_add = discord.ui.TextInput(
        label="Only Add? (true/false)",
        max_length=5,
        required=False
    )
    max_roles = discord.ui.TextInput(
        label="Max roles a user can pick (0 = unlimited)",
        max_length=2,
        required=False
    )

    def __init__(self, entry: dict, message_id: str, bot):
        super().__init__()
        self.bot = bot
        self.message_id = message_id
        self.entry = entry
        # Pre-fill with current values
        self.rr_title.default = entry.get("title", "")
        self.only_add.default = str(entry.get("only_add", False)).lower()
        self.max_roles.default = str(entry.get("max_roles") or 0)

    async def on_submit(self, interaction: discord.Interaction):
        only_add_val = self.only_add.value.strip().lower() == "true"
        try:
            max_val = int(self.max_roles.value.strip())
            max_val = max_val if max_val > 0 else None
        except ValueError:
            max_val = self.entry.get("max_roles")

        updated = {
            **self.entry,
            "title": self.rr_title.value.strip(),
            "only_add": only_add_val,
            "max_roles": max_val,
            "editing_message_id": self.message_id
        }
        _sessions[interaction.user.id] = updated

        roles_list = "\n".join(
            f"• **{r['label']}** → {interaction.guild.get_role(r['role_id']).name if interaction.guild.get_role(r['role_id']) else 'deleted role'}"
            for r in updated["roles"]
        ) or "No roles yet."

        view = _build_edit_role_view(interaction.user.id)

        await interaction.response.send_message(
            f"✅ **Settings updated!**\n"
            f"**Title:** {updated['title']} | **Only Add:** {only_add_val} | **Max:** {max_val or 'unlimited'}\n\n"
            f"**Current roles:**\n{roles_list}\n\n"
            f"Use the buttons below to add/remove roles, then `/rr publish` to save.",
            view=view,
            ephemeral=True
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_edit_role_view(user_id: int) -> discord.ui.View:
    """Build the Add/Remove role buttons for edit flow"""
    view = discord.ui.View()
    add_btn = discord.ui.Button(label="Add Role", style=discord.ButtonStyle.success)
    remove_btn = discord.ui.Button(label="Remove Role", style=discord.ButtonStyle.danger)

    async def add_callback(i: discord.Interaction):
        await i.response.send_message("Use `/rr addrole` to add a role, then `/rr publish` to save.", ephemeral=True)

    async def remove_callback(i: discord.Interaction):
        session = _sessions.get(user_id, {})
        roles = session.get("roles", [])
        if not roles:
            await i.response.send_message("No roles to remove.", ephemeral=True)
            return

        options = [discord.SelectOption(label=r["label"], value=str(r["role_id"])) for r in roles]
        select = discord.ui.Select(placeholder="Select role to remove", options=options)

        async def select_cb(si: discord.Interaction):
            role_id_to_remove = int(select.values[0])
            s = _sessions.get(si.user.id, {})
            s["roles"] = [r for r in s["roles"] if r["role_id"] != role_id_to_remove]
            _sessions[si.user.id] = s
            await si.response.send_message("✅ Role removed. Use `/rr publish` to save.", ephemeral=True)

        select.callback = select_cb
        rv = discord.ui.View()
        rv.add_item(select)
        await i.response.send_message("Select the role to remove:", view=rv, ephemeral=True)

    add_btn.callback = add_callback
    remove_btn.callback = remove_callback
    view.add_item(add_btn)
    view.add_item(remove_btn)
    return view


async def _get_or_create_role(guild: discord.Guild, role_name: str) -> discord.Role:
    clean = role_name.strip().lstrip("@")
    existing = discord.utils.get(guild.roles, name=clean)
    if existing:
        return existing
    new_role = await guild.create_role(name=clean, reason="Created by ExcelProtocol reaction roles")
    logger.info(f"Created role '{clean}' in {guild.name}")
    return new_role


def _build_view(rr_entry: dict, bot) -> discord.ui.View:
    rr_type = rr_entry.get("type", "dropdown")
    only_add = rr_entry.get("only_add", False)
    max_roles = rr_entry.get("max_roles", None)
    roles_data = rr_entry.get("roles", [])

    view = discord.ui.View(timeout=None)

    if rr_type == "dropdown":
        options = [
            discord.SelectOption(label=r["label"], value=str(r["role_id"]))
            for r in roles_data
        ]
        select = discord.ui.Select(
            placeholder="Make a selection",
            min_values=0,
            max_values=min(max_roles, len(options)) if max_roles else len(options),
            options=options,
            custom_id=f"rr_select_{rr_entry['message_id']}"
        )

        async def select_callback(interaction: discord.Interaction):
            await _handle_select(interaction, select.values, roles_data, only_add, max_roles)

        select.callback = select_callback
        view.add_item(select)

    elif rr_type == "buttons":
        for r in roles_data:
            button = discord.ui.Button(
                label=r["label"],
                style=discord.ButtonStyle.primary,
                custom_id=f"rr_btn_{rr_entry['message_id']}_{r['role_id']}"
            )
            role_id = r["role_id"]

            async def btn_callback(interaction: discord.Interaction, rid=role_id):
                await _handle_button(interaction, rid, only_add)

            button.callback = btn_callback
            view.add_item(button)

    return view


async def _handle_select(interaction: discord.Interaction, selected_values: list, roles_data: list, only_add: bool, max_roles):
    member = interaction.user
    guild = interaction.guild
    all_role_ids = [r["role_id"] for r in roles_data]
    selected_ids = [int(v) for v in selected_values]

    try:
        roles_to_add = []
        roles_to_remove = []

        for role_id in all_role_ids:
            role = guild.get_role(role_id)
            if not role:
                continue
            if role_id in selected_ids:
                if role not in member.roles:
                    roles_to_add.append(role)
            else:
                if not only_add and role in member.roles:
                    roles_to_remove.append(role)

        if roles_to_add:
            await member.add_roles(*roles_to_add, reason="Reaction role selection")
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Reaction role deselection")

        lines = []
        if roles_to_add:
            lines.append(f"✅ Added: {', '.join(r.name for r in roles_to_add)}")
        if roles_to_remove:
            lines.append(f"➖ Removed: {', '.join(r.name for r in roles_to_remove)}")
        if not lines:
            lines.append("No changes made.")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to assign that role.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in select callback: {e}", exc_info=True)
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


async def _handle_button(interaction: discord.Interaction, role_id: int, only_add: bool):
    member = interaction.user
    guild = interaction.guild
    role = guild.get_role(role_id)

    if not role:
        await interaction.response.send_message("❌ That role no longer exists.", ephemeral=True)
        return

    try:
        if role in member.roles:
            if only_add:
                await interaction.response.send_message(f"You already have **{role.name}**.", ephemeral=True)
            else:
                await member.remove_roles(role, reason="Reaction role button")
                await interaction.response.send_message(f"➖ Removed **{role.name}**.", ephemeral=True)
        else:
            await member.add_roles(role, reason="Reaction role button")
            await interaction.response.send_message(f"✅ Added **{role.name}**.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to assign that role.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in button callback: {e}", exc_info=True)
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ------------------------------------------------------------------
# Restore views on startup
# ------------------------------------------------------------------

async def restore_views(bot):
    data = _load_data()
    for message_id, entry in data.items():
        try:
            view = _build_view(entry, bot)
            bot.add_view(view, message_id=int(message_id))
        except Exception as e:
            logger.error(f"Failed to restore view for message {message_id}: {e}")
    logger.info(f"Restored {len(data)} reaction role view(s)")


# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

async def setup(bot):
    await restore_views(bot)

    rr_group = app_commands.Group(name="rr", description="Reaction role management")

    # ------------------------------------------------------------------
    # /rr create
    # ------------------------------------------------------------------
    @rr_group.command(name="create", description="Start creating a new reaction role message")
    async def rr_create(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return
        if interaction.user.id in _sessions:
            await interaction.response.send_message(
                "❌ You already have an active session. Use `/rr cancel` to cancel it first.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(CreateSettingsModal(bot))

    # ------------------------------------------------------------------
    # /rr cancel
    # ------------------------------------------------------------------
    @rr_group.command(name="cancel", description="Cancel your current reaction role session")
    async def rr_cancel(interaction: discord.Interaction):
        if interaction.user.id in _sessions:
            del _sessions[interaction.user.id]
            await interaction.response.send_message("✅ Session cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No active session to cancel.", ephemeral=True)

    # ------------------------------------------------------------------
    # /rr addrole
    # ------------------------------------------------------------------
    @rr_group.command(name="addrole", description="Add a role to your reaction role message")
    @app_commands.describe(
        label="The label shown on the button or dropdown option",
        role="The role to assign (will be created if it doesn't exist)"
    )
    async def rr_addrole(interaction: discord.Interaction, label: str, role: str):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        session = _sessions.get(interaction.user.id)
        if not session or session["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ No active session. Run `/rr create` first.", ephemeral=True)
            return

        if session["type"] == "buttons" and len(session["roles"]) >= 25:
            await interaction.response.send_message("❌ Maximum 25 buttons per message.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        discord_role = await _get_or_create_role(interaction.guild, role)
        session["roles"].append({"label": label, "role_id": discord_role.id})

        roles_so_far = "\n".join(
            f"• **{r['label']}** → {interaction.guild.get_role(r['role_id']).name}"
            for r in session["roles"]
        )

        await interaction.followup.send(
            f"✅ Added **{label}** → `{discord_role.name}`\n\n"
            f"**Roles so far:**\n{roles_so_far}\n\n"
            f"Add more with `/rr addrole` or publish with `/rr publish`.",
            ephemeral=True
        )

    # ------------------------------------------------------------------
    # /rr publish
    # ------------------------------------------------------------------
    @rr_group.command(name="publish", description="Post the reaction role message to this channel")
    async def rr_publish(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        session = _sessions.get(interaction.user.id)
        if not session or session["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ No active session. Run `/rr create` first.", ephemeral=True)
            return

        if not session["roles"]:
            await interaction.response.send_message("❌ Add at least one role first with `/rr addrole`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(title=session["title"], color=0x9146FF)
        editing_id = session.get("editing_message_id")

        if editing_id:
            # Update existing message
            data = _load_data()
            entry = {**session, "message_id": int(editing_id)}
            view = _build_view(entry, bot)

            try:
                channel = bot.get_channel(session["channel_id"])
                msg = await channel.fetch_message(int(editing_id))
                await msg.edit(embed=embed, view=view)
                bot.add_view(view, message_id=int(editing_id))
                data[str(editing_id)] = entry
                _save_data(data)
                del _sessions[interaction.user.id]
                await interaction.followup.send(f"✅ Reaction role message updated!", ephemeral=True)
            except Exception as e:
                logger.error(f"Error updating message: {e}")
                await interaction.followup.send("❌ Could not find or edit the original message.", ephemeral=True)
        else:
            # Post new message
            temp_entry = {**session, "message_id": 0}
            view = _build_view(temp_entry, bot)
            message = await interaction.channel.send(embed=embed, view=view)

            entry = {**session, "message_id": message.id}
            view = _build_view(entry, bot)
            await message.edit(view=view)
            bot.add_view(view, message_id=message.id)

            data = _load_data()
            data[str(message.id)] = entry
            _save_data(data)

            del _sessions[interaction.user.id]
            await interaction.followup.send(f"✅ Reaction role message posted! Message ID: `{message.id}`", ephemeral=True)

    # ------------------------------------------------------------------
    # /rr edit
    # ------------------------------------------------------------------
    @rr_group.command(name="edit", description="Edit an existing reaction role message")
    @app_commands.describe(message_id="The ID of the reaction role message to edit")
    async def rr_edit(interaction: discord.Interaction, message_id: str):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        data = _load_data()
        entry = data.get(message_id)

        if not entry or entry["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ Reaction role message not found in this server.", ephemeral=True)
            return

        await interaction.response.send_modal(EditSettingsModal(entry, message_id, bot))

    # ------------------------------------------------------------------
    # /rr delete
    # ------------------------------------------------------------------
    @rr_group.command(name="delete", description="Delete a reaction role message")
    @app_commands.describe(message_id="The ID of the reaction role message to delete")
    async def rr_delete(interaction: discord.Interaction, message_id: str):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        data = _load_data()
        entry = data.get(message_id)

        if not entry or entry["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ Reaction role message not found in this server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            channel = bot.get_channel(entry["channel_id"])
            if channel:
                msg = await channel.fetch_message(int(message_id))
                await msg.delete()
        except Exception:
            pass

        del data[message_id]
        _save_data(data)
        await interaction.followup.send("✅ Reaction role message deleted.", ephemeral=True)

    # ------------------------------------------------------------------
    # /rr list
    # ------------------------------------------------------------------
    @rr_group.command(name="list", description="List all reaction role messages in this server")
    async def rr_list(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        data = _load_data()
        server_entries = {mid: e for mid, e in data.items() if e["guild_id"] == interaction.guild_id}

        if not server_entries:
            await interaction.response.send_message("No reaction role messages in this server yet.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Reaction Role Messages", color=0x9146FF)

        for mid, entry in server_entries.items():
            channel = bot.get_channel(entry["channel_id"])
            channel_mention = f"<#{entry['channel_id']}>" if channel else "unknown channel"
            role_names = ", ".join(r["label"] for r in entry["roles"])
            embed.add_field(
                name=f"{entry['title']} (ID: {mid})",
                value=f"Channel: {channel_mention}\nType: {entry['type']} | Roles: {role_names}",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(rr_group)
    logger.info("Reaction roles commands registered")
