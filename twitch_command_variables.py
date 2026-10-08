"""Safe variable expansion for Twitch custom-command responses."""

import re
import secrets


TWITCH_MESSAGE_LIMIT = 500
_RANDOM_VARIABLE_RE = re.compile(
    r"\$random\(\s*(-?\d{1,7})\s*,\s*(-?\d{1,7})\s*\)", re.IGNORECASE
)
_CHOICE_VARIABLE_RE = re.compile(
    r"\$choice\(([^()\r\n]{1,300})\)", re.IGNORECASE
)


def render_custom_command(text: str, username: str, channel: str, count: int,
                          args: str = "", display_name: str = None,
                          user_count: int = 1, command_name: str = "") -> str:
    """Expand supported placeholders without evaluating user-authored code."""
    raw_args = str(args or "").strip()
    target_match = re.match(r"^@?([A-Za-z0-9_]{1,25})(?:\s|$)", raw_args)
    target_name = target_match.group(1) if target_match else username
    target = f"@{target_name}"

    def random_value(match):
        low = max(-1_000_000, min(1_000_000, int(match.group(1))))
        high = max(-1_000_000, min(1_000_000, int(match.group(2))))
        if low > high:
            low, high = high, low
        return str(low + secrets.randbelow(high - low + 1))

    def choice_value(match):
        options = [option.strip() for option in match.group(1).split("|") if option.strip()]
        return secrets.choice(options[:50]) if options else ""

    rendered = _RANDOM_VARIABLE_RE.sub(random_value, str(text or ""))
    rendered = _CHOICE_VARIABLE_RE.sub(choice_value, rendered)
    # Replace longer names first so $user does not corrupt $usercount.
    variables = (
        ("$displayname", display_name or username),
        ("$targetname", target_name),
        ("$usercount", str(user_count)),
        ("$command", command_name),
        ("$channel", channel),
        ("$target", target),
        ("$count", str(count)),
        ("$args", raw_args),
        ("$user", username),
    )
    for variable, value in variables:
        rendered = rendered.replace(variable, str(value))
    return " ".join(
        rendered.replace("\r", " ").replace("\n", " ").split()
    )[:TWITCH_MESSAGE_LIMIT]
