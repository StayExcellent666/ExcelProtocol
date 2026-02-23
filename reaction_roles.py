import discord
from discord import app_commands
import json
import os
import logging

logger = logging.getLogger(__name__)

RR_DATA_PATH = "/data/reaction_roles.json"
DEFAULT_COLOR = 0x00FFFF

# {user_id: {guild_id, channel_id, title, type, only_add, max_roles, roles, editing_message_id}}
_sessions: dict[int, dict] = {}
_bot = None  # stored reference for color lookups


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
# Color helpers
# ------------------------------------------------------------------

def _get_button_style(guild_id: int) -> discord.ButtonStyle:
    """Map the server's embed color to the closest Discord button style"""
    try:
        color = _bot.db.get_embed_color(guild_id)
    except Exception:
        color = DEFAULT_COLOR

    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF

    # Map to closest Discord button color
    # Success = green, Danger = red, Primary = blue, Secondary = grey
    max_channel = max(r, g, b)
    if max_channel == 0:
        return discord.ButtonStyle.secondary

    if r == max_channel and r > g + 30 and r > b + 30:
        return discord.ButtonStyle.danger   # reddish
    elif g == max_channel and g > r + 30 and g > b + 30:
        return discord.ButtonStyle.success  # greenish
    elif b == max_channel and b > r + 30 and b > g + 30:
        return discord.ButtonStyle.primary  # blueish
    elif r == max_channel and g > b + 30:
        return discord.ButtonStyle.danger   # orange-ish → red
    else:
        return discord.ButtonStyle.secondary  # default grey


def _get_embed_color(guild_id: int) -> int:
    try:
        return _bot.db.get_embed_color(guild_id)
    except Exception:
        return DEFAULT_COLOR


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
            "type": "dropdown",
            "only_add": only_add_val,
            "max_roles": max_val,
            "roles": [],
            "editing_message_id": None
        }

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
            "✅ Settings saved! Now choose the selector type:",
            view=view,
            ephemeral=True
        )


class EditSettingsModal(discord.ui.Modal, title="Edit Reaction Role"):
    rr_title = discord.ui.TextInput(label="Title", max_length=100)
    only_add = discord.ui.TextInput(label="Only Add? (true/false)", max_length=5, required=False)
    max_roles = discord.ui.TextInput(label="Max roles a user can pick (0 = unlimited)", max_length=2, required=False)

    def __init__(self, entry: dict, message_id: str):
        super().__init__()
        self.message_id = message_id
        self.entry = entry
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
            "editing_message_id": self.message_id,
            "channel_id": self.entry["channel_id"],  # preserve original channel
        }
        _sessions[interaction.user.id] = updated

        roles_list = "\n".join(
            f"• **{r['label']}** → {interaction.guild.get_role(r['role_id']).name if interaction.guild.get_role(r['role_id']) else 'deleted role'}"
            for r in updated["roles"]
        ) or "No roles yet."

        view = _build_edit_role_view(interaction.user.id, interaction.guild)

        await interaction.response.send_message(
            f"✅ **Settings updated!**\n"
            f"**Title:** {updated['title']} | **Only Add:** {only_add_val} | **Max:** {max_val or 'unlimited'}\n\n"
            f"**Current roles:**\n{roles_list}\n\n"
            "Use the buttons below to add/remove/edit roles, then `/rr publish` to save.",
            view=view,
            ephemeral=True
        )


# ------------------------------------------------------------------
# Edit role view (add / remove / edit label)
# ------------------------------------------------------------------

def _build_edit_role_view(user_id: int, guild: discord.Guild) -> discord.ui.View:
    view = discord.ui.View()
    add_btn = discord.ui.Button(label="Add Role", style=discord.ButtonStyle.success)
    remove_btn = discord.ui.Button(label="Remove Role", style=discord.ButtonStyle.danger)
    edit_btn = discord.ui.Button(label="Edit Label", style=discord.ButtonStyle.secondary)

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

    async def edit_label_callback(i: discord.Interaction):
        session = _sessions.get(user_id, {})
        roles = session.get("roles", [])
        if not roles:
            await i.response.send_message("No roles to edit.", ephemeral=True)
            return

        options = [discord.SelectOption(label=r["label"], value=str(r["role_id"])) for r in roles]
        select = discord.ui.Select(placeholder="Select role to edit label", options=options)

        async def select_cb(si: discord.Interaction):
            role_id_to_edit = int(select.values[0])
            s = _sessions.get(si.user.id, {})

            class EditLabelModal(discord.ui.Modal, title="Edit Role Label"):
                new_label = discord.ui.TextInput(label="New Label", max_length=50)

                async def on_submit(inner_self, mi: discord.Interaction):
                    for r in s["roles"]:
                        if r["role_id"] == role_id_to_edit:
                            r["label"] = inner_self.new_label.value.strip()
                    _sessions[mi.user.id] = s
                    await mi.response.send_message(f"✅ Label updated to **{inner_self.new_label.value.strip()}**. Use `/rr publish` to save.", ephemeral=True)

            await si.response.send_modal(EditLabelModal())

        select.callback = select_cb
        rv = discord.ui.View()
        rv.add_item(select)
        await i.response.send_message("Select role to edit its label:", view=rv, ephemeral=True)

    add_btn.callback = add_callback
    remove_btn.callback = remove_callback
    edit_btn.callback = edit_label_callback
    view.add_item(add_btn)
    view.add_item(remove_btn)
    view.add_item(edit_btn)
    return view


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _get_or_create_role(guild: discord.Guild, role_name: str) -> discord.Role:
    # Handle Discord role mention format <@&ROLE_ID>
    import re
    mention_match = re.match(r'<@&(\d+)>', role_name.strip())
    if mention_match:
        role_id = int(mention_match.group(1))
        role = guild.get_role(role_id)
        if role:
            return role

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
    guild_id = rr_entry.get("guild_id", 0)

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
        btn_style = _get_button_style(guild_id)
        all_role_ids = [r["role_id"] for r in roles_data]
        for r in roles_data:
            button = discord.ui.Button(
                label=r["label"],
                style=btn_style,
                custom_id=f"rr_btn_{rr_entry['message_id']}_{r['role_id']}"
            )
            role_id = r["role_id"]

            async def btn_callback(interaction: discord.Interaction, rid=role_id, arids=all_role_ids, mr=max_roles):
                await _handle_button(interaction, rid, only_add, arids, mr)

            button.callback = btn_callback
            view.add_item(button)

    return view


async def _handle_select(interaction: discord.Interaction, selected_values: list, roles_data: list, only_add: bool, max_roles):
    member = interaction.user
    guild = interaction.guild
    all_role_ids = [r["role_id"] for r in roles_data]
    selected_ids = [int(v) for v in selected_values]

    # Enforce max_roles server-side
    if max_roles and len(selected_ids) > max_roles:
        await interaction.response.send_message(
            f"❌ You can only select up to **{max_roles}** role{'s' if max_roles != 1 else ''}.",
            ephemeral=True
        )
        return

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


async def _handle_button(interaction: discord.Interaction, role_id: int, only_add: bool, all_role_ids: list = None, max_roles: int = None):
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
            # Enforce max_roles for buttons
            if max_roles and all_role_ids:
                current_count = sum(1 for rid in all_role_ids if guild.get_role(rid) in member.roles)
                if current_count >= max_roles:
                    await interaction.response.send_message(
                        f"❌ You can only have **{max_roles}** role{'s' if max_roles != 1 else ''} from this group.",
                        ephemeral=True
                    )
                    return
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
    global _bot
    _bot = bot

    await restore_views(bot)

    rr_group = app_commands.Group(name="rr", description="Reaction role management")
    rr_group.default_permissions = discord.Permissions(manage_roles=True)

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
        await interaction.response.send_modal(CreateSettingsModal())

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
        role="The role to assign (type a name to create a new one, or pick existing)",
        new_role_name="Create a new role with this name instead of picking an existing one"
    )
    async def rr_addrole(interaction: discord.Interaction, label: str, role: discord.Role = None, new_role_name: str = None):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        session = _sessions.get(interaction.user.id)
        if not session or session["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ No active session. Run `/rr create` or `/rr edit` first.", ephemeral=True)
            return

        if session["type"] == "buttons" and len(session["roles"]) >= 25:
            await interaction.response.send_message("❌ Maximum 25 buttons per message.", ephemeral=True)
            return

        if not role and not new_role_name:
            await interaction.response.send_message("❌ Please either pick an existing role or provide a new role name.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if role:
            discord_role = role
        else:
            discord_role = await _get_or_create_role(interaction.guild, new_role_name)
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
    @rr_group.command(name="publish", description="Post or update the reaction role message")
    async def rr_publish(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You need 'Manage Roles' permission.", ephemeral=True)
            return

        session = _sessions.get(interaction.user.id)
        if not session or session["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("❌ No active session. Run `/rr create` or `/rr edit` first.", ephemeral=True)
            return

        if not session["roles"]:
            await interaction.response.send_message("❌ Add at least one role first with `/rr addrole`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title=session["title"],
            color=_get_embed_color(session["guild_id"])
        )

        editing_id = session.get("editing_message_id")

        if editing_id:
            # Update existing message
            data = _load_data()
            entry = {**session, "message_id": int(editing_id)}
            view = _build_view(entry, bot)

            try:
                # Use stored channel_id from original entry
                channel = bot.get_channel(session["channel_id"])
                if not channel:
                    await interaction.followup.send("❌ Could not find the original channel.", ephemeral=True)
                    return
                msg = await channel.fetch_message(int(editing_id))
                await msg.edit(embed=embed, view=view)
                bot.add_view(view, message_id=int(editing_id))
                data[str(editing_id)] = entry
                _save_data(data)
                del _sessions[interaction.user.id]
                await interaction.followup.send("✅ Reaction role message updated!", ephemeral=True)
            except discord.NotFound:
                await interaction.followup.send("❌ The original message was deleted. Use `/rr create` to make a new one.", ephemeral=True)
                del _sessions[interaction.user.id]
            except Exception as e:
                logger.error(f"Error updating RR message: {e}", exc_info=True)
                await interaction.followup.send("❌ Something went wrong updating the message.", ephemeral=True)
        else:
            # Post new message
            temp_entry = {**session, "message_id": 0}
            view = _build_view(temp_entry, bot)
            message = await interaction.channel.send(embed=embed, view=view)

            entry = {**session, "message_id": message.id, "channel_id": interaction.channel_id}
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

        await interaction.response.send_modal(EditSettingsModal(entry, message_id))

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

        embed = discord.Embed(title="📋 Reaction Role Messages", color=_get_embed_color(interaction.guild_id))

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
