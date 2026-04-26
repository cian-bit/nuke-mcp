"""Mock Nuke server + node graph for testing without a running Nuke instance.

The ``MockNukeServer`` mocks the addon wire protocol; ``MockNukeNode`` and
``MockNukeScript`` mock the in-process ``nuke`` Python API so tools that ship
``execute_python`` payloads can be exercised against an in-memory graph.

A4 brought ``MockNukeNode`` (ported from MockHouNode in
``houdini-mcp-beta/tests/conftest.py``) plus 30+ first-class node-type
factories. Tests opt into the registry via the ``mock_script`` fixture.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest

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
        return cls(name=name, node_class="PlanarTrackerNode", knobs=knobs, xpos=xpos, ypos=ypos)

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
        node = cls(name=name, node_class="DeepHoldout", knobs=knobs, xpos=xpos, ypos=ypos)
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
        # Recorded execute_python payloads. Tests that ship f-string Python
        # blobs (comp/render/channels/roto/viewer) can assert on
        # ``mock_server.executed_code`` to lock in the current behaviour
        # before A3 migrates to typed handlers.
        self.executed_code: list[str] = []
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
