from types import SimpleNamespace

import discord

import reaction_roles


def _entry(**overrides):
    entry = {
        "message_id": 123456789,
        "guild_id": 987654321,
        "channel_id": 111,
        "title": "Choose the games you play",
        "type": "modal",
        "only_add": False,
        "max_roles": None,
        "roles": [
            {"label": "Overwatch", "role_id": 10, "emoji": "🎮"},
            {"label": "Minecraft", "role_id": 20, "emoji": None},
            {"label": "Fortnite", "role_id": 30, "emoji": None},
        ],
    }
    entry.update(overrides)
    return entry


def test_modal_panel_builds_one_persistent_launch_button():
    view = reaction_roles._build_view(_entry(), bot=SimpleNamespace())

    assert len(view.children) == 1
    button = view.children[0]
    assert isinstance(button, discord.ui.Button)
    assert button.label == "Choose Your Roles"
    assert button.custom_id == "rr_modal_123456789"


def test_existing_dropdown_panel_still_builds_its_select_menu():
    view = reaction_roles._build_view(_entry(type="dropdown"), bot=SimpleNamespace())

    assert len(view.children) == 1
    select = view.children[0]
    assert isinstance(select, discord.ui.Select)
    assert select.custom_id == "rr_select_123456789"
    assert [option.label for option in select.options] == [
        "Overwatch", "Minecraft", "Fortnite"
    ]


def test_existing_button_panel_still_builds_one_button_per_role():
    view = reaction_roles._build_view(_entry(type="buttons"), bot=SimpleNamespace())

    assert len(view.children) == 3
    assert all(isinstance(item, discord.ui.Button) for item in view.children)
    assert [item.label for item in view.children] == [
        "Overwatch", "Minecraft", "Fortnite"
    ]


def test_modal_prefills_options_from_members_real_roles():
    member = SimpleNamespace(
        id=42,
        roles=[SimpleNamespace(id=10), SimpleNamespace(id=30)],
    )

    modal = reaction_roles.ReactionRolePickerModal(_entry(), member)

    assert modal.role_select.max_values == 3
    assert [option.default for option in modal.role_select.options] == [True, False, True]
    assert modal.role_select.required is False
    assert modal.role_select.min_values == 0


def test_modal_does_not_hide_existing_roles_above_new_limit():
    member = SimpleNamespace(
        id=42,
        roles=[SimpleNamespace(id=10), SimpleNamespace(id=20)],
    )

    modal = reaction_roles.ReactionRolePickerModal(_entry(max_roles=1), member)

    assert modal.role_select.max_values == 2
    assert modal.validation_max == 2
    assert [option.default for option in modal.role_select.options] == [True, True, False]
