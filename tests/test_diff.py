from nuke_mcp import connection


def test_snapshot_and_diff(connected):
    connection.send("create_node", type="Grade", name="Before")
    snap = connection.send("snapshot_comp")
    assert "snapshot_id" in snap

    connection.send("create_node", type="Blur", name="After")
    diff = connection.send("diff_comp", snapshot_id=snap["snapshot_id"])
    added_names = [n["name"] for n in diff["added"]]
    assert "After" in added_names
    assert len(diff["removed"]) == 0


def test_snapshot_limit(connected):
    # take 6 snapshots, oldest should be evicted
    for _i in range(6):
        connection.send("snapshot_comp")
    # first snapshot (id "1") should be gone
    import pytest

    from nuke_mcp.connection import CommandError

    with pytest.raises(CommandError):
        connection.send("diff_comp", snapshot_id="1")
