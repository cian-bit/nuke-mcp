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


def test_read_selected_returns_only_selected(connected):
    connection.send("create_node", type="Grade", name="SelectedGrade")
    connection.send("create_node", type="Blur", name="NotSelected")
    connected.selected = {"SelectedGrade"}

    result = connection.send("read_selected")
    names = [n["name"] for n in result["nodes"]]
    assert "SelectedGrade" in names
    assert "NotSelected" not in names


def test_read_selected_empty(connected):
    connection.send("create_node", type="Grade", name="Orphan")
    result = connection.send("read_selected")
    assert result["count"] == 0


def test_read_comp_summary_mode(connected):
    connection.send("create_node", type="Grade", name="g")
    connection.send("set_knob", node="g", knob="mix", value=0.5)
    result = connection.send("read_comp", summary=True)
    g_node = next(n for n in result["nodes"] if n["name"] == "g")
    assert "knobs" not in g_node


def test_read_comp_type_filter(connected):
    connection.send("create_node", type="Grade", name="g1")
    connection.send("create_node", type="Blur", name="b1")
    connection.send("create_node", type="Grade", name="g2")
    result = connection.send("read_comp", type="Grade")
    assert result["count"] == 2
    assert all(n["type"] == "Grade" for n in result["nodes"])


def test_read_comp_pagination(connected):
    for i in range(5):
        connection.send("create_node", type="Grade", name=f"g{i}")
    result = connection.send("read_comp", offset=2, limit=2)
    assert result["count"] == 2
    assert result["total"] == 5
