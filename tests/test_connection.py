"""Tests for the connection layer."""

import pytest

from nuke_mcp import connection


def test_connect_and_handshake(mock_server):
    _, port = mock_server
    version = connection.connect("localhost", port)
    assert version.major == 15
    assert version.minor == 1
    assert version.is_nukex
    assert str(version) == "NukeX 15.1v3"
    connection.disconnect()


def test_connect_sets_connected(mock_server):
    _, port = mock_server
    assert not connection.is_connected()
    connection.connect("localhost", port)
    assert connection.is_connected()
    connection.disconnect()
    assert not connection.is_connected()


def test_ping(connected):
    assert connection.ping()


def test_send_command(connected):
    result = connection.send("get_script_info")
    assert result["fps"] == 24.0
    assert result["first_frame"] == 1001


def test_send_unknown_command(connected):
    with pytest.raises(connection.CommandError, match="unknown command"):
        connection.send("nonexistent_command")


def test_version_gating():
    v = connection.NukeVersion(15, 1, 3, "Nuke")
    assert not v.is_nukex
    assert v.at_least(15, 0)
    assert v.at_least(15, 1)
    assert not v.at_least(16, 0)

    vx = connection.NukeVersion(16, 0, 1, "NukeX")
    assert vx.is_nukex
    assert vx.at_least(15, 0)
    assert vx.at_least(16, 0)


def test_version_from_handshake():
    v = connection.NukeVersion.from_handshake({"nuke_version": "16.0v2", "variant": "NukeX"})
    assert v.major == 16
    assert v.minor == 0
    assert v.patch == 2
    assert v.variant == "NukeX"
