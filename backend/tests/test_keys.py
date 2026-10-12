"""Unit tests for key mapping used by agent press / live view (no Chrome required)."""

from browser_plugin.sessions import _KEYS, _press_key


class FakeCDP:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))


def test_enter_has_keycode_and_text():
    cdp = FakeCDP()
    _press_key(cdp, "Enter")
    down = cdp.calls[0][1]
    assert down["windowsVirtualKeyCode"] == _KEYS["Enter"] and down["text"] == "\r"
    assert cdp.calls[1][1]["type"] == "keyUp"


def test_printable_char_sends_text():
    cdp = FakeCDP()
    _press_key(cdp, "a")
    assert cdp.calls[0][1]["text"] == "a"
