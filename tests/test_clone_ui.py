"""The public /start keyboard gives cloning the requested prominent action."""
import pytest

pytest.importorskip("pyrogram")
pytest.importorskip("imdb")

import utils


def test_clone_button_is_first_full_width_action(monkeypatch):
    monkeypatch.setattr(utils, "CLONE_CHILD_MODE", False)
    monkeypatch.setattr(utils.temp, "U_NAME", "SampleBot")

    keyboard = utils.start_buttons()

    assert len(keyboard.inline_keyboard[0]) == 1
    button = keyboard.inline_keyboard[0][0]
    assert "ᴄʀᴇᴀᴛᴇ ʏᴏᴜʀ ᴏᴡɴ ʙᴏᴛ" in button.text
    assert button.callback_data == "clone_start"


def test_clone_of_a_clone_does_not_show_a_clone_button(monkeypatch):
    monkeypatch.setattr(utils, "CLONE_CHILD_MODE", True)
    monkeypatch.setattr(utils.temp, "U_NAME", "SampleBot")

    keyboard = utils.start_buttons()

    assert all(button.callback_data != "clone_start" for row in keyboard.inline_keyboard for button in row)
