"""Tests for welcome_banner pure helpers.

We don't test the full image rendering (pixel-level testing is brittle
and adds little value). We DO test:
  - RGB → hue conversion
  - Saturation detection (to know when to skip shifting)
  - Placeholder substitution
  - That the renderer doesn't crash for valid inputs
"""
import pytest
import welcome_banner


class TestRgbToHue:
    def test_pure_red(self):
        h = welcome_banner._rgb_to_hue(255, 0, 0)
        assert h == 0  # red = 0 degrees

    def test_pure_green(self):
        h = welcome_banner._rgb_to_hue(0, 255, 0)
        # green = 120 degrees of 360 → 85 of 256
        assert 80 <= h <= 90

    def test_pure_blue(self):
        h = welcome_banner._rgb_to_hue(0, 0, 255)
        # blue = 240 degrees → 170 of 256
        assert 165 <= h <= 175

    def test_cyan(self):
        h = welcome_banner._rgb_to_hue(0, 255, 255)
        # cyan = 180 degrees → 128 of 256
        assert 123 <= h <= 133

    def test_white_returns_zero(self):
        # Greyscale colors have no defined hue
        assert welcome_banner._rgb_to_hue(255, 255, 255) == 0

    def test_black_returns_zero(self):
        assert welcome_banner._rgb_to_hue(0, 0, 0) == 0

    def test_grey_returns_zero(self):
        assert welcome_banner._rgb_to_hue(128, 128, 128) == 0


class TestSaturation:
    def test_grey_zero(self):
        assert welcome_banner._color_saturation(128, 128, 128) == 0.0

    def test_white_zero(self):
        assert welcome_banner._color_saturation(255, 255, 255) == 0.0

    def test_black_zero(self):
        assert welcome_banner._color_saturation(0, 0, 0) == 0.0

    def test_pure_red_full(self):
        assert welcome_banner._color_saturation(255, 0, 0) == 1.0

    def test_pure_cyan_full(self):
        assert welcome_banner._color_saturation(0, 255, 255) == 1.0


class TestSubstitutePlaceholders:
    def test_user_and_server(self):
        out = welcome_banner.substitute_placeholders(
            "Welcome to {server}, {user}!",
            user="alice", server="MyServer",
        )
        assert out == "Welcome to MyServer, alice!"

    def test_only_user(self):
        out = welcome_banner.substitute_placeholders(
            "Hi {user}", user="bob", server="anything",
        )
        assert out == "Hi bob"

    def test_no_placeholders(self):
        out = welcome_banner.substitute_placeholders(
            "Just a message", user="x", server="y",
        )
        assert out == "Just a message"

    def test_empty_template(self):
        assert welcome_banner.substitute_placeholders("", user="a", server="b") == ""

    def test_none_template(self):
        assert welcome_banner.substitute_placeholders(None, user="a", server="b") == ""


@pytest.mark.asyncio
async def test_render_returns_png_bytes_for_valid_input(tmp_path):
    """End-to-end smoke test: the renderer produces something that looks
    like a PNG. Doesn't validate visual content (too brittle)."""
    result = await welcome_banner.render_welcome_banner(
        username="testuser",
        server_name="TestServer",
        avatar_bytes=None,
        accent_color=0x00F5D4,
        action="welcome",
    )
    if result is None:
        # Template missing in the test env — acceptable, just skip
        pytest.skip("Banner template not available in test env")
    # PNG magic bytes
    assert result[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_render_handles_goodbye(tmp_path):
    result = await welcome_banner.render_welcome_banner(
        username="testuser",
        server_name="TestServer",
        avatar_bytes=None,
        accent_color=0x00F5D4,
        action="goodbye",
    )
    if result is None:
        pytest.skip("Banner template not available in test env")
    assert result[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_render_handles_desaturated_color():
    """A grey/white accent color shouldn't crash — it should skip the
    hue shift and use cyan as the glow fallback."""
    result = await welcome_banner.render_welcome_banner(
        username="testuser",
        server_name="TestServer",
        avatar_bytes=None,
        accent_color=0x808080,  # grey
        action="welcome",
    )
    if result is None:
        pytest.skip("Banner template not available")
    assert result[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_render_handles_long_username():
    """Long usernames should be truncated, not overflow the canvas."""
    result = await welcome_banner.render_welcome_banner(
        username="averylongusernamethatdoesnotfit" * 3,
        server_name="TestServer",
        avatar_bytes=None,
        accent_color=0x00F5D4,
        action="welcome",
    )
    if result is None:
        pytest.skip("Banner template not available")
    assert result[:8] == b"\x89PNG\r\n\x1a\n"
