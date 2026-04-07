# nuke-mcp

MCP server for Foundry Nuke. Lets AI assistants (Claude, Cursor, etc.) read, understand, and manipulate Nuke compositing scripts.

## What it does

- **Reads your comp natively** -- Claude can understand your node graph without screenshots. It sees every node, connection, and non-default knob value.
- **Creates and wires nodes** -- build comps through conversation.
- **Sets up common workflows** -- keying pipelines, colour correction, merges, precomps.
- **Precomp automation** -- one command to set up Write + Read + rewire downstream nodes.

## Requirements

- Python 3.10+
- Foundry Nuke (tested on 15.x, 16.x)
- An MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.)

## Install

```bash
pip install -e .
```

Copy `nuke_plugin/addon.py` to your `~/.nuke/` directory (rename to `nuke_mcp_addon.py`), then add to your `~/.nuke/init.py`:

```python
import nuke_mcp_addon
```

And to your `~/.nuke/menu.py`:

```python
import nuke_mcp_addon
toolbar = nuke.menu("Nodes")
mcp_menu = toolbar.addMenu("MCP")
mcp_menu.addCommand("Start Server", nuke_mcp_addon.start)
mcp_menu.addCommand("Stop Server", nuke_mcp_addon.stop)
```

## Client config

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "nuke": {
      "command": "nuke-mcp",
      "transport": "stdio"
    }
  }
}
```

Or for Claude Code (`~/.claude/mcp.json`):

```json
{
  "nuke": {
    "command": "nuke-mcp"
  }
}
```

## Usage

1. Open Nuke
2. Start the MCP server (Nodes > MCP > Start Server)
3. Ask Claude to read your comp, create nodes, set up workflows, etc.

## Tools

### Reading
- `read_comp` -- serialize the full node graph
- `read_node_detail` -- inspect a single node in depth
- `read_selected` -- inspect selected nodes only

### Graph
- `create_node`, `delete_node`, `find_nodes`, `list_nodes`
- `connect_nodes`, `auto_layout`, `modify_node`

### Knobs
- `get_knob`, `set_knob`

### Compositing
- `setup_keying`, `setup_color_correction`, `setup_merge`
- `setup_transform`, `setup_denoise`

### Render
- `setup_write`, `render_frames`, `setup_precomp`, `list_precomps`

### Script
- `get_script_info`, `save_script`, `load_script`, `set_frame_range`

### Other
- `list_channels`, `shuffle_channels`, `setup_aov_merge`
- `view_node`, `set_viewer_lut`
- `execute_python`

## License

MIT
