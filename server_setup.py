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
                    ("🎵-music", "text"),
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
                    ("music", "text"),
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
