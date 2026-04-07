from nuke_mcp import connection


def test_set_expression(connected):
    connection.send("create_node", type="Grade", name="ExprGrade")
    result = connection.send(
        "set_expression", node="ExprGrade", knob="white", expression="frame/100.0"
    )
    assert result["expression"] == "frame/100.0"


def test_clear_expression(connected):
    connection.send("create_node", type="Grade", name="ExprGrade2")
    connection.send("set_expression", node="ExprGrade2", knob="white", expression="frame/100.0")
    result = connection.send("clear_expression", node="ExprGrade2", knob="white")
    assert result["cleared"] == "white"


def test_set_keyframe(connected):
    connection.send("create_node", type="Grade", name="KeyGrade")
    result = connection.send("set_keyframe", node="KeyGrade", knob="white", frame=1, value=0.0)
    assert result["frame"] == 1
    assert result["value"] == 0.0


def test_list_keyframes(connected):
    connection.send("create_node", type="Grade", name="KeyGrade2")
    connection.send("set_keyframe", node="KeyGrade2", knob="white", frame=1, value=0.0)
    connection.send("set_keyframe", node="KeyGrade2", knob="white", frame=100, value=1.0)
    result = connection.send("list_keyframes", node="KeyGrade2", knob="white")
    assert len(result["keyframes"]) == 2
