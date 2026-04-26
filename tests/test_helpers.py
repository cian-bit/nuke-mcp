"""Tests for tools/_helpers.py and main_thread.py (Phase A2)."""

from __future__ import annotations

from typing import Any

from nuke_mcp import connection, main_thread
from nuke_mcp.tools._helpers import nuke_command


def test_nuke_command_success_path():
    """Successful return passes through with no envelope wrapping."""

    @nuke_command("test_op")
    def go() -> dict[str, Any]:
        return {"value": 42}

    out = go()
    assert out == {"value": 42}


def test_nuke_command_command_error_envelope():
    """CommandError with envelope must surface envelope fields."""
    env = {
        "error_class": "ValueError",
        "error_code": "E_BAD",
        "traceback": "Traceback (most recent call last):\n  ...",
        "duration_ms": 7,
        "request_id": "abcd1234",
    }

    @nuke_command("test_op")
    def go() -> dict[str, Any]:
        raise connection.CommandError("boom", envelope=env)

    out = go()
    assert out["status"] == "error"
    assert out["error"] == "boom"
    assert out["error_class"] == "ValueError"
    assert out["duration_ms"] == 7
    assert out["request_id"] == "abcd1234"
    assert out["traceback"].startswith("Traceback")
    assert out["error_code"] == "E_BAD"


def test_nuke_command_command_error_no_envelope():
    """CommandError without envelope still produces a structured dict."""

    @nuke_command("test_op")
    def go() -> dict[str, Any]:
        raise connection.CommandError("boom")

    out = go()
    assert out["status"] == "error"
    assert out["error_class"] == "CommandError"
    assert "duration_ms" in out
    assert "request_id" not in out


def test_nuke_command_connection_error():
    """ConnectionError is caught and reported with class field."""

    @nuke_command("test_op")
    def go() -> dict[str, Any]:
        raise connection.ConnectionError("not connected")

    out = go()
    assert out["status"] == "error"
    assert out["error_class"] == "ConnectionError"
    assert "not connected to Nuke" in out["error"]
    assert "duration_ms" in out


def test_nuke_command_generic_exception():
    """Generic Exception returns error_class=type-name."""

    @nuke_command("test_op")
    def go() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    out = go()
    assert out["status"] == "error"
    assert out["error_class"] == "RuntimeError"
    assert out["error"] == "kaboom"
    assert "duration_ms" in out


# -- main_thread.run_on_main --


def test_run_on_main_dispatches_via_send(connected, monkeypatch):
    """run_on_main wraps connection.send with the timeout class."""
    captured: list[tuple[str, str, dict]] = []

    real_send = connection.send

    def spy(command, _class="read", **params):
        captured.append((command, _class, dict(params)))
        return real_send(command, _class=_class, **params)

    monkeypatch.setattr(connection, "send", spy)
    out = main_thread.run_on_main("get_script_info", {}, timeout_class="mutate")
    assert out["fps"] == 24.0
    assert captured == [("get_script_info", "mutate", {})]


def test_run_on_main_default_timeout_class(connected, monkeypatch):
    """Default timeout_class is 'mutate' per A3 typed-handler convention."""
    captured: list[str] = []
    real_send = connection.send

    def spy(command, _class="read", **params):
        captured.append(_class)
        return real_send(command, _class=_class, **params)

    monkeypatch.setattr(connection, "send", spy)
    main_thread.run_on_main("get_script_info")
    assert captured == ["mutate"]


def test_run_on_main_forwards_params(connected, monkeypatch):
    captured: list[dict] = []
    real_send = connection.send

    def spy(command, _class="read", **params):
        captured.append(dict(params))
        return real_send(command, _class=_class, **params)

    monkeypatch.setattr(connection, "send", spy)
    main_thread.run_on_main("create_node", {"type": "Blur", "name": "MyBlur"})
    assert captured == [{"type": "Blur", "name": "MyBlur"}]
