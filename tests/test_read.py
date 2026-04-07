"""Tests for read_comp and related tools."""

from nuke_mcp import connection


def test_read_empty_comp(connected):
    result = connection.send("read_comp")
    assert result["count"] == 0
    assert result["nodes"] == []


def test_read_comp_with_nodes(connected):
    connection.send("create_node", type="Read", name="plate")
    connection.send("create_node", type="Grade", name="cc", connect_to="plate")
    result = connection.send("read_comp")
    assert result["count"] == 2

    names = {n["name"] for n in result["nodes"]}
    assert "plate" in names
    assert "cc" in names


def test_read_comp_shows_connections(connected):
    connection.send("create_node", type="Read", name="src")
    connection.send("create_node", type="Grade", name="dst", connect_to="src")
    result = connection.send("read_comp")

    dst_node = next(n for n in result["nodes"] if n["name"] == "dst")
    assert "inputs" in dst_node
    assert "src" in dst_node["inputs"]


def test_read_comp_shows_knobs(connected):
    connection.send("create_node", type="Grade", name="g")
    connection.send("set_knob", node="g", knob="mix", value=0.5)
    result = connection.send("read_comp")

    g_node = next(n for n in result["nodes"] if n["name"] == "g")
    assert g_node["knobs"]["mix"] == 0.5
