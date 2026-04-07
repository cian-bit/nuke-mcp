"""Tests for script tools."""

from nuke_mcp import connection


def test_get_script_info(connected):
    result = connection.send("get_script_info")
    assert result["fps"] == 24.0
    assert result["first_frame"] == 1001
    assert result["last_frame"] == 1100


def test_set_frame_range(connected):
    result = connection.send("set_frame_range", first=1, last=100)
    assert result["first"] == 1
    assert result["last"] == 100


def test_save_script(connected):
    result = connection.send("save_script")
    assert "saved" in result


def test_load_script(connected):
    result = connection.send("load_script", path="/tmp/other.nk")
    assert result["loaded"] == "/tmp/other.nk"
