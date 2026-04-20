# DBC Viewer

A set of tools for viewing and browsing CAN database (.dbc) files on macOS. No external dependencies required - just Python 3 and optionally tmux.

Built as a macOS-friendly alternative to Vector CANdb++.

---

## Tools

### `dbc_viewer.py` - Web Dashboard

Interactive web-based DBC viewer that runs in your browser.

```bash
python3 dbc_viewer.py                       # auto-loads all .dbc files in current dir
python3 dbc_viewer.py file1.dbc file2.dbc   # load specific files
python3 dbc_viewer.py --port 9000           # custom port (default: 8087)
```

**Features:**
- File tabs to switch between multiple DBC files
- Message browser with search/filter by name, CAN ID, sender, or signal name
- Signal detail view with bit position, factor/offset, range, unit, value descriptions
- Bit layout visualization showing signal placement in the CAN frame
- Node/ECU view with TX/RX message counts
- Value table browser
- Comments and attributes display (cycle time, send type, SignalLongName, etc.)
- Auto-finds a free port if the default is in use

### `dbc_tui.py` - Terminal UI

Curses-based terminal viewer with the same functionality.

```bash
python3 dbc_tui.py                       # auto-loads all .dbc files in current dir
python3 dbc_tui.py file1.dbc file2.dbc   # load specific files
```

**Controls:**

| Key | Action |
|-----|--------|
| `j` / `k` or Up / Down | Navigate lists |
| `Tab` | Switch focus between sidebar and detail panel |
| `Enter` | Expand/collapse signal details |
| `1` - `5` | Switch sidebar tab (Messages, Nodes, ValTables, Search, Info) |
| `/` | Search |
| `f` / `F` | Next / previous DBC file |
| `PgUp` / `PgDn` | Scroll fast |
| `Esc` / `q` | Back / exit search / quit |

### `dbc_launch.sh` - tmux Launcher

Opens the TUI in a split pane next to your terminal using tmux.

```bash
./dbc_launch.sh                       # auto-loads all .dbc files
./dbc_launch.sh file1.dbc file2.dbc   # load specific files
```

If you're already inside tmux, it splits your current pane. If not, it starts a new tmux session.

**Requires:** tmux (`brew install tmux`)

---

## Supported DBC Sections

| Section | Description |
|---------|-------------|
| `VERSION` | Database version |
| `BU_` | Nodes / ECUs |
| `BO_` / `SG_` | Messages and signals |
| `CM_` | Comments (node, message, signal) |
| `VAL_TABLE_` | Value tables |
| `VAL_` | Signal value descriptions |
| `BA_DEF_` / `BA_DEF_DEF_` | Attribute definitions and defaults |
| `BA_` | Attribute values (GenMsgCycleTime, GenMsgSendType, SignalLongName, SignalType, VFrameFormat, etc.) |
| `BO_TX_BU_` | Multiple transmitters per message |

---

## Requirements

- Python 3.6+
- macOS (or any Unix with curses support for the TUI)
- tmux (optional, for `dbc_launch.sh`)
- No pip packages needed
