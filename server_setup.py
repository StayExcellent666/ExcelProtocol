"""Server template definitions and helpers for the Set Up Server wizard.

This module defines what each template looks like (channels, categories,
roles, modules) as pure data. The orchestration that actually applies these
to a guild lives in bot.py — keeping the data here makes it easy to test
template generation in isolation and to extend with new template variants
later without touching bot internals.

The three templates are NAMED INTENTIONALLY by the user:
  - Aesthetic    — emoji-prefixed channel names, curated feel
  - Simplistic   — bare minimum, easy to manage
  - Cluttered    — everything-and-the-kitchen-sink

VIP is a module overlay that adds VIP channels to any of the three.
Verification is a flag that affects permissions (hides everything except
welcome + rules from @everyone until the user clicks Verify).
"""

# Default role names, mapped to the categories the user can override.
DEFAULT_ROLE_NAMES = {
    "member":    "MEMBER",
    "vip":       "VIP",
    "moderator": "MODERATOR",
    "admin":     "ADMIN",
    "bot":       "BOT",
}

# Default permission flags per role.
#
# Discord permissions are bitflags applied at the guild level (server-wide
# perms) and can be further refined by per-channel overwrites. These are
# the GUILD-LEVEL perms the wizard sets when creating each role. Values
# correspond to flag names on discord.Permissions.
#
# Philosophy: give each role what it actually NEEDS to do its job, nothing
# more. Admin gets every non-admin management perm explicitly enumerated
# (since the bot avoids the Administrator flag itself, it can't grant it).
# Mod gets the typical moderation toolkit. Bot gets the standard "managing
# things" perms a community bot needs. VIP and Member get social-only perms.
#
# A note on the Administrator flag: it grants everything AND bypasses all
# channel-level deny overwrites. The wizard intentionally never grants it
# because the bot itself doesn't have it (and Discord won't let a bot
# grant permissions it doesn't have). The post-run summary in the dashboard
# reminds the server owner to manually toggle Administrator on the admin
# role in Discord's Server Settings → Roles if they want it.
ROLE_PERMISSIONS = {
    "admin": [
        # All meaningful server-management perms EXCEPT `administrator`.
        # Rationale: the bot itself avoids Administrator, and Discord won't
        # let a bot grant a permission it doesn't have. So we enumerate
        # everything the admin role typically needs and let the server owner
        # grant the actual Administrator flag manually after wizard runs.
        # The wizard surfaces a post-run note reminding them to do so.
        "view_channel",
        "manage_channels",
        "manage_roles",
        "manage_emojis_and_stickers",
        "manage_webhooks",
        "manage_messages",
        "manage_threads",
        "manage_nicknames",
        "manage_guild",
        "manage_events",
        "view_audit_log",
        "view_guild_insights",
        "kick_members",
        "ban_members",
        "moderate_members",
        "mention_everyone",
        "send_messages",
        "send_messages_in_threads",
        "create_public_threads",
        "create_private_threads",
        "send_tts_messages",
        "embed_links",
        "attach_files",
        "read_message_history",
        "add_reactions",
        "use_external_emojis",
        "use_external_stickers",
        "connect",
        "speak",
        "stream",
        "use_voice_activation",
        "priority_speaker",
        "mute_members",
        "deafen_members",
        "move_members",
        "use_application_commands",
    ],
    "moderator": [
        "view_channel",
        "read_message_history",
        "send_messages",
        "embed_links",
        "attach_files",
        "add_reactions",
        "use_external_emojis",
        "kick_members",
        "ban_members",
        "manage_messages",       # delete others' messages, pin
        "manage_threads",
        "moderate_members",      # timeout
        "mute_members",          # voice mute
        "deafen_members",        # voice deafen
        "move_members",          # voice move
        "connect",
        "speak",
    ],
    "bot": [
        # What a community bot typically needs to do its job. Specific
        # bots may need more; users can grant additional perms per-bot
        # in Server Settings → Roles afterward. This is a sensible default.
        "view_channel",
        "read_message_history",
        "send_messages",
        "send_messages_in_threads",
        "embed_links",
        "attach_files",
        "add_reactions",
        "use_external_emojis",
        "manage_messages",
        "manage_channels",
        "manage_roles",
        "manage_webhooks",
        "manage_threads",
        "create_public_threads",
        "create_private_threads",
        "connect",
        "speak",
        "move_members",
    ],
    "vip": [
        # Slightly elevated member perks: higher attach limits implicit via
        # the role, and access to more channels via channel overwrites
        # (handled in _apply_setup_permissions). Server-level perms are
        # just the standard social ones.
        "view_channel",
        "read_message_history",
        "send_messages",
        "send_messages_in_threads",
        "create_public_threads",
        "embed_links",
        "attach_files",
        "add_reactions",
        "use_external_emojis",
        "use_external_stickers",
        "connect",
        "speak",
        "stream",
        "use_voice_activation",
    ],
    "member": [
        # Bare minimum to participate. When verification is enabled, this
        # role is what unlocks the normal community channels.
        "view_channel",
        "read_message_history",
        "send_messages",
        "send_messages_in_threads",
        "embed_links",
        "attach_files",
        "add_reactions",
        "use_external_emojis",
        "connect",
        "speak",
        "use_voice_activation",
    ],
}


def build_permissions(role_key: str):
    """Return a discord.Permissions object for the given role key.

    Imports discord lazily so this module stays importable in test runs
    that don't bring discord.py along.
    """
    import discord
    flags = ROLE_PERMISSIONS.get(role_key, [])
    kwargs = {flag: True for flag in flags}
    try:
        return discord.Permissions(**kwargs)
    except TypeError as e:
        # A flag name was wrong for the installed discord.py version.
        # Filter out the bad one(s) and try again so the wizard still
        # works rather than crashing entirely.
        import logging
        logging.getLogger(__name__).warning(
            f"build_permissions for {role_key}: unknown flag, retrying without it ({e})"
        )
        # Build incrementally so we know which flag failed
        perms = discord.Permissions.none()
        for flag in flags:
            try:
                setattr(perms, flag, True)
            except (AttributeError, TypeError):
                continue
        return perms


# Role hierarchy from HIGHEST (top) to LOWEST. Created in this order so
# positions stack correctly. All sit just below the bot's own role.
ROLE_HIERARCHY = ["admin", "moderator", "bot", "vip", "member"]

# Hardcoded rules text. Posted if `auto_post_rules` is enabled.
DEFAULT_RULES_TEXT = (
    "**Server Rules**\n\n"
    "1. **Be respectful** — treat everyone with basic decency. No personal "
    "attacks or harassment.\n"
    "2. **No spam** — including link spam, copypastas, and excessive caps/emojis.\n"
    "3. **No hate speech** — racism, sexism, homophobia, transphobia, or any "
    "other targeted hate gets you removed.\n"
    "4. **No NSFW content** — keep all images, links, and discussion SFW.\n"
    "5. **Follow Discord and Twitch ToS** — if it would get you banned there, "
    "it'll get you banned here.\n"
    "6. **Listen to staff** — moderators and admins have the final call.\n"
    "7. **Self-promo** only in designated channels (if any). Otherwise, ask first.\n\n"
    "Click the **Verify** button below to gain access to the server."
)

# Verification prompt text shown alongside the Verify button.
# When verification is ENABLED, this replaces the standard rules post:
# the rules + verify button appear together so users see the rules
# before clicking Verify.
VERIFY_PROMPT_HEADER = "**Welcome — please verify to access the server**"


# ── Template definitions ──────────────────────────────────────────────────────
# Each template is a list of categories. Each category has a name and a
# list of channels. Each channel is (name, type) where type is "text" or
# "voice". Aesthetic uses emoji prefixes; others don't.
#
# When verification is enabled, the FIRST category's first two channels
# (welcome, rules) stay visible to @everyone. Everything else gets hidden.

TEMPLATES = {
    "aesthetic": {
        "label": "Aesthetic",
        "description": "Curated channels with emoji prefixes for a polished, vibe-y feel.",
        "categories": [
            {
                "name": "🌸 INFO",
                "channels": [
                    ("🌸-welcome", "text"),
                    ("📜-rules", "text"),
                    ("📣-announcements", "text"),
                ],
            },
            {
                "name": "✨ COMMUNITY",
                "channels": [
                    ("💬-general", "text"),
                    ("🖼-images-and-vibes", "text"),
                ],
            },
            {
                "name": "🎮 STREAMING",
                "channels": [
                    ("🔴-live-notifications", "text"),
                    ("📎-clips", "text"),
                    ("📼-vods", "text"),
                ],
            },
            {
                "name": "🎤 VOICE",
                "channels": [
                    ("Lounge", "voice"),
                    ("Stream Hangout", "voice"),
                ],
            },
        ],
    },
    "simplistic": {
        "label": "Simplistic",
        "description": "Bare minimum: welcome, rules, general, live notifications, and one voice channel.",
        "categories": [
            {
                "name": "INFO",
                "channels": [
                    ("welcome", "text"),
                    ("rules", "text"),
                ],
            },
            {
                "name": "GENERAL",
                "channels": [
                    ("general", "text"),
                    ("live-notifications", "text"),
                ],
            },
            {
                "name": "VOICE",
                "channels": [
                    ("Lounge", "voice"),
                ],
            },
        ],
    },
    "cluttered": {
        "label": "Cluttered",
        "description": "Everything-and-the-kitchen-sink: lots of channels organized by topic for active communities.",
        "categories": [
            {
                "name": "INFO",
                "channels": [
                    ("welcome", "text"),
                    ("rules", "text"),
                    ("announcements", "text"),
                    ("server-updates", "text"),
                ],
            },
            {
                "name": "COMMUNITY",
                "channels": [
                    ("general", "text"),
                    ("introductions", "text"),
                    ("off-topic", "text"),
                    ("pets", "text"),
                    ("gaming", "text"),
                    ("food", "text"),
                    ("art", "text"),
                    ("selfies", "text"),
                ],
            },
            {
                "name": "STREAMING",
                "channels": [
                    ("live-notifications", "text"),
                    ("clips", "text"),
                    ("vods", "text"),
                    ("stream-discussion", "text"),
                    ("schedule", "text"),
                ],
            },
            {
                "name": "BOTS",
                "channels": [
                    ("bot-commands", "text"),
                    ("bot-spam", "text"),
                ],
            },
            {
                "name": "VOICE",
                "channels": [
                    ("Lounge", "voice"),
                    ("Stream Hangout", "voice"),
                    ("Gaming", "voice"),
                    ("AFK", "voice"),
                ],
            },
        ],
    },
}


# VIP module is applied as an OVERLAY to any base template.
def vip_category(template_id: str) -> dict:
    """Generate the VIP category for a given template (uses emoji if aesthetic)."""
    if template_id == "aesthetic":
        return {
            "name": "💎 VIP",
            "channels": [
                ("💎-vip-chat", "text"),
                ("VIP VC", "voice"),
            ],
        }
    return {
        "name": "VIP",
        "channels": [
            ("vip-chat", "text"),
            ("VIP VC", "voice"),
        ],
    }


def resolve_role_names(user_overrides: dict) -> dict:
    """Apply user overrides to default role names. Empty/missing → default."""
    resolved = {}
    for key, default in DEFAULT_ROLE_NAMES.items():
        override = (user_overrides or {}).get(key, "")
        if isinstance(override, str) and override.strip():
            resolved[key] = override.strip()
        else:
            resolved[key] = default
    return resolved


def build_plan(config: dict) -> dict:
    """Build the full plan dict from a wizard config.

    config keys:
      template:           'aesthetic' | 'simplistic' | 'cluttered'
      enable_verification: bool
      enable_vip:          bool
      auto_post_rules:     bool
      role_names:          dict of overrides
      color:               int (RGB) — applied to bot embeds via db.get_embed_color
                           (separate concern; stored alongside but not part of the plan)

    Returns a plan dict with:
      template_id
      template_label
      role_names: resolved final names
      categories: list of category-with-channels to create
      verification_enabled: bool
      vip_enabled: bool
      auto_post_rules: bool

    The orchestrator (bot.setup_orchestrator) walks this plan and reports
    per-step progress to the status dict.
    """
    template_id = config.get("template", "simplistic")
    if template_id not in TEMPLATES:
        template_id = "simplistic"
    template = TEMPLATES[template_id]

    categories = [dict(c) for c in template["categories"]]
    if config.get("enable_vip"):
        categories.append(vip_category(template_id))

    role_names = resolve_role_names(config.get("role_names") or {})

    return {
        "template_id": template_id,
        "template_label": template["label"],
        "role_names": role_names,
        "categories": categories,
        "verification_enabled": bool(config.get("enable_verification")),
        "vip_enabled": bool(config.get("enable_vip")),
        "auto_post_rules": bool(config.get("auto_post_rules")),
    }


def is_server_established(n_channels: int, n_custom_roles: int, n_members: int) -> bool:
    """Heuristic: is this an existing, lived-in server (vs a fresh one)?

    Used by the Set Up Server wizard to decide whether to require explicit
    confirmation before applying a template. Returns True if the guild has
    any signal of substantial existing content.

    Thresholds tuned to NOT trip on a brand-new Discord server (which
    auto-starts with 2-4 channels and only @everyone) but DO trip on any
    real community.

    Args:
        n_channels:      total channel count (text + voice + categories)
        n_custom_roles:  roles excluding @everyone and managed integration roles
        n_members:       total member count

    Returns:
        True if any threshold is exceeded, False otherwise.
    """
    return n_channels > 5 or n_custom_roles > 3 or n_members > 10


def count_plan_items(plan: dict) -> dict:
    """Return summary counts for the preview / progress UI."""
    n_roles = len(ROLE_HIERARCHY)
    n_categories = len(plan["categories"])
    n_text = sum(1 for cat in plan["categories"] for _, t in cat["channels"] if t == "text")
    n_voice = sum(1 for cat in plan["categories"] for _, t in cat["channels"] if t == "voice")
    return {
        "roles": n_roles,
        "categories": n_categories,
        "text_channels": n_text,
        "voice_channels": n_voice,
        "total_channels": n_text + n_voice,
    }
