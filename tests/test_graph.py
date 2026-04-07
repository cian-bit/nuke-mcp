"""Tests for graph tools via the mock server."""

import pytest

from nuke_mcp import connection


def test_create_node(connected):
    result = connection.send("create_node", type="Grade")
    assert result["type"] == "Grade"
    assert "name" in result


def test_create_node_with_name(connected):
    result = connection.send("create_node", type="Blur", name="my_blur")
    assert result["name"] == "my_blur"


def test_create_and_connect(connected):
    connection.send("create_node", type="Read", name="plate")
    result = connection.send("create_node", type="Grade", name="cc", connect_to="plate")
    assert result["name"] == "cc"


def test_delete_node(connected):
    connection.send("create_node", type="Grade", name="temp")
    result = connection.send("delete_node", name="temp")
    assert result["deleted"] == "temp"


def test_delete_nonexistent(connected):
    with pytest.raises(connection.CommandError):
        connection.send("delete_node", name="nope")


def test_find_nodes_by_type(connected):
    connection.send("create_node", type="Grade", name="g1")
    connection.send("create_node", type="Grade", name="g2")
    connection.send("create_node", type="Blur", name="b1")
    result = connection.send("find_nodes", type="Grade")
    assert result["count"] == 2


def test_find_nodes_by_pattern(connected):
    connection.send("create_node", type="Grade", name="hero_grade")
    connection.send("create_node", type="Grade", name="bg_grade")
    result = connection.send("find_nodes", pattern="hero")
    assert result["count"] == 1
    assert result["nodes"][0]["name"] == "hero_grade"


def test_list_nodes(connected):
    connection.send("create_node", type="Read", name="r1")
    connection.send("create_node", type="Grade", name="g1")
    result = connection.send("list_nodes")
    assert result["count"] == 2


def test_connect_nodes(connected):
    connection.send("create_node", type="Read", name="src")
    connection.send("create_node", type="Grade", name="dst")
    result = connection.send("connect_nodes", **{"from": "src", "to": "dst"})
    assert "src -> dst" in result["connected"]


def test_get_node_info(connected):
    connection.send("create_node", type="Grade", name="test_grade")
    connection.send("set_knob", node="test_grade", knob="mix", value=0.5)
    result = connection.send("get_node_info", name="test_grade")
    assert result["type"] == "Grade"
    assert result["knobs"]["mix"] == 0.5


def test_auto_layout(connected):
    connection.send("create_node", type="Read", name="r")
    connection.send("create_node", type="Grade", name="g")
    result = connection.send("auto_layout")
    assert result["laid_out"] == 2
