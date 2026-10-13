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
_BOLD_ITALIC_RE = re.compile(r"\*\*\*([^*\r\n]+?)\*\*\*")
_BOLD_RE = re.compile(r"\*\*([^*\r\n]+?)\*\*")
_ITALIC_RE = re.compile(r"\*([^*\r\n]+?)\*")
_ESCAPED_ASTERISK = "\ue000"


def _stylize_ascii(value: str, style: str) -> str:
    """Convert basic ASCII to Twitch-safe mathematical Unicode lettering."""
    ranges = {
        "bold": (0x1D5D4, 0x1D5EE, 0x1D7EC),
        "italic": (0x1D608, 0x1D622, None),
        "bold_italic": (0x1D63C, 0x1D656, 0x1D7EC),
    }
    upper_start, lower_start, digit_start = ranges[style]
    converted = []
    for char in str(value):
        if "A" <= char <= "Z":
            converted.append(chr(upper_start + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            converted.append(chr(lower_start + ord(char) - ord("a")))
        elif digit_start is not None and "0" <= char <= "9":
            converted.append(chr(digit_start + ord(char) - ord("0")))
        else:
            converted.append(char)
    return "".join(converted)


def _render_text_styles(value: str) -> str:
    rendered = str(value).replace(r"\*", _ESCAPED_ASTERISK)
    rendered = _BOLD_ITALIC_RE.sub(
        lambda match: _stylize_ascii(match.group(1), "bold_italic"), rendered
    )
    rendered = _BOLD_RE.sub(
        lambda match: _stylize_ascii(match.group(1), "bold"), rendered
    )
    rendered = _ITALIC_RE.sub(
        lambda match: _stylize_ascii(match.group(1), "italic"), rendered
    )
    return rendered.replace(_ESCAPED_ASTERISK, "*")


def render_custom_command(text: str, username: str, channel: str, count: int,
                          args: str = "", display_name: str = None,
                          user_count: int = 1, command_name: str = "") -> str:
    """Expand supported placeholders without evaluating user-authored code."""
    raw_args = str(args or "").strip()
    target_match = re.match(r"^@?([A-Za-z0-9_]{1,25})(?:\s|$)", raw_args)
    target_name = target_match.group(1) if target_match else username
    target = f"@{target_name}"
    remaining_args = raw_args[target_match.end():].strip() if target_match else raw_args

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
        ("$rest", remaining_args),
        ("$args", raw_args),
        ("$user", username),
    )
    for variable, value in variables:
        rendered = rendered.replace(variable, str(value))
    rendered = _render_text_styles(rendered)
    return " ".join(
        rendered.replace("\r", " ").replace("\n", " ").split()
    )[:TWITCH_MESSAGE_LIMIT]
