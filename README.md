# nuke-mcp

MCP server for Foundry Nuke. Lets AI assistants (Claude, Cursor, etc.) read, understand, and manipulate Nuke compositing scripts.

## What it does

- **Reads your comp natively** -- Claude can understand your node graph without screenshots. It sees every node, connection, and non-default knob value.
- **Creates and wires nodes** -- build comps through conversation.
- **Sets up common workflows** -- keying pipelines, colour correction, merges, precomps.
- **Precomp automation** -- one command to set up Write + Read + rewire downstream nodes.
- **Expressions and keyframes** -- set TCL expressions, animate knobs, inspect animation curves.
- **Diff tracking** -- snapshot your comp, make changes, diff to see what changed.
- **Auto-reconnect** -- if Nuke restarts or the addon toggles, the server reconnects transparently.

## Requirements

- Python 3.10+
- Foundry Nuke 15.x or 16.x
- An MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.)

## Install

```bash
git clone https://github.com/cian-bit/nuke-mcp.git
cd nuke-mcp
python -m venv .venv
.venv/Scripts/pip install -e .       # Windows
# .venv/bin/pip install -e .         # macOS/Linux
```

### Nuke addon setup

Copy the `nuke_plugin/` contents to `~/.nuke/nuke_mcp_addon/`:

```
~/.nuke/
  nuke_mcp_addon/
    __init__.py
    addon.py
    menu.py
```

Add to `~/.nuke/init.py`:

```python
nuke.pluginAddPath('./nuke_mcp_addon')
```

Add to `~/.nuke/menu.py`:

```python
import nuke_mcp_addon
toolbar = nuke.menu("Nodes")
mcp_menu = toolbar.addMenu("MCP")
mcp_menu.addCommand("Start Server", nuke_mcp_addon.start)
mcp_menu.addCommand("Stop Server", nuke_mcp_addon.stop)
```

## Client config

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nuke": {
      "command": "C:\\path\\to\\nuke-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "nuke_mcp"],
      "env": {
        "NUKE_HOST": "localhost",
        "NUKE_PORT": "9876"
      }
    }
  }
}
```

### Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "nuke": {
    "command": "nuke-mcp"
  }
}
```

## Usage

1. Open Nuke
2. Start the MCP addon: Nodes > MCP > Start Server
3. Open Claude and start talking to your comp

## Tools (38)

### Reading (5)
- `read_comp` -- serialize the full node graph
- `read_node_detail` -- inspect a single node
- `read_selected` -- inspect selected nodes only
- `snapshot_comp` -- take a comp snapshot for diffing
- `diff_comp` -- compare current comp to a snapshot

### Graph (7)
- `create_node`, `delete_node`, `find_nodes`, `list_nodes`
- `connect_nodes`, `auto_layout`, `modify_node`

### Knobs (2)
- `get_knob`, `set_knob`

### Expressions (4)
- `set_expression`, `clear_expression`
- `set_keyframe`, `list_keyframes`

### Compositing (5)
- `setup_keying`, `setup_color_correction`, `setup_merge`
- `setup_transform`, `setup_denoise`

### Render (4)
- `setup_write`, `render_frames`, `setup_precomp`, `list_precomps`

### Script (4)
- `get_script_info`, `save_script`, `load_script`, `set_frame_range`

### Roto (2)
- `create_roto`, `list_roto_shapes`

### Other (5)
- `list_channels`, `shuffle_channels`, `setup_aov_merge`
- `view_node`, `set_viewer_lut`
- `execute_python`

## Architecture

```
Claude/Cursor/etc
    | stdio (MCP protocol)
    v
nuke-mcp server (Python subprocess)
    | TCP socket (JSON, port 9876)
    v
Nuke addon (threaded server inside Nuke)
    | nuke.executeInMainThreadWithResult()
    v
Nuke Python API
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
pre-commit run --all-files
```

## License

MIT
