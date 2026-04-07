"""Tests for knob tools."""

import pytest

from nuke_mcp import connection


def test_set_and_get_knob(connected):
    connection.send("create_node", type="Grade", name="g")
    connection.send("set_knob", node="g", knob="mix", value=0.75)
    result = connection.send("get_knob", node="g", knob="mix")
    assert result["value"] == 0.75
    assert not result["default"]


def test_get_default_knob(connected):
    connection.send("create_node", type="Grade", name="g")
    result = connection.send("get_knob", node="g", knob="white")
    assert result["default"]


def test_set_knob_nonexistent_node(connected):
    with pytest.raises(connection.CommandError):
        connection.send("set_knob", node="nope", knob="mix", value=1)


def test_set_knobs_batch(connected):
    connection.send("create_node", type="Grade", name="g1")
    connection.send("create_node", type="Blur", name="b1")
    result = connection.send(
        "set_knobs",
        operations=[
            {"node": "g1", "knob": "mix", "value": 0.5},
            {"node": "b1", "knob": "size", "value": 10},
        ],
    )
    assert result["count"] == 2
    # verify values stuck
    assert connection.send("get_knob", node="g1", knob="mix")["value"] == 0.5
    assert connection.send("get_knob", node="b1", knob="size")["value"] == 10
