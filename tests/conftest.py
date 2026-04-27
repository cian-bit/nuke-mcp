"""Mock Nuke server + node graph for testing without a running Nuke instance.

The ``MockNukeServer`` mocks the addon wire protocol; ``MockNukeNode`` and
``MockNukeScript`` mock the in-process ``nuke`` Python API so tools that ship
``execute_python`` payloads can be exercised against an in-memory graph.

A4 brought ``MockNukeNode`` (ported from MockHouNode in
``houdini-mcp-beta/tests/conftest.py``) plus 30+ first-class node-type
factories. Tests opt into the registry via the ``mock_script`` fixture.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import threading
import time
from typing import Any

import pytest

# Load nuke_plugin/addon.py directly (its package __init__ pulls a Nuke-only
# import). Used by the mock server to mirror the addon's setup_write path
# policy without inlining the validation rules in two places.
_ADDON_PATH = pathlib.Path(__file__).resolve().parents[1] / "nuke_plugin" / "addon.py"
_addon_spec = importlib.util.spec_from_file_location("_nuke_addon_for_tests", _ADDON_PATH)
assert _addon_spec is not None and _addon_spec.loader is not None
_addon_module = importlib.util.module_from_spec(_addon_spec)
_addon_spec.loader.exec_module(_addon_module)
_validate_write_path = _addon_module._validate_write_path
PathPolicyViolation = _addon_module.PathPolicyViolation

# ---------------------------------------------------------------------------
# MockNukeNode + supporting knob types
# ---------------------------------------------------------------------------


class MockKnob:
    """Mock Nuke knob. Default scalar knob with value/expression/animation."""

    _knob_class = "Double_Knob"
    _dimensions = 1

    def __init__(self, name: str, value: Any = 0.0, default: Any | None = None) -> None:
        self._name = name
        self._value = value
        self._default = default if default is not None else value
        self._expression: str | None = None
        self._keys: list[tuple[float, Any]] = []
        self._animated = False

    # -- basics --

    def name(self) -> str:
        return self._name

    def Class(self) -> str:  # noqa: N802 - Nuke API casing
        return self._knob_class

    def value(self) -> Any:
        return self._value

    def setValue(self, v: Any) -> bool:  # noqa: N802 - Nuke API casing
        self._value = v
        return True

    def getValue(self) -> Any:  # noqa: N802 - Nuke API casing
        return self._value

    def evaluate(self) -> Any:
        # default scalar evaluate returns the current value
        return self._value

    # -- expressions --

    def setExpression(self, expr: str, channel: int = 0) -> bool:  # noqa: N802
        self._expression = expr
        return True

    def clearAnimated(self, channel: int = -1) -> bool:  # noqa: N802
        self._animated = False
        self._keys = []
        return True

    def hasExpression(self) -> bool:  # noqa: N802
        return self._expression is not None

    def expression(self) -> str | None:
        return self._expression

    # -- animation / keys --

    def isAnimated(self, channel: int = 0) -> bool:  # noqa: N802
        return self._animated

    def setAnimated(self) -> None:  # noqa: N802
        self._animated = True

    def isKey(self, time: float | None = None) -> bool:  # noqa: N802
        if time is None:
            return self._animated
        return any(abs(k[0] - time) < 1e-6 for k in self._keys)

    def setKey(self, time: float, value: Any) -> None:  # noqa: N802
        self._animated = True
        self._keys = [k for k in self._keys if abs(k[0] - time) > 1e-6]
        self._keys.append((time, value))
        self._keys.sort(key=lambda k: k[0])

    def numKeys(self) -> int:  # noqa: N802
        return len(self._keys)

    def getKeyTime(self, index: int) -> float:  # noqa: N802
        return self._keys[index][0]

    def getKeyValue(self, index: int) -> Any:  # noqa: N802
        return self._keys[index][1]

    # -- defaults --

    def isDefault(self) -> bool:  # noqa: N802
        return self._value == self._default and not self._animated and self._expression is None

    def defaultValue(self) -> Any:  # noqa: N802
        return self._default

    def dimensions(self) -> int:
        return self._dimensions


class MockFileKnob(MockKnob):
    """File_Knob -- evaluate() expands TCL/python like ``[python ...]``."""

    _knob_class = "File_Knob"

    def evaluate(self) -> Any:
        # in real Nuke evaluate() expands TCL; for the mock we return the value
        return self._value


class MockFormatKnob(MockKnob):
    """Format_Knob -- value is a string label."""

    _knob_class = "Format_Knob"

    def __init__(self, name: str, value: str = "HD 1920x1080") -> None:
        super().__init__(name, value)


class MockArrayKnob(MockKnob):
    """N-dim array knob (XY_Knob, XYZ_Knob, Color_Knob)."""

    def __init__(
        self,
        name: str,
        value: list[float] | tuple[float, ...] | None = None,
        dims: int = 2,
        knob_class: str = "XY_Knob",
    ) -> None:
        v = list(value) if value is not None else [0.0] * dims
        super().__init__(name, v, default=list(v))
        self._dimensions = dims
        self._knob_class = knob_class

    def setValue(self, v: Any, channel: int | None = None) -> bool:  # noqa: N802
        if channel is not None and isinstance(self._value, list):
            self._value[channel] = v
        else:
            self._value = list(v) if isinstance(v, list | tuple) else v
        return True


class MockBoolKnob(MockKnob):
    _knob_class = "Boolean_Knob"


class MockChannelKnob(MockKnob):
    _knob_class = "Channel_Knob"


class MockEnumKnob(MockKnob):
    _knob_class = "Enumeration_Knob"

    def __init__(self, name: str, value: str = "", values: list[str] | None = None) -> None:
        super().__init__(name, value)
        self._values = values or []

    def values(self) -> list[str]:
        return list(self._values)


class MockNukeNode:
    """Mock Nuke node. Mirrors the surface of the real ``nuke.Node`` enough
    to drive comp/render/channel tools through ``execute_python`` payloads.

    Construct directly for custom shapes, or use one of the type factories
    (``MockNukeNode.read``, ``.write``, etc.) for sensible defaults.
    """

    def __init__(
        self,
        name: str = "Node1",
        node_class: str = "NoOp",
        knobs: dict[str, MockKnob] | None = None,
        xpos: int = 0,
        ypos: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._class = node_class
        self._knobs: dict[str, MockKnob] = knobs if knobs is not None else {}
        self._xpos = xpos
        self._ypos = ypos
        self._metadata: dict[str, Any] = metadata or {}
        self._inputs: list[MockNukeNode | None] = []
        self._dependents: list[MockNukeNode] = []
        self._selected = False
        self._disabled = False
        self._error = False
        self._warning = False

        # ensure baseline knobs every Nuke node carries
        self._knobs.setdefault("name", MockKnob("name", value=name))
        self._knobs.setdefault("xpos", MockKnob("xpos", value=xpos))
        self._knobs.setdefault("ypos", MockKnob("ypos", value=ypos))
        self._knobs.setdefault("disable", MockBoolKnob("disable", value=False))
        self._knobs.setdefault("label", MockKnob("label", value=""))

    # -- identity --

    def name(self) -> str:
        return self._name

    def setName(self, name: str) -> None:  # noqa: N802
        self._name = name
        if "name" in self._knobs:
            self._knobs["name"].setValue(name)

    def Class(self) -> str:  # noqa: N802 - Nuke API casing
        return self._class

    def fullName(self) -> str:  # noqa: N802
        return self._name

    # -- position --

    def xpos(self) -> int:
        return self._xpos

    def ypos(self) -> int:
        return self._ypos

    def setXYpos(self, x: int, y: int) -> None:  # noqa: N802
        self._xpos = int(x)
        self._ypos = int(y)
        if "xpos" in self._knobs:
            self._knobs["xpos"].setValue(int(x))
        if "ypos" in self._knobs:
            self._knobs["ypos"].setValue(int(y))

    # -- inputs / outputs --

    def inputs(self) -> int:
        # Nuke's inputs() returns the count of valid input slots
        return len(self._inputs)

    def maximumInputs(self) -> int:  # noqa: N802
        return max(2, len(self._inputs))

    def minimumInputs(self) -> int:  # noqa: N802
        return 0

    def input(self, idx: int) -> MockNukeNode | None:
        if idx < 0 or idx >= len(self._inputs):
            return None
        return self._inputs[idx]

    def setInput(self, idx: int, node: MockNukeNode | None) -> bool:  # noqa: N802
        while len(self._inputs) <= idx:
            self._inputs.append(None)
        old = self._inputs[idx]
        if old is not None and self in old._dependents:
            old._dependents.remove(self)
        self._inputs[idx] = node
        if node is not None and self not in node._dependents:
            node._dependents.append(self)
        return True

    def dependent(self, what: int = 0, forceEvaluate: bool = True) -> list[MockNukeNode]:  # noqa: N803
        return list(self._dependents)

    def dependencies(self, what: int = 0) -> list[MockNukeNode]:
        return [n for n in self._inputs if n is not None]

    # -- knobs --

    def knob(self, name: str) -> MockKnob | None:
        return self._knobs.get(name)

    def knobs(self) -> dict[str, MockKnob]:
        return dict(self._knobs)

    def addKnob(self, knob: MockKnob) -> None:  # noqa: N802
        self._knobs[knob.name()] = knob

    def __getitem__(self, name: str) -> MockKnob:
        if name not in self._knobs:
            # mimic Nuke -- create on demand for unknown knobs (Nuke is permissive)
            self._knobs[name] = MockKnob(name, value="")
        return self._knobs[name]

    def __contains__(self, name: str) -> bool:
        return name in self._knobs

    # -- metadata --

    def metadata(self, key: str | None = None) -> Any:
        if key is None:
            return dict(self._metadata)
        return self._metadata.get(key)

    def setMetadata(self, key: str, value: Any) -> None:  # noqa: N802
        self._metadata[key] = value

    # -- flags / state --

    def selected(self) -> bool:
        return self._selected

    def setSelected(self, value: bool) -> None:  # noqa: N802
        self._selected = bool(value)

    def isDisabled(self) -> bool:  # noqa: N802
        return self._disabled

    def setDisabled(self, value: bool) -> None:  # noqa: N802
        self._disabled = bool(value)

    def hasError(self) -> bool:  # noqa: N802
        return self._error

    def error(self) -> bool:
        return self._error

    def warning(self) -> bool:
        return self._warning

    # -- serialise (used by snapshots / digests) --

    def to_dict(self) -> dict[str, Any]:
        knobs_out: dict[str, Any] = {}
        for k_name, k in self._knobs.items():
            if k_name in ("name", "xpos", "ypos"):
                continue
            if not k.isDefault():
                knobs_out[k_name] = k.value()
        return {
            "name": self._name,
            "type": self._class,
            "x": self._xpos,
            "y": self._ypos,
            "knobs": knobs_out,
            "inputs": [n.name() if n else None for n in self._inputs],
        }

    # ------------------------------------------------------------------
    # First-class node-type factories
    # ------------------------------------------------------------------

    @classmethod
    def read(
        cls, name: str = "Read1", file: str = "", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "file": MockFileKnob("file", value=file),
            "first": MockKnob("first", value=1001),
            "last": MockKnob("last", value=1100),
            "format": MockFormatKnob("format", value="HD 1920x1080"),
            "colorspace": MockKnob("colorspace", value="default"),
            "channels": MockChannelKnob("channels", value="rgba"),
            "missing_frames": MockKnob("missing_frames", value="error"),
        }
        return cls(name=name, node_class="Read", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def write(
        cls,
        name: str = "Write1",
        file: str = "",
        file_type: str = "exr",
        xpos: int = 0,
        ypos: int = 0,
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "file": MockFileKnob("file", value=file),
            "file_type": MockEnumKnob(
                "file_type", value=file_type, values=["exr", "png", "jpg", "dpx"]
            ),
            "first": MockKnob("first", value=1001),
            "last": MockKnob("last", value=1100),
            "channels": MockChannelKnob("channels", value="rgba"),
            "colorspace": MockKnob("colorspace", value="scene_linear"),
            "create_directories": MockBoolKnob("create_directories", value=True),
        }
        return cls(name=name, node_class="Write", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def merge2(
        cls, name: str = "Merge1", operation: str = "over", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "operation": MockEnumKnob(
                "operation", value=operation, values=["over", "plus", "multiply", "screen"]
            ),
            "mix": MockKnob("mix", value=1.0),
            "bbox": MockEnumKnob("bbox", value="union"),
        }
        node = cls(name=name, node_class="Merge2", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def blur(
        cls, name: str = "Blur1", size: float = 1.0, xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "size": MockKnob("size", value=size),
            "channels": MockChannelKnob("channels", value="rgba"),
            "filter": MockEnumKnob("filter", value="gaussian"),
        }
        return cls(name=name, node_class="Blur", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def roto(cls, name: str = "Roto1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "curves": MockKnob("curves", value=""),
            "output": MockEnumKnob("output", value="alpha"),
        }
        return cls(name=name, node_class="Roto", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def rotopaint(cls, name: str = "RotoPaint1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "curves": MockKnob("curves", value=""),
            "output": MockEnumKnob("output", value="rgba"),
        }
        return cls(name=name, node_class="RotoPaint", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def tracker4(
        cls, name: str = "Tracker1", num_tracks: int = 4, xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "transform": MockEnumKnob("transform", value="match-move"),
            "reference_frame": MockKnob("reference_frame", value=1001),
        }
        for i in range(1, num_tracks + 1):
            knobs[f"track{i}"] = MockArrayKnob(f"track{i}", value=[0.0, 0.0])
        return cls(name=name, node_class="Tracker4", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def cameratracker(
        cls, name: str = "CameraTracker1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "numberFeatures": MockKnob("numberFeatures", value=300),
            "solveMethod": MockEnumKnob("solveMethod", value="auto"),
            "rangeFirst": MockKnob("rangeFirst", value=1001),
            "rangeLast": MockKnob("rangeLast", value=1100),
        }
        return cls(name=name, node_class="CameraTracker", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def planartracker(
        cls, name: str = "PlanarTracker1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "referenceFrame": MockKnob("referenceFrame", value=1001),
            "rootWarp": MockEnumKnob("rootWarp", value="perspective"),
        }
        return cls(name=name, node_class="PlanarTracker", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def shuffle(cls, name: str = "Shuffle1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "in1": MockChannelKnob("in1", value="rgba"),
            "out1": MockChannelKnob("out1", value="rgba"),
        }
        return cls(name=name, node_class="Shuffle2", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def scanlinerender(
        cls, name: str = "ScanlineRender1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "samples": MockKnob("samples", value=1),
            "MB_samples": MockKnob("MB_samples", value=1),
            "projection_mode": MockEnumKnob("projection_mode", value="render camera"),
        }
        node = cls(name=name, node_class="ScanlineRender", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None, None]
        return node

    @classmethod
    def deeprecolor(cls, name: str = "DeepRecolor1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "target_input_alpha": MockBoolKnob("target_input_alpha", value=True),
        }
        node = cls(name=name, node_class="DeepRecolor", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def deepmerge(
        cls, name: str = "DeepMerge1", op: str = "over", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "operation": MockEnumKnob("operation", value=op, values=["over", "holdout", "combine"]),
        }
        node = cls(name=name, node_class="DeepMerge", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def deepholdout(cls, name: str = "DeepHoldout1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {}
        node = cls(name=name, node_class="DeepHoldout2", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def deeptransform(
        cls, name: str = "DeepTransform1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "translate": MockArrayKnob(
                "translate", value=[0.0, 0.0, 0.0], dims=3, knob_class="XYZ_Knob"
            ),
        }
        return cls(name=name, node_class="DeepTransform", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def copycat(cls, name: str = "CopyCat1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "modelFile": MockFileKnob("modelFile", value=""),
            "epochs": MockKnob("epochs", value=10000),
            "inLayer": MockChannelKnob("inLayer", value="rgb"),
            "outLayer": MockChannelKnob("outLayer", value="rgb"),
        }
        return cls(name=name, node_class="CopyCat", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def stmap(cls, name: str = "STMap1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "channels": MockChannelKnob("channels", value="rgba"),
            "uv": MockChannelKnob("uv", value="forward"),
        }
        node = cls(name=name, node_class="STMap", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def idistort(cls, name: str = "IDistort1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "uv": MockChannelKnob("uv", value="forward"),
        }
        node = cls(name=name, node_class="IDistort", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def smartvector(cls, name: str = "SmartVector1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "frame": MockKnob("frame", value=1001),
            "rangeFirst": MockKnob("rangeFirst", value=1001),
            "rangeLast": MockKnob("rangeLast", value=1100),
        }
        return cls(name=name, node_class="SmartVector", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def vectordistort(
        cls, name: str = "VectorDistort1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "uv": MockChannelKnob("uv", value="motion"),
            "referenceFrame": MockKnob("referenceFrame", value=1001),
        }
        return cls(name=name, node_class="VectorDistort", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def grade(cls, name: str = "Grade1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "white": MockArrayKnob(
                "white", value=[1.0, 1.0, 1.0, 1.0], dims=4, knob_class="Color_Knob"
            ),
            "black": MockArrayKnob(
                "black", value=[0.0, 0.0, 0.0, 0.0], dims=4, knob_class="Color_Knob"
            ),
            "gain": MockArrayKnob(
                "gain", value=[1.0, 1.0, 1.0, 1.0], dims=4, knob_class="Color_Knob"
            ),
            "mix": MockKnob("mix", value=1.0),
        }
        return cls(name=name, node_class="Grade", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def colorcorrect(
        cls, name: str = "ColorCorrect1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "saturation": MockKnob("saturation", value=1.0),
            "contrast": MockKnob("contrast", value=1.0),
            "gamma": MockKnob("gamma", value=1.0),
        }
        return cls(name=name, node_class="ColorCorrect", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def huecorrect(cls, name: str = "HueCorrect1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "hue": MockKnob("hue", value=""),
        }
        return cls(name=name, node_class="HueCorrect", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def ociocolorspace(
        cls, name: str = "OCIOColorSpace1", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "in_colorspace": MockKnob("in_colorspace", value="ACES - ACEScg"),
            "out_colorspace": MockKnob("out_colorspace", value="ACES - ACEScct"),
        }
        return cls(name=name, node_class="OCIOColorSpace", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def group(cls, name: str = "Group1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {}
        return cls(name=name, node_class="Group", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def backdrop(
        cls, name: str = "BackdropNode1", label: str = "", xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "label": MockKnob("label", value=label),
            "tile_color": MockKnob("tile_color", value=0x808080FF),
            "bdwidth": MockKnob("bdwidth", value=200),
            "bdheight": MockKnob("bdheight", value=150),
        }
        return cls(name=name, node_class="BackdropNode", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def switch(
        cls, name: str = "Switch1", which: int = 0, xpos: int = 0, ypos: int = 0
    ) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {"which": MockKnob("which", value=which)}
        node = cls(name=name, node_class="Switch", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def card(cls, name: str = "Card1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "translate": MockArrayKnob(
                "translate", value=[0.0, 0.0, 0.0], dims=3, knob_class="XYZ_Knob"
            ),
            "rotate": MockArrayKnob("rotate", value=[0.0, 0.0, 0.0], dims=3, knob_class="XYZ_Knob"),
        }
        return cls(name=name, node_class="Card3D", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def project3d(cls, name: str = "Project3D1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "project_on": MockEnumKnob("project_on", value="back"),
            "crop": MockBoolKnob("crop", value=True),
        }
        node = cls(name=name, node_class="Project3D2", knobs=knobs, xpos=xpos, ypos=ypos)
        node._inputs = [None, None]
        return node

    @classmethod
    def zdefocus(cls, name: str = "ZDefocus1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "math": MockEnumKnob("math", value="depth"),
            "channels": MockChannelKnob("channels", value="rgba"),
            "size": MockKnob("size", value=10.0),
            "depth": MockChannelKnob("depth", value="depth.z"),
        }
        return cls(name=name, node_class="ZDefocus2", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def relight(cls, name: str = "Relight1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "output": MockEnumKnob("output", value="rgb"),
        }
        return cls(name=name, node_class="ReLight", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def premult(cls, name: str = "Premult1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "channels": MockChannelKnob("channels", value="rgb"),
            "alpha": MockChannelKnob("alpha", value="alpha"),
        }
        return cls(name=name, node_class="Premult", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def filtererode(cls, name: str = "FilterErode1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "channels": MockChannelKnob("channels", value="alpha"),
            "size": MockKnob("size", value=-0.5),
            "filter": MockEnumKnob("filter", value="gaussian"),
        }
        return cls(name=name, node_class="FilterErode", knobs=knobs, xpos=xpos, ypos=ypos)

    @classmethod
    def edgeblur(cls, name: str = "EdgeBlur1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "size": MockKnob("size", value=3.0),
            "channels": MockChannelKnob("channels", value="alpha"),
        }
        return cls(name=name, node_class="EdgeBlur", knobs=knobs, xpos=xpos, ypos=ypos)

    # C1: deep_to_image factory. The other deep + tracker factories
    # already exist above (deeprecolor / deepmerge / deepholdout /
    # deeptransform / cameratracker / planartracker / tracker4); we only
    # add the one that wasn't there.
    @classmethod
    def deeptoimage(cls, name: str = "DeepToImage1", xpos: int = 0, ypos: int = 0) -> MockNukeNode:
        knobs: dict[str, MockKnob] = {
            "channels": MockChannelKnob("channels", value="rgba"),
        }
        return cls(name=name, node_class="DeepToImage", knobs=knobs, xpos=xpos, ypos=ypos)


# ---------------------------------------------------------------------------
# Registry: name -> MockNukeNode (mirrors nuke.toNode)
# ---------------------------------------------------------------------------


class MockNukeScript:
    """Live-graph registry of MockNukeNodes accessible by name.

    Used by tests that exercise tools through the addon path while still
    wanting an in-memory graph to inspect. Tests opt in via the
    ``mock_script`` fixture; existing dict-based tests are untouched.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, MockNukeNode] = {}

    def add(self, node: MockNukeNode) -> MockNukeNode:
        self._nodes[node.name()] = node
        return node

    def remove(self, name: str) -> bool:
        return self._nodes.pop(name, None) is not None

    def get(self, name: str) -> MockNukeNode | None:
        return self._nodes.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._nodes

    def __iter__(self):
        return iter(self._nodes.values())

    def __len__(self) -> int:
        return len(self._nodes)

    def names(self) -> list[str]:
        return list(self._nodes.keys())

    def all_nodes(self, node_class: str | None = None) -> list[MockNukeNode]:
        if node_class is None:
            return list(self._nodes.values())
        return [n for n in self._nodes.values() if n.Class() == node_class]

    def clear(self) -> None:
        self._nodes.clear()


# ---------------------------------------------------------------------------
# MockNukeServer -- wire-protocol fake
# ---------------------------------------------------------------------------


class MockNukeServer:
    """Fake Nuke socket server. Maintains a minimal node graph in memory
    so tests can verify the full command/response flow."""

    def __init__(self, port: int = 0):
        self.port = port
        self.nodes: dict[str, dict] = {}
        self.connections: dict[str, list[str | None]] = {}
        self.selected: set[str] = set()
        self.expressions: dict[str, dict[str, str]] = {}
        self.keyframes: dict[str, dict[str, list[dict]]] = {}
        self._snapshots: dict[str, dict] = {}
        self._snap_counter = 0
        # Recorded execute_python payloads. After A3 the comp/setup_write
        # tools no longer reach this path; the channels/roto/viewer/precomp
        # tools still do, and tests assert on the recorded payloads.
        self.executed_code: list[str] = []
        # A3: typed-handler call log. Each entry is (cmd, params) so tests
        # can assert ``("setup_keying", {...})`` round-tripped through the
        # wire without inspecting f-string blobs.
        self.typed_calls: list[tuple[str, dict]] = []
        # B7: counter spy for read_comp single-pass verification. Each time
        # the mock visits a node entry to serialize knobs we bump this.
        # test_speed.py asserts the counter equals the node count exactly
        # (one visit per node, not two).
        self.read_comp_knob_visits: int = 0
        # B7: scene_delta call log. Tests assert that on a no-change call
        # we returned the short-circuit path (no node enumeration).
        self.scene_delta_short_circuits: int = 0
        # B2 async render: record render_async / cancel_render payloads
        # so tests can assert the wire shape without observing the
        # mock server's internal threading.
        self.async_renders: list[dict] = []
        self.cancelled_renders: list[str | None] = []
        self.script_info = {
            "script": "/tmp/test.nk",
            "first_frame": 1001,
            "last_frame": 1100,
            "fps": 24.0,
            "format": "HD 1920x1080",
            "colorspace": "ACES",
            "node_count": 0,
        }
        # Optional MockNukeNode-backed registry. Default-empty; tests opt in
        # via the ``mock_script`` fixture which attaches one.
        self.script: MockNukeScript | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        self.port = self._sock.getsockname()[1]

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while self._running:
            assert self._sock is not None
            try:
                client, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self._handle(client)

    def _handle(self, client: socket.socket) -> None:
        # handshake
        handshake = {"nuke_version": "15.1v3", "variant": "NukeX", "pid": 12345}
        client.sendall(json.dumps(handshake).encode() + b"\n")

        buf = b""
        while self._running:
            try:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line)
                    resp = self._dispatch(msg)
                    client.sendall(json.dumps(resp).encode() + b"\n")
            except (OSError, json.JSONDecodeError):
                break
        client.close()

    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("type", "")
        params = msg.get("params", {})
        rid = msg.get("_request_id")

        handler = {
            "ping": self._ping,
            "get_script_info": self._get_script_info,
            "get_node_info": self._get_node_info,
            "create_node": self._create_node,
            "delete_node": self._delete_node,
            "modify_node": self._modify_node,
            "connect_nodes": self._connect_nodes,
            "find_nodes": self._find_nodes,
            "list_nodes": self._list_nodes,
            "get_knob": self._get_knob,
            "set_knob": self._set_knob,
            "auto_layout": self._auto_layout,
            "read_comp": self._read_comp,
            "read_selected": self._read_selected,
            "execute_python": self._execute_python,
            "render": self._render,
            "render_async": self._render_async,
            "cancel_render": self._cancel_render,
            "save_script": self._save_script,
            "load_script": self._load_script,
            "set_frame_range": self._set_frame_range,
            "view_node": self._view_node,
            "list_channels": self._list_channels,
            "set_expression": self._set_expression,
            "clear_expression": self._clear_expression,
            "set_keyframe": self._set_keyframe,
            "list_keyframes": self._list_keyframes,
            "snapshot_comp": self._snapshot_comp,
            "diff_comp": self._diff_comp,
            "create_nodes": self._create_nodes,
            "set_knobs": self._set_knobs,
            "disconnect_input": self._disconnect_input,
            "set_node_position": self._set_node_position,
            # A3 typed handlers
            "setup_keying": self._setup_keying,
            "setup_color_correction": self._setup_color_correction,
            "setup_merge": self._setup_merge,
            "setup_transform": self._setup_transform,
            "setup_denoise": self._setup_denoise,
            "setup_write": self._setup_write,
            # B7 scene digest
            "scene_digest": self._scene_digest,
            "scene_delta": self._scene_delta,
            # C1 tracking primitives
            "setup_camera_tracker": self._setup_camera_tracker,
            "setup_planar_tracker": self._setup_planar_tracker,
            "setup_tracker4": self._setup_tracker4,
            "bake_tracker_to_corner_pin": self._bake_tracker_to_corner_pin,
            "solve_3d_camera": self._solve_3d_camera,
            "bake_camera_to_card": self._bake_camera_to_card,
            # C5 tracking workflow macros
            "setup_spaceship_track_patch": self._setup_spaceship_track_patch,
            # C1 deep primitives
            "create_deep_recolor": self._create_deep_recolor,
            "create_deep_merge": self._create_deep_merge,
            "create_deep_holdout": self._create_deep_holdout,
            "create_deep_transform": self._create_deep_transform,
            "deep_to_image": self._deep_to_image,
        }.get(cmd)

        resp: dict[str, Any]
        if handler is None:
            resp = {"status": "error", "error": f"unknown command: {cmd}"}
            if rid is not None:
                resp["_request_id"] = rid
            return resp

        try:
            result = handler(params)
            resp = {"status": "ok", "result": result}
        except Exception as e:
            resp = {"status": "error", "error": str(e), "error_class": type(e).__name__}
        if rid is not None:
            resp["_request_id"] = rid
        return resp

    def _ping(self, p: dict) -> dict:
        return {"pong": True}

    def _get_script_info(self, p: dict) -> dict:
        self.script_info["node_count"] = len(self.nodes)
        return self.script_info

    def _get_node_info(self, p: dict) -> dict:
        name = p["name"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        node = self.nodes[name]
        return {
            "name": name,
            "type": node["type"],
            "inputs": self.connections.get(name, []),
            "knobs": node.get("knobs", {}),
            "error": False,
            "warning": False,
            "x": node.get("x", 0),
            "y": node.get("y", 0),
        }

    def _create_node(self, p: dict) -> dict:
        node_type = p["type"]
        name = p.get("name") or f"{node_type}1"
        # ensure unique name
        base = name
        i = 1
        while name in self.nodes:
            i += 1
            name = f"{base}{i}"

        self.nodes[name] = {
            "type": node_type,
            "knobs": p.get("knobs", {}),
            "x": p.get("position", [0, 0])[0] if p.get("position") else 0,
            "y": p.get("position", [0, 0])[1] if p.get("position") else 0,
        }
        self.connections[name] = []

        if p.get("connect_to") and p["connect_to"] in self.nodes:
            self.connections[name] = [p["connect_to"]]

        # mirror to MockNukeScript registry if attached
        if self.script is not None and name not in self.script:
            self.script.add(MockNukeNode(name=name, node_class=node_type))

        return {"name": name, "type": node_type, "x": 0, "y": 0}

    def _delete_node(self, p: dict) -> dict:
        name = p["name"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        del self.nodes[name]
        self.connections.pop(name, None)
        if self.script is not None:
            self.script.remove(name)
        return {"deleted": name}

    def _modify_node(self, p: dict) -> dict:
        name = p["name"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        node = self.nodes[name]
        if p.get("knobs"):
            node.setdefault("knobs", {}).update(p["knobs"])
        if p.get("new_name"):
            self.nodes[p["new_name"]] = self.nodes.pop(name)
            name = p["new_name"]
        return {"name": name, "type": node["type"]}

    def _connect_nodes(self, p: dict) -> dict:
        src = p["from"]
        dst = p["to"]
        if src not in self.nodes:
            raise ValueError(f"source node not found: {src}")
        if dst not in self.nodes:
            raise ValueError(f"target node not found: {dst}")
        idx = p.get("input", 0)
        conns = self.connections.setdefault(dst, [])
        while len(conns) <= idx:
            conns.append(None)
        conns[idx] = src
        return {"connected": f"{src} -> {dst}[{idx}]"}

    def _find_nodes(self, p: dict) -> dict:
        results = []
        for name, node in self.nodes.items():
            if p.get("type") and node["type"] != p["type"]:
                continue
            if p.get("pattern") and p["pattern"].lower() not in name.lower():
                continue
            results.append({"name": name, "type": node["type"], "error": False})
        return {"nodes": results, "count": len(results)}

    def _list_nodes(self, p: dict) -> dict:
        nodes = [{"name": n, "type": d["type"]} for n, d in self.nodes.items()]
        return {"nodes": nodes, "count": len(nodes)}

    def _get_knob(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        knobs = self.nodes[name].get("knobs", {})
        knob_name = p["knob"]
        return {
            "value": knobs.get(knob_name, 0),
            "type": "Double_Knob",
            "animated": False,
            "default": knob_name not in knobs,
        }

    def _set_knob(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        self.nodes[name].setdefault("knobs", {})[p["knob"]] = p["value"]
        return {"node": name, "knob": p["knob"], "value": p["value"]}

    def _auto_layout(self, p: dict) -> dict:
        return {"laid_out": len(self.nodes)}

    def _read_comp(self, p: dict) -> dict:
        all_nodes = []
        type_filter = p.get("type")
        summary = p.get("summary", False)
        for name, data in self.nodes.items():
            if type_filter and data["type"] != type_filter:
                continue
            # B7: count one knob visit per node entry. The single-pass
            # check in test_speed.py asserts visits == nodes-after-filter.
            self.read_comp_knob_visits += 1
            entry: dict[str, Any] = {"name": name, "type": data["type"]}
            conns = self.connections.get(name, [])
            if any(conns):
                entry["inputs"] = conns
            if not summary:
                knobs = data.get("knobs", {})
                if knobs:
                    entry["knobs"] = knobs
            all_nodes.append(entry)
        total = len(all_nodes)
        offset = p.get("offset", 0)
        limit = p.get("limit", 0)
        if offset:
            all_nodes = all_nodes[offset:]
        if limit:
            all_nodes = all_nodes[:limit]
        return {"nodes": all_nodes, "count": len(all_nodes), "total": total}

    def _read_selected(self, p: dict) -> dict:
        if not self.selected:
            return {"nodes": [], "count": 0}
        nodes = []
        for name in self.selected:
            if name not in self.nodes:
                continue
            data = self.nodes[name]
            entry: dict[str, Any] = {"name": name, "type": data["type"]}
            conns = self.connections.get(name, [])
            if any(conns):
                entry["inputs"] = conns
            knobs = data.get("knobs", {})
            if knobs:
                entry["knobs"] = knobs
            nodes.append(entry)
        return {"nodes": nodes, "count": len(nodes)}

    def _execute_python(self, p: dict) -> dict:
        # record the code so tests can assert on the string-injected payload
        code = p.get("code", "")
        self.executed_code.append(code)
        return {}

    def _render(self, p: dict) -> dict:
        return {"rendered": "Write1", "frames": [1001, 1100]}

    def _render_async(self, p: dict) -> dict:
        # Mock for B2 async render. Records the call so tests can
        # assert the wire payload and returns the immediate ack
        # without actually spawning a worker -- tests inject
        # task_progress notifications via the notification queue.
        self.async_renders.append(p)
        return {"task_id": p.get("task_id"), "started": True}

    def _cancel_render(self, p: dict) -> dict:
        self.cancelled_renders.append(p.get("task_id"))
        return {"cancelled": True, "task_id": p.get("task_id")}

    def _save_script(self, p: dict) -> dict:
        return {"saved": self.script_info["script"]}

    def _load_script(self, p: dict) -> dict:
        return {"loaded": p["path"]}

    def _set_frame_range(self, p: dict) -> dict:
        if "first" in p:
            self.script_info["first_frame"] = p["first"]
        if "last" in p:
            self.script_info["last_frame"] = p["last"]
        return {
            "first": self.script_info["first_frame"],
            "last": self.script_info["last_frame"],
        }

    def _view_node(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        return {"viewing": name}

    def _set_expression(self, p: dict) -> dict:
        node, knob, expr = p["node"], p["knob"], p["expression"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        self.expressions.setdefault(node, {})[knob] = expr
        return {"node": node, "knob": knob, "expression": expr}

    def _clear_expression(self, p: dict) -> dict:
        node, knob = p["node"], p["knob"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        self.expressions.get(node, {}).pop(knob, None)
        return {"node": node, "cleared": knob}

    def _set_keyframe(self, p: dict) -> dict:
        node, knob = p["node"], p["knob"]
        frame, value = p["frame"], p["value"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        kfs = self.keyframes.setdefault(node, {}).setdefault(knob, [])
        kfs = [k for k in kfs if k["frame"] != frame]
        kfs.append({"frame": frame, "value": value})
        kfs.sort(key=lambda k: k["frame"])
        self.keyframes[node][knob] = kfs
        return {"node": node, "knob": knob, "frame": frame, "value": value}

    def _list_keyframes(self, p: dict) -> dict:
        node, knob = p["node"], p["knob"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        kfs = self.keyframes.get(node, {}).get(knob, [])
        return {"node": node, "knob": knob, "keyframes": kfs}

    def _snapshot_comp(self, p: dict) -> dict:
        self._snap_counter += 1
        snap_id = str(self._snap_counter)
        self._snapshots[snap_id] = self._read_comp({})
        if len(self._snapshots) > 5:
            oldest = min(self._snapshots.keys(), key=int)
            del self._snapshots[oldest]
        return {"snapshot_id": snap_id, "node_count": self._snapshots[snap_id]["count"]}

    def _diff_comp(self, p: dict) -> dict:
        snap_id = p.get("snapshot_id")
        if not snap_id or snap_id not in self._snapshots:
            raise ValueError(f"snapshot not found: {snap_id}")
        before = self._snapshots[snap_id]
        current = self._read_comp({})
        before_nodes = {n["name"]: n for n in before.get("nodes", [])}
        current_nodes = {n["name"]: n for n in current.get("nodes", [])}
        added = [
            {"name": n["name"], "type": n["type"]}
            for name, n in current_nodes.items()
            if name not in before_nodes
        ]
        removed = [
            {"name": n["name"], "type": n["type"]}
            for name, n in before_nodes.items()
            if name not in current_nodes
        ]
        return {"added": added, "removed": removed, "changed": []}

    def _list_channels(self, p: dict) -> dict:
        name = p["node"]
        if name not in self.nodes:
            raise ValueError(f"node not found: {name}")
        return {"layers": {"rgba": ["red", "green", "blue", "alpha"]}}

    def _create_nodes(self, p: dict) -> dict:
        results = []
        for spec in p["nodes"]:
            results.append(self._create_node(spec))
        return {"nodes": results, "count": len(results)}

    def _set_knobs(self, p: dict) -> dict:
        results = []
        for op in p["operations"]:
            results.append(self._set_knob(op))
        return {"results": results, "count": len(results)}

    def _disconnect_input(self, p: dict) -> dict:
        node = p["node"]
        idx = p["input"]
        if node not in self.nodes:
            raise ValueError(f"node not found: {node}")
        conns = self.connections.get(node, [])
        if idx < len(conns):
            conns[idx] = None
        return {"node": node, "input": idx, "disconnected": True}

    def _set_node_position(self, p: dict) -> dict:
        positions = p.get("positions", [])
        results = []
        for pos in positions:
            name = pos["node"]
            if name not in self.nodes:
                results.append({"node": name, "error": "not found"})
                continue
            x, y = int(pos["x"]), int(pos["y"])
            self.nodes[name]["x"] = x
            self.nodes[name]["y"] = y
            results.append({"node": name, "x": x, "y": y})
        return {"results": results, "count": len(results)}

    # ------------------------------------------------------------------
    # A3 typed handlers
    #
    # Each handler:
    #   * appends ``(cmd, params)`` to ``self.typed_calls`` so tests can
    #     assert the wire shape without inspecting f-string blobs.
    #   * validates the same allowlists / inputs the real addon does so
    #     the mock's error envelope matches production.
    #   * mutates ``self.nodes`` to simulate node creation, mirroring the
    #     behaviour of ``_create_node`` so downstream tools see the new
    #     nodes.
    # ------------------------------------------------------------------

    _COLOR_OPS = frozenset({"Grade", "ColorCorrect", "HueCorrect", "OCIOColorSpace"})
    _MERGE_OPS = frozenset(
        {
            "over",
            "plus",
            "multiply",
            "screen",
            "stencil",
            "mask",
            "minus",
            "difference",
            "divide",
            "from",
            "copy",
        }
    )
    _TRANSFORM_OPS = frozenset({"Transform", "CornerPin2D", "Reformat", "Tracker4"})
    _KEYER_TYPES = frozenset({"Keylight", "Primatte", "IBKGizmo", "Cryptomatte"})
    _WRITE_TYPES = frozenset({"exr", "tiff", "tif", "png", "jpeg", "jpg", "mov", "dpx"})

    def _next_unique_name(self, base: str) -> str:
        name = base
        i = 1
        while name in self.nodes:
            i += 1
            name = f"{base}{i}"
        return name

    def _setup_keying(self, p: dict) -> dict:
        self.typed_calls.append(("setup_keying", dict(p)))
        input_node = p["input_node"]
        keyer_type = p.get("keyer_type", "Keylight")
        if keyer_type not in self._KEYER_TYPES:
            raise ValueError(f"invalid keyer_type: {keyer_type}")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")

        keyer = self._next_unique_name(keyer_type)
        self.nodes[keyer] = {"type": keyer_type, "knobs": {}, "x": 0, "y": 0}
        self.connections[keyer] = [input_node]

        erode = self._next_unique_name("FilterErode1")
        self.nodes[erode] = {
            "type": "FilterErode",
            "knobs": {"channels": "alpha", "size": -0.5},
            "x": 0,
            "y": 0,
        }
        self.connections[erode] = [keyer]

        edge = self._next_unique_name("EdgeBlur1")
        self.nodes[edge] = {"type": "EdgeBlur", "knobs": {"size": 3}, "x": 0, "y": 0}
        self.connections[edge] = [erode]

        premult = self._next_unique_name("Premult1")
        self.nodes[premult] = {"type": "Premult", "knobs": {}, "x": 0, "y": 0}
        self.connections[premult] = [edge]

        return {
            "keyer": keyer,
            "erode": erode,
            "edge_blur": edge,
            "premult": premult,
            "tip": "adjust the keyer node settings and erode size to refine the matte",
        }

    def _setup_color_correction(self, p: dict) -> dict:
        self.typed_calls.append(("setup_color_correction", dict(p)))
        input_node = p["input_node"]
        operation = p.get("operation", "Grade")
        if operation not in self._COLOR_OPS:
            raise ValueError(f"invalid operation: {operation}")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        name = self._next_unique_name(f"{operation}1")
        self.nodes[name] = {"type": operation, "knobs": {}, "x": 0, "y": 0}
        self.connections[name] = [input_node]
        return {"name": name, "type": operation}

    def _setup_merge(self, p: dict) -> dict:
        self.typed_calls.append(("setup_merge", dict(p)))
        fg = p["fg"]
        bg = p["bg"]
        operation = p.get("operation", "over")
        if operation not in self._MERGE_OPS:
            raise ValueError(f"invalid operation: {operation}")
        if fg not in self.nodes:
            raise ValueError(f"fg node not found: {fg}")
        if bg not in self.nodes:
            raise ValueError(f"bg node not found: {bg}")
        name = self._next_unique_name("Merge1")
        self.nodes[name] = {
            "type": "Merge2",
            "knobs": {"operation": operation},
            "x": 0,
            "y": 0,
        }
        # B pipe = fg (input 1), A pipe = bg (input 0)
        self.connections[name] = [bg, fg]
        return {"name": name, "operation": operation}

    def _setup_transform(self, p: dict) -> dict:
        self.typed_calls.append(("setup_transform", dict(p)))
        input_node = p["input_node"]
        operation = p.get("operation", "Transform")
        if operation not in self._TRANSFORM_OPS:
            raise ValueError(f"invalid operation: {operation}")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        name = self._next_unique_name(f"{operation}1")
        self.nodes[name] = {"type": operation, "knobs": {}, "x": 0, "y": 0}
        self.connections[name] = [input_node]
        return {"name": name, "type": operation}

    def _setup_denoise(self, p: dict) -> dict:
        self.typed_calls.append(("setup_denoise", dict(p)))
        input_node = p["input_node"]
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        name = self._next_unique_name("Denoise1")
        self.nodes[name] = {"type": "Denoise2", "knobs": {}, "x": 0, "y": 0}
        self.connections[name] = [input_node]
        return {"name": name, "type": "Denoise2"}

    def _setup_write(self, p: dict) -> dict:
        self.typed_calls.append(("setup_write", dict(p)))
        input_node = p["input_node"]
        # Mirror the addon-side path policy via shared validator.
        path = _validate_write_path(p["path"])
        file_type = p.get("file_type", "exr")
        colorspace = p.get("colorspace", "scene_linear")
        if file_type not in self._WRITE_TYPES:
            raise ValueError(f"invalid file_type: {file_type}")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        name = self._next_unique_name("Write1")
        self.nodes[name] = {
            "type": "Write",
            "knobs": {"file": path, "file_type": file_type, "colorspace": colorspace},
            "x": 0,
            "y": 0,
        }
        self.connections[name] = [input_node]
        return {"name": name, "path": path, "file_type": file_type}

    # ------------------------------------------------------------------
    # B7 scene digest / delta
    # ------------------------------------------------------------------

    def _build_digest_body(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        errors: list[str] = []
        warnings: list[str] = []
        for name, data in self.nodes.items():
            cls = data["type"]
            counts[cls] = counts.get(cls, 0) + 1
            if data.get("error"):
                errors.append(name)
            if data.get("warning"):
                warnings.append(name)
        return {
            "counts": counts,
            "total": len(self.nodes),
            "errors": errors,
            "warnings": warnings,
            "selected": sorted(self.selected),
            "viewer_active": "",
            "display_node": "",
        }

    def _digest_hash(self, body: dict[str, Any]) -> str:
        import hashlib

        body = {k: v for k, v in body.items() if k not in ("hash", "status", "changed")}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]

    def _scene_digest(self, p: dict) -> dict:
        body = self._build_digest_body()
        body["hash"] = self._digest_hash(body)
        return body

    def _scene_delta(self, p: dict) -> dict:
        prev_hash = p.get("prev_hash") or ""
        body = self._build_digest_body()
        current_hash = self._digest_hash(body)
        if current_hash == prev_hash:
            self.scene_delta_short_circuits += 1
            return {"changed": False, "hash": current_hash}
        body["hash"] = current_hash
        body["changed"] = True
        return body

    # ------------------------------------------------------------------
    # C1 tracking + deep typed handlers
    #
    # Each mirrors the addon-side allowlist + idempotency behaviour:
    #   * appends ``(cmd, params)`` to ``self.typed_calls``
    #   * if ``name`` is supplied AND a node of matching class+inputs
    #     exists at that name, returns the existing NodeRef (idempotent).
    #   * otherwise creates a fresh node, registers it in
    #     ``self.nodes`` / ``self.connections``, and returns its NodeRef.
    # ------------------------------------------------------------------

    _CAMERA_SOLVE_METHODS = frozenset({"Match-Move", "Tripod", "Free Camera", "Planar", "Object"})
    _DEEP_MERGE_OPS = frozenset({"over", "holdout"})

    def _node_ref_from_state(self, name: str) -> dict[str, Any]:
        data = self.nodes[name]
        return {
            "name": name,
            "type": data["type"],
            "x": int(data.get("x", 0)),
            "y": int(data.get("y", 0)),
            "inputs": list(self.connections.get(name, [])),
        }

    def _try_idempotent(
        self,
        name: str | None,
        node_class: str,
        expected_inputs: list[str | None],
    ) -> dict[str, Any] | None:
        """Return existing NodeRef if name+class+inputs matches; raise on mismatch."""
        if not name or name not in self.nodes:
            return None
        existing = self.nodes[name]
        if existing["type"] != node_class:
            raise ValueError(
                f"node '{name}' exists but is class '{existing['type']}', "
                f"expected '{node_class}'"
            )
        actual_inputs = list(self.connections.get(name, []))
        leading = actual_inputs[: len(expected_inputs)]
        if leading != expected_inputs:
            raise ValueError(
                f"node '{name}' exists but has inputs {actual_inputs}, "
                f"expected {expected_inputs}"
            )
        return self._node_ref_from_state(name)

    def _register_node(
        self,
        node_class: str,
        explicit_name: str | None,
        default_base: str,
        inputs: list[str | None],
        knobs: dict[str, Any] | None = None,
    ) -> str:
        """Insert a freshly-built node into ``self.nodes``. Returns its name."""
        name = explicit_name or self._next_unique_name(default_base)
        # If an explicit name collides with an unrelated node, the caller
        # would have hit the idempotent path; this fallback uniquifies.
        if name in self.nodes:
            name = self._next_unique_name(default_base)
        self.nodes[name] = {
            "type": node_class,
            "knobs": knobs or {},
            "x": 0,
            "y": 0,
        }
        self.connections[name] = list(inputs)
        return name

    def _setup_camera_tracker(self, p: dict) -> dict:
        self.typed_calls.append(("setup_camera_tracker", dict(p)))
        input_node = p["input_node"]
        features = int(p.get("features", 300))
        solve_method = p.get("solve_method", "Match-Move")
        mask = p.get("mask")
        name = p.get("name")
        if solve_method not in self._CAMERA_SOLVE_METHODS:
            raise ValueError(f"invalid solve_method: {solve_method}")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        if mask is not None and mask not in self.nodes:
            raise ValueError(f"mask node not found: {mask}")
        expected_inputs: list[str | None] = [input_node]
        if mask is not None:
            expected_inputs.append(mask)
        cached = self._try_idempotent(name, "CameraTracker", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "CameraTracker",
            name,
            "CameraTracker1",
            expected_inputs,
            knobs={
                "numberFeatures": features,
                "solveMethod": solve_method,
            },
        )
        return self._node_ref_from_state(new_name)

    def _setup_planar_tracker(self, p: dict) -> dict:
        self.typed_calls.append(("setup_planar_tracker", dict(p)))
        input_node = p["input_node"]
        plane_roto = p["plane_roto"]
        ref_frame = int(p.get("ref_frame", 1))
        name = p.get("name")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        if plane_roto not in self.nodes:
            raise ValueError(f"plane_roto node not found: {plane_roto}")
        plane_type = self.nodes[plane_roto]["type"]
        if plane_type not in ("Roto", "RotoPaint"):
            raise ValueError(
                f"plane_roto '{plane_roto}' is class '{plane_type}', expected Roto or RotoPaint"
            )
        expected_inputs = [input_node, plane_roto]
        cached = self._try_idempotent(name, "PlanarTracker", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "PlanarTracker",
            name,
            "PlanarTracker1",
            expected_inputs,
            knobs={"referenceFrame": ref_frame},
        )
        return self._node_ref_from_state(new_name)

    def _setup_tracker4(self, p: dict) -> dict:
        self.typed_calls.append(("setup_tracker4", dict(p)))
        input_node = p["input_node"]
        num_tracks = int(p.get("num_tracks", 4))
        name = p.get("name")
        if num_tracks < 1:
            raise ValueError(f"num_tracks must be >= 1, got {num_tracks}")
        if input_node not in self.nodes:
            raise ValueError(f"node not found: {input_node}")
        expected_inputs = [input_node]
        cached = self._try_idempotent(name, "Tracker4", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "Tracker4",
            name,
            "Tracker1",
            expected_inputs,
            knobs={"num_tracks": num_tracks},
        )
        return self._node_ref_from_state(new_name)

    def _bake_tracker_to_corner_pin(self, p: dict) -> dict:
        self.typed_calls.append(("bake_tracker_to_corner_pin", dict(p)))
        tracker_node = p["tracker_node"]
        ref_frame = int(p.get("ref_frame", 1))
        name = p.get("name")
        if tracker_node not in self.nodes:
            raise ValueError(f"tracker node not found: {tracker_node}")
        tracker_type = self.nodes[tracker_node]["type"]
        if tracker_type not in ("Tracker4", "PlanarTracker", "PlanarTracker"):
            raise ValueError(
                f"tracker_node '{tracker_node}' is class '{tracker_type}', "
                "expected Tracker4 or PlanarTracker"
            )
        expected_inputs = [tracker_node]
        cached = self._try_idempotent(name, "CornerPin2D", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "CornerPin2D",
            name,
            "CornerPin1",
            expected_inputs,
            knobs={"reference_frame": ref_frame},
        )
        return self._node_ref_from_state(new_name)

    def _solve_3d_camera(self, p: dict) -> dict:
        self.typed_calls.append(("solve_3d_camera", dict(p)))
        tracker_node = p["camera_tracker_node"]
        name = p.get("name")
        if tracker_node not in self.nodes:
            raise ValueError(f"camera_tracker_node not found: {tracker_node}")
        existing = self.nodes[tracker_node]
        if existing["type"] != "CameraTracker":
            raise ValueError(
                f"node '{tracker_node}' is class '{existing['type']}', " f"expected CameraTracker"
            )
        # Idempotent: solving doesn't create a new node, it just marks
        # the existing one solved. Optionally rename to ``name``.
        existing.setdefault("knobs", {})["solved"] = True
        if name and name != tracker_node:
            self.nodes[name] = self.nodes.pop(tracker_node)
            self.connections[name] = self.connections.pop(tracker_node, [])
            tracker_node = name
        return self._node_ref_from_state(tracker_node)

    def _bake_camera_to_card(self, p: dict) -> dict:
        self.typed_calls.append(("bake_camera_to_card", dict(p)))
        camera_node = p["camera_node"]
        frame = int(p.get("frame", 1))
        name = p.get("name")
        if camera_node not in self.nodes:
            raise ValueError(f"camera_node not found: {camera_node}")
        expected_inputs = [camera_node]
        cached = self._try_idempotent(name, "Card3D", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "Card3D",
            name,
            "Card1",
            expected_inputs,
            knobs={"frame": frame},
        )
        return self._node_ref_from_state(new_name)

    # ------------------------------------------------------------------
    # C5 tracking workflow macros (setup_spaceship_track_patch)
    #
    # Mirrors the addon-side handler: validates surface_type, derives a
    # shot tag from $SS_SHOT or the script_info path, composes the C1
    # typed sub-handlers so ``typed_calls`` records the underlying
    # primitives the macro invokes, and registers the Group + member
    # sub-nodes in ``self.nodes``.
    # ------------------------------------------------------------------

    _SURFACE_TYPES = frozenset({"planar", "3d"})

    def _derive_shot_tag(self) -> str:
        raw = os.environ.get("SS_SHOT") or ""
        if not raw:
            script_path = self.script_info.get("script") or ""
            if script_path:
                raw = pathlib.Path(script_path).stem
        if not raw:
            raw = "unsaved"
        cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in raw)
        return cleaned or "unsaved"

    def _setup_spaceship_track_patch(self, p: dict) -> dict:
        self.typed_calls.append(("setup_spaceship_track_patch", dict(p)))
        plate_name = p["plate"]
        ref_frame = int(p["ref_frame"])
        surface_type = p.get("surface_type", "planar")
        patch_source = p.get("patch_source")
        explicit_name = p.get("name")

        if surface_type not in self._SURFACE_TYPES:
            raise ValueError(f"invalid surface_type: {surface_type!r} (expected 'planar' or '3d')")
        if plate_name not in self.nodes:
            raise ValueError(f"plate node not found: {plate_name}")
        if patch_source is not None and patch_source not in self.nodes:
            raise ValueError(f"patch_source node not found: {patch_source}")

        group_name = explicit_name or f"SpaceshipPatch_{self._derive_shot_tag()}"

        existing = self.nodes.get(group_name)
        if existing is not None:
            if existing["type"] != "Group":
                raise ValueError(
                    f"node '{group_name}' exists but is class '{existing['type']}', "
                    "expected 'Group'"
                )
            return self._node_ref_from_state(group_name)

        members: list[str] = []

        # Group input plate handle (mirrors the addon-side ``Input`` node
        # inside the Group context).
        plate_in = self._register_node("Input", None, f"{group_name}_plateIn", [])
        members.append(plate_in)

        if surface_type == "planar":
            roto_plane = self._register_node(
                "Roto", f"{group_name}_plane", f"{group_name}_plane", [plate_in]
            )
            members.append(roto_plane)
            planar_ref = self._setup_planar_tracker(
                {
                    "input_node": plate_in,
                    "plane_roto": roto_plane,
                    "ref_frame": ref_frame,
                    "name": f"{group_name}_planar",
                }
            )
            members.append(planar_ref["name"])
            forward_pin = self._bake_tracker_to_corner_pin(
                {
                    "tracker_node": planar_ref["name"],
                    "ref_frame": ref_frame,
                    "name": f"{group_name}_pinFwd",
                }
            )
            members.append(forward_pin["name"])
            if patch_source is not None:
                patch_root_name = patch_source
            else:
                patch_root_name = self._register_node(
                    "RotoPaint",
                    f"{group_name}_paint",
                    f"{group_name}_paint",
                    [forward_pin["name"]],
                )
            members.append(patch_root_name)
            restore_pin = self._register_node(
                "CornerPin2D",
                f"{group_name}_pinRestore",
                f"{group_name}_pinRestore",
                [patch_root_name],
                knobs={"reference_frame": ref_frame, "invert": True},
            )
            members.append(restore_pin)
            output_input = restore_pin
        else:
            camtrack_ref = self._setup_camera_tracker(
                {
                    "input_node": plate_in,
                    "name": f"{group_name}_camTrack",
                }
            )
            members.append(camtrack_ref["name"])
            solved_ref = self._solve_3d_camera(
                {
                    "camera_tracker_node": camtrack_ref["name"],
                    "name": f"{group_name}_camTrack",
                }
            )
            members.append(solved_ref["name"])
            card_ref = self._bake_camera_to_card(
                {
                    "camera_node": solved_ref["name"],
                    "frame": ref_frame,
                    "name": f"{group_name}_card",
                }
            )
            members.append(card_ref["name"])
            if patch_source is not None:
                patch_root_name = patch_source
            else:
                patch_root_name = self._register_node(
                    "RotoPaint",
                    f"{group_name}_paint",
                    f"{group_name}_paint",
                    [plate_in],
                )
            members.append(patch_root_name)
            project_name = self._register_node(
                "Project3D",
                f"{group_name}_project3D",
                f"{group_name}_project3D",
                [patch_root_name, solved_ref["name"]],
            )
            members.append(project_name)
            scanline_name = self._register_node(
                "ScanlineRender",
                f"{group_name}_scanline",
                f"{group_name}_scanline",
                [None, card_ref["name"], solved_ref["name"]],
            )
            members.append(scanline_name)
            merge_name = self._register_node(
                "Merge2",
                f"{group_name}_merge",
                f"{group_name}_merge",
                [plate_in, scanline_name],
                knobs={"operation": "over"},
            )
            members.append(merge_name)
            output_input = merge_name

        # Group output node closing the inner graph.
        group_output = self._register_node("Output", None, f"{group_name}_out", [output_input])
        members.append(group_output)

        # The Group itself takes the plate as its single external input.
        self.nodes[group_name] = {
            "type": "Group",
            "knobs": {"surface_type": surface_type},
            "x": 0,
            "y": 0,
        }
        self.connections[group_name] = [plate_name]

        ref = self._node_ref_from_state(group_name)
        ref["surface_type"] = surface_type
        ref["members"] = members
        return ref

    def _create_deep_recolor(self, p: dict) -> dict:
        self.typed_calls.append(("create_deep_recolor", dict(p)))
        deep_node = p["deep_node"]
        color_node = p["color_node"]
        target_input_alpha = bool(p.get("target_input_alpha", True))
        name = p.get("name")
        if deep_node not in self.nodes:
            raise ValueError(f"deep node not found: {deep_node}")
        if color_node not in self.nodes:
            raise ValueError(f"color node not found: {color_node}")
        expected_inputs = [deep_node, color_node]
        cached = self._try_idempotent(name, "DeepRecolor", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "DeepRecolor",
            name,
            "DeepRecolor1",
            expected_inputs,
            knobs={"target_input_alpha": target_input_alpha},
        )
        return self._node_ref_from_state(new_name)

    def _create_deep_merge(self, p: dict) -> dict:
        self.typed_calls.append(("create_deep_merge", dict(p)))
        a_node = p["a_node"]
        b_node = p["b_node"]
        op = p.get("op", "over")
        name = p.get("name")
        if op not in self._DEEP_MERGE_OPS:
            raise ValueError(f"invalid op: {op}")
        if a_node not in self.nodes:
            raise ValueError(f"a_node not found: {a_node}")
        if b_node not in self.nodes:
            raise ValueError(f"b_node not found: {b_node}")
        expected_inputs = [a_node, b_node]
        cached = self._try_idempotent(name, "DeepMerge", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "DeepMerge",
            name,
            "DeepMerge1",
            expected_inputs,
            knobs={"operation": op},
        )
        return self._node_ref_from_state(new_name)

    def _create_deep_holdout(self, p: dict) -> dict:
        self.typed_calls.append(("create_deep_holdout", dict(p)))
        subject_node = p["subject_node"]
        holdout_node = p["holdout_node"]
        name = p.get("name")
        if subject_node not in self.nodes:
            raise ValueError(f"subject_node not found: {subject_node}")
        if holdout_node not in self.nodes:
            raise ValueError(f"holdout_node not found: {holdout_node}")
        expected_inputs = [subject_node, holdout_node]
        cached = self._try_idempotent(name, "DeepHoldout2", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "DeepHoldout2",
            name,
            "DeepHoldout1",
            expected_inputs,
        )
        return self._node_ref_from_state(new_name)

    def _create_deep_transform(self, p: dict) -> dict:
        self.typed_calls.append(("create_deep_transform", dict(p)))
        input_node = p["input_node"]
        translate = p.get("translate", (0.0, 0.0, 0.0))
        name = p.get("name")
        if not isinstance(translate, list | tuple) or len(translate) != 3:
            raise ValueError(f"translate must be a 3-tuple, got {translate!r}")
        if input_node not in self.nodes:
            raise ValueError(f"input_node not found: {input_node}")
        expected_inputs = [input_node]
        cached = self._try_idempotent(name, "DeepTransform", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "DeepTransform",
            name,
            "DeepTransform1",
            expected_inputs,
            knobs={"translate": list(translate)},
        )
        return self._node_ref_from_state(new_name)

    def _deep_to_image(self, p: dict) -> dict:
        self.typed_calls.append(("deep_to_image", dict(p)))
        input_node = p["input_node"]
        name = p.get("name")
        if input_node not in self.nodes:
            raise ValueError(f"input_node not found: {input_node}")
        expected_inputs = [input_node]
        cached = self._try_idempotent(name, "DeepToImage", expected_inputs)
        if cached is not None:
            return cached
        new_name = self._register_node(
            "DeepToImage",
            name,
            "DeepToImage1",
            expected_inputs,
        )
        return self._node_ref_from_state(new_name)


@pytest.fixture(autouse=True)
def _disable_heartbeat(monkeypatch):
    """Heartbeat thread off by default in tests.

    Most tests don't exercise the heartbeat and a 5s interval thread
    holding the I/O lock makes them flaky. Tests that need heartbeat
    behavior re-enable it explicitly.
    """
    monkeypatch.setenv("NUKE_MCP_HEARTBEAT", "0")


@pytest.fixture
def mock_server():
    server = MockNukeServer()
    port = server.start()
    time.sleep(0.1)  # let server bind
    yield server, port
    server.stop()


@pytest.fixture
def connected(mock_server):
    """Connect to mock server, disconnect after test."""
    from nuke_mcp import connection

    server, port = mock_server
    connection.connect("localhost", port)
    yield server
    connection.disconnect()


@pytest.fixture
def mock_script(mock_server):
    """Connected mock-server with a MockNukeScript registry attached.

    Tests can pre-populate via ``mock_script.add(MockNukeNode.read("Plate"))``
    and then drive tools that ship execute_python payloads. The recorded
    payloads land in ``server.executed_code`` for assertion.

    Yields a tuple ``(server, script)`` so tests get both halves.
    """
    from nuke_mcp import connection

    server, port = mock_server
    server.script = MockNukeScript()
    connection.connect("localhost", port)
    yield server, server.script
    connection.disconnect()
