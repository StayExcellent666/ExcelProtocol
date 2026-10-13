from pathlib import Path

from twitch_command_variables import render_custom_command, TWITCH_MESSAGE_LIMIT


def test_docker_image_includes_command_variable_module():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY *.py ./" in dockerfile


def render(text, *, args="@Target extra words"):
    return render_custom_command(
        text,
        "caller",
        "streamer",
        42,
        args=args,
        display_name="CallerDisplay",
        user_count=3,
        command_name="!test",
    )


def test_custom_command_context_variables_expand_without_name_collisions():
    result = render(
        "$displayname ($user) used $command in $channel: "
        "$target / $targetname / $args / $rest / $count / $usercount"
    )
    assert result == (
        "CallerDisplay (caller) used !test in streamer: "
        "@Target / Target / @Target extra words / extra words / 42 / 3"
    )


def test_target_falls_back_to_caller_when_no_argument_is_given():
    assert render("$user hugs $target", args="") == "caller hugs @caller"


def test_rest_excludes_the_target_but_args_remains_backward_compatible():
    result = render(
        "$user duels $target over [$rest] (full: [$args])",
        args="@Opponent for the crown",
    )
    assert result == (
        "caller duels @Opponent over [for the crown] "
        "(full: [@Opponent for the crown])"
    )


def test_rest_is_empty_when_only_a_target_is_given():
    assert render("$target [$rest]", args="Opponent") == "@Opponent []"


def test_markdown_style_wrappers_render_as_twitch_safe_unicode():
    assert render("**Bold 27** *Italic text*") == "𝗕𝗼𝗹𝗱 𝟮𝟳 𝘐𝘵𝘢𝘭𝘪𝘤 𝘵𝘦𝘹𝘵"


def test_styles_can_wrap_expanded_variables():
    assert render("**$displayname** duels *$targetname*") == (
        "𝗖𝗮𝗹𝗹𝗲𝗿𝗗𝗶𝘀𝗽𝗹𝗮𝘆 duels 𝘛𝘢𝘳𝘨𝘦𝘵"
    )


def test_triple_markers_render_bold_italic_and_escaped_markers_stay_literal():
    assert render(r"***Winner*** \*not styled\*") == "𝙒𝙞𝙣𝙣𝙚𝙧 *not styled*"


def test_random_number_stays_inside_requested_range():
    for _ in range(20):
        value = int(render("$random(9,4)"))
        assert 4 <= value <= 9


def test_random_choice_uses_one_of_the_safe_options():
    assert render("$choice(red|blue|green)") in {"red", "blue", "green"}


def test_rendered_response_is_single_line_and_twitch_sized():
    result = render(("hello\n" * 200) + "$args")
    assert "\n" not in result
    assert len(result) == TWITCH_MESSAGE_LIMIT
