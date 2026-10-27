from types import SimpleNamespace

import discord

import reaction_roles


def _entry(**overrides):
    entry = {
        "message_id": 123456789,
        "guild_id": 987654321,
        "channel_id": 111,
        "title": "Choose the games you play",
        "type": "dropdown",
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


def test_retired_modal_panel_is_converted_to_dropdown():
    legacy_entry = _entry(type="modal")

    class FakeDB:
        def __init__(self):
            self.saved = None

        def rr_get_all(self):
            return [legacy_entry]

        def rr_save(self, **kwargs):
            self.saved = kwargs

    bot = SimpleNamespace(db=FakeDB())

    converted = reaction_roles._retire_modal_panel(bot, legacy_entry)
    view = reaction_roles._build_view(legacy_entry, bot)

    assert converted is True
    assert legacy_entry["type"] == "dropdown"
    assert bot.db.saved["rr_type"] == "dropdown"
    assert isinstance(view.children[0], discord.ui.Select)
