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
        "$target / $targetname / $args / $count / $usercount"
    )
    assert result == (
        "CallerDisplay (caller) used !test in streamer: "
        "@Target / Target / @Target extra words / 42 / 3"
    )


def test_target_falls_back_to_caller_when_no_argument_is_given():
    assert render("$user hugs $target", args="") == "caller hugs @caller"


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
