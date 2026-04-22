#!/usr/bin/env python3
"""
DBC TUI Viewer - Terminal-based CAN database file viewer.
Replacement for CANdb++ with no external dependencies.

Usage:
    python3 dbc_tui.py                         # Auto-loads all .dbc files in current dir
    python3 dbc_tui.py file1.dbc file2.dbc     # Load specific files

Controls:
    Tab / Shift+Tab    Switch between panels
    Up/Down / j/k      Navigate lists
    Enter              Select / expand
    1-5                Switch sidebar tab (Messages/Nodes/ValTables/Search/Info)
    /                  Search
    f                  Switch DBC file
    q / Esc            Quit (or go back)
"""

import os
import re
import sys
import glob
import curses
import locale
import argparse
import textwrap

# Required for curses to render multi-byte UTF-8 chars (box drawing, etc.)
# with correct cell widths. Without this Python defaults to the C locale
# and consecutive ─ characters all overwrite the same cell.
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass
# Some terminals (e.g. Ghostty launched without LANG) leave the preferred
# encoding as ASCII/ANSI even after the call above. Force UTF-8 so wide
# addstr paths work.
if locale.getpreferredencoding(False).upper().replace("-", "") != "UTF8":
    for _loc in ("en_US.UTF-8", "en_US.UTF8", "C.UTF-8", "C.UTF8"):
        try:
            locale.setlocale(locale.LC_ALL, _loc)
            break
        except locale.Error:
            continue

# ─── DBC Parser (shared with dbc_viewer.py) ──────────────────────────────────

def parse_dbc(filepath):
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    db = {
        "filename": os.path.basename(filepath),
        "filepath": filepath,
        "version": "",
        "nodes": [],
        "messages": {},
        "comments": {"nodes": {}, "messages": {}, "signals": {}},
        "value_tables": {},
        "value_descriptions": {},
        "attribute_definitions": [],
        "attribute_defaults": {},
        "attributes": {"messages": {}, "signals": {}, "nodes": {}},
    }

    lines = content.replace("\r\n", "\n").split("\n")

    m = re.search(r'^VERSION\s+"(.*?)"', content, re.MULTILINE)
    if m:
        db["version"] = m.group(1)

    m = re.search(r"^BU_\s*:(.*?)$", content, re.MULTILINE)
    if m:
        db["nodes"] = m.group(1).strip().split()

    for m in re.finditer(r"^VAL_TABLE_\s+(\S+)(.*?);", content, re.MULTILINE | re.DOTALL):
        db["value_tables"][m.group(1)] = _parse_value_pairs(m.group(2))

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        bo_match = re.match(r"^BO_\s+(\d+)\s+(\S+)\s*:\s*(\d+)\s+(\S+)", line)
        if bo_match:
            msg_id = int(bo_match.group(1))
            is_extended = (msg_id & 0x80000000) != 0
            can_id = msg_id & 0x1FFFFFFF if is_extended else msg_id & 0x7FF
            msg = {
                "id": msg_id, "can_id": can_id, "hex_id": f"0x{can_id:03X}",
                "name": bo_match.group(2), "dlc": int(bo_match.group(3)),
                "sender": bo_match.group(4), "is_extended": is_extended, "signals": [],
            }
            i += 1
            while i < len(lines):
                sg_match = re.match(
                    r"^\s+SG_\s+(\S+)\s*(?:(\S+)\s+)?:\s*(\d+)\|(\d+)@([01])([+-])"
                    r"\s+\(([^,]+),([^)]+)\)\s+\[([^|]+)\|([^\]]+)\]\s+"
                    r'"([^"]*)"\s+(.*)', lines[i])
                if sg_match:
                    msg["signals"].append({
                        "name": sg_match.group(1),
                        "mux_indicator": sg_match.group(2) or "",
                        "start_bit": int(sg_match.group(3)),
                        "bit_length": int(sg_match.group(4)),
                        "byte_order": "little_endian" if sg_match.group(5) == "1" else "big_endian",
                        "is_signed": sg_match.group(6) == "-",
                        "factor": float(sg_match.group(7)),
                        "offset": float(sg_match.group(8)),
                        "minimum": float(sg_match.group(9)),
                        "maximum": float(sg_match.group(10)),
                        "unit": sg_match.group(11),
                        "receivers": [r.strip() for r in sg_match.group(12).rstrip(";").split(",") if r.strip()],
                    })
                    i += 1
                else:
                    break
            db["messages"][msg_id] = msg
            continue
        i += 1

    for m in re.finditer(r'CM_\s+BU_\s+(\S+)\s+"(.*?)"\s*;', content, re.DOTALL):
        db["comments"]["nodes"][m.group(1)] = m.group(2).replace("\n", " ").strip()
    for m in re.finditer(r'CM_\s+BO_\s+(\d+)\s+"(.*?)"\s*;', content, re.DOTALL):
        db["comments"]["messages"][int(m.group(1))] = m.group(2).replace("\n", " ").strip()
    for m in re.finditer(r'CM_\s+SG_\s+(\d+)\s+(\S+)\s+"(.*?)"\s*;', content, re.DOTALL):
        db["comments"]["signals"][f"{m.group(1)}_{m.group(2)}"] = m.group(3).replace("\n", " ").strip()

    for m in re.finditer(r"^VAL_\s+(\d+)\s+(\S+)(.*?);", content, re.MULTILINE | re.DOTALL):
        db["value_descriptions"][f"{m.group(1)}_{m.group(2)}"] = _parse_value_pairs(m.group(3))

    for m in re.finditer(r'^BA_DEF_DEF_\s+"(\S+)"\s+(.*?);', content, re.MULTILINE):
        db["attribute_defaults"][m.group(1)] = m.group(2).strip().strip('"')

    for m in re.finditer(r'^BA_\s+"(\S+)"\s+BO_\s+(\d+)\s+(.*?);', content, re.MULTILINE):
        msg_id = int(m.group(2))
        if msg_id not in db["attributes"]["messages"]:
            db["attributes"]["messages"][msg_id] = {}
        db["attributes"]["messages"][msg_id][m.group(1)] = m.group(3).strip().strip('"')

    for m in re.finditer(r'^BA_\s+"(\S+)"\s+SG_\s+(\d+)\s+(\S+)\s+(.*?);', content, re.MULTILINE):
        key = f"{m.group(2)}_{m.group(3)}"
        if key not in db["attributes"]["signals"]:
            db["attributes"]["signals"][key] = {}
        db["attributes"]["signals"][key][m.group(1)] = m.group(4).strip().strip('"')

    for m in re.finditer(r"^BO_TX_BU_\s+(\d+)\s*:\s*(.*?);", content, re.MULTILINE):
        msg_id = int(m.group(1))
        if msg_id in db["messages"]:
            db["messages"][msg_id]["transmitters"] = [t.strip() for t in m.group(2).split(",") if t.strip()]

    return db


def _parse_value_pairs(text):
    pairs = {}
    for m in re.finditer(r'(\d+)\s+"([^"]*)"', text):
        pairs[int(m.group(1))] = m.group(2)
    return pairs


# ─── TUI Application ─────────────────────────────────────────────────────────

class DBCTui:
    # Sidebar tabs
    TAB_MESSAGES = 0
    TAB_NODES = 1
    TAB_VTABLES = 2
    TAB_SEARCH = 3
    TAB_INFO = 4
    TAB_NAMES = ["Messages", "Nodes", "ValTables", "Search", "Info"]

    # Focus
    FOCUS_SIDEBAR = 0
    FOCUS_DETAIL = 1

    def __init__(self, databases):
        self.databases = databases
        self.file_names = list(databases.keys())
        self.active_file_idx = 0
        self.sidebar_tab = self.TAB_MESSAGES
        self.focus = self.FOCUS_SIDEBAR

        # Sidebar list state
        self.sidebar_cursor = 0
        self.sidebar_scroll = 0
        self.sidebar_items = []

        # Detail state
        self.detail_scroll = 0
        self.detail_lines = []

        # Search
        self.search_query = ""

        # Signal cursor and expansion for message detail view
        self.signal_cursor = 0      # which signal is highlighted
        self.expanded_signal = -1   # which signal is expanded (-1 = none)

        self._rebuild_sidebar()

    @property
    def db(self):
        if not self.file_names:
            return None
        return self.databases[self.file_names[self.active_file_idx]]

    def _get_messages(self):
        if not self.db:
            return []
        msgs = [m for m in self.db["messages"].values() if m["name"] != "VECTOR__INDEPENDENT_SIG_MSG"]
        msgs.sort(key=lambda m: m["can_id"])
        return msgs

    def _rebuild_sidebar(self):
        self.sidebar_items = []
        self.sidebar_cursor = 0
        self.sidebar_scroll = 0
        self.expanded_signal = -1
        self.signal_cursor = 0
        q = self.search_query.lower()

        if self.sidebar_tab == self.TAB_MESSAGES:
            for m in self._get_messages():
                if q and q not in m["name"].lower() and q not in m["hex_id"].lower() \
                        and q not in m["sender"].lower() \
                        and not any(q in s["name"].lower() for s in m["signals"]):
                    continue
                self.sidebar_items.append(("msg", m))

        elif self.sidebar_tab == self.TAB_NODES:
            if self.db:
                for n in self.db["nodes"]:
                    if q and q not in n.lower():
                        continue
                    self.sidebar_items.append(("node", n))

        elif self.sidebar_tab == self.TAB_VTABLES:
            if self.db:
                for name in sorted(self.db["value_tables"].keys()):
                    if q and q not in name.lower():
                        continue
                    self.sidebar_items.append(("vt", name))

        elif self.sidebar_tab == self.TAB_SEARCH:
            # Search results across all types
            if self.db and q:
                for m in self._get_messages():
                    if q in m["name"].lower() or q in m["hex_id"].lower():
                        self.sidebar_items.append(("msg", m))
                for m in self._get_messages():
                    for s in m["signals"]:
                        if q in s["name"].lower():
                            self.sidebar_items.append(("sig_ref", (m, s)))
                for n in self.db["nodes"]:
                    if q in n.lower():
                        self.sidebar_items.append(("node", n))

        elif self.sidebar_tab == self.TAB_INFO:
            self.sidebar_items.append(("info", None))

        self._build_detail()

    def _build_detail(self):
        """Build detail lines for the currently selected sidebar item."""
        self.detail_lines = []
        self.detail_scroll = 0

        if not self.sidebar_items:
            if self.sidebar_tab == self.TAB_INFO:
                self._build_info_detail()
            else:
                self.detail_lines = [("", 0)]
            return

        if self.sidebar_tab == self.TAB_INFO:
            self._build_info_detail()
            return

        kind, data = self.sidebar_items[min(self.sidebar_cursor, len(self.sidebar_items) - 1)]
        if kind == "msg":
            self._build_msg_detail(data)
        elif kind == "node":
            self._build_node_detail(data)
        elif kind == "vt":
            self._build_vt_detail(data)
        elif kind == "sig_ref":
            self._build_msg_detail(data[0])

    # ─── Box Drawing Helpers ─────────────────────────────────────────

    def _box_top(self, w, title=""):
        if title:
            title = f" {title} "
            pad = w - 2 - len(title)
            return ("  \u250c" + title + "\u2500" * max(0, pad) + "\u2510", "dim")
        return ("  \u250c" + "\u2500" * (w - 2) + "\u2510", "dim")

    def _box_mid(self, w):
        return ("  \u251c" + "\u2500" * (w - 2) + "\u2524", "dim")

    def _box_bot(self, w):
        return ("  \u2514" + "\u2500" * (w - 2) + "\u2518", "dim")

    def _box_row(self, text, w):
        text = text[:w - 4]
        line = "  \u2502 " + text + " " * max(0, w - 4 - len(text)) + " \u2502"
        return (line, [(2, 1, "dim"), (len(line) - 1, 1, "dim")])

    def _box_row_styled(self, text, w, style):
        text = text[:w - 4]
        line = "  \u2502 " + text + " " * max(0, w - 4 - len(text)) + " \u2502"
        # base style applied to whole line, then │ walls overlaid in dim
        return (line, [(0, len(line), style), (2, 1, "dim"), (len(line) - 1, 1, "dim")])

    def _section_header(self, title):
        bar = "\u2501" * 3
        return (f"  {bar} {title} {bar}", "header")

    def _blank(self):
        return ("", 0)

    def _build_info_detail(self):
        db = self.db
        if not db:
            return
        msgs = self._get_messages()
        sig_count = sum(len(m["signals"]) for m in msgs)
        L = self.detail_lines
        bw = 50

        L.append(self._blank())
        L.append(("  \u2588\u2588\u2588  DBC DATABASE OVERVIEW", "title"))
        L.append(("  " + "\u2500" * 50, "dim"))
        L.append(self._blank())

        # Stats boxes
        L.append(self._box_top(bw, "Statistics"))
        L.append(self._box_row(f"File:          {db['filename']}", bw))
        L.append(self._box_row(f"Path:          {db['filepath']}", bw))
        L.append(self._box_row(f"Version:       {db['version'] or 'N/A'}", bw))
        L.append(self._box_mid(bw))
        L.append(self._box_row(f"\u25cf Messages:      {len(msgs)}", bw))
        L.append(self._box_row(f"\u25cf Signals:       {sig_count}", bw))
        L.append(self._box_row(f"\u25cf Nodes/ECUs:    {len(db['nodes'])}", bw))
        L.append(self._box_row(f"\u25cf Value Tables:  {len(db['value_tables'])}", bw))
        L.append(self._box_bot(bw))
        L.append(self._blank())

        if db["nodes"]:
            L.append(self._section_header("NODES / ECUs"))
            L.append(self._blank())
            for n in db["nodes"]:
                comment = db["comments"]["nodes"].get(n, "")
                tx = sum(1 for m in msgs if m["sender"] == n)
                rx = sum(1 for m in msgs if any(n in s["receivers"] for s in m["signals"]))
                L.append((f"    \u25b8 {n}", "accent"))
                if comment:
                    L.append((f"      {comment}", "dim"))
                L.append((f"      TX: {tx}  \u2502  RX: {rx}", 0))
                L.append(self._blank())

    def _build_msg_detail(self, msg):
        db = self.db
        L = self.detail_lines
        attrs = db["attributes"]["messages"].get(msg["id"], {})
        comment = db["comments"]["messages"].get(msg["id"], "")
        cycle = attrs.get("GenMsgCycleTime", "")
        bw = 50

        L.append(self._blank())
        L.append((f"  \u2588\u2588\u2588  {msg['name']}", "title"))
        L.append(("  " + "\u2500" * 50, "dim"))
        L.append(self._blank())

        # Message info box
        L.append(self._box_top(bw, "Message Info"))
        L.append(self._box_row(f"CAN ID:       {msg['hex_id']}  ({msg['can_id']})", bw))
        L.append(self._box_row(f"Raw ID:       {msg['id']}", bw))
        L.append(self._box_row(f"DLC:          {msg['dlc']} bytes", bw))
        L.append(self._box_row(f"Sender:       {msg['sender']}", bw))
        frame_type = "Extended (29-bit)" if msg["is_extended"] else "Standard (11-bit)"
        L.append(self._box_row(f"Frame:        {frame_type}", bw))
        if cycle:
            L.append(self._box_row(f"Cycle Time:   {cycle} ms", bw))
        tx = msg.get("transmitters", [])
        if tx:
            L.append(self._box_row(f"Transmitters: {', '.join(tx)}", bw))
        L.append(self._box_bot(bw))

        if comment:
            L.append(self._blank())
            L.append(self._box_top(bw, "Comment"))
            for wline in textwrap.wrap(comment, bw - 4):
                L.append(self._box_row(wline, bw))
            L.append(self._box_bot(bw))
        L.append(self._blank())

        # Bit layout
        if msg["dlc"] > 0 and msg["signals"]:
            L.append(self._section_header("BIT LAYOUT"))
            L.append(self._blank())
            bit_map = [None] * (msg["dlc"] * 8)
            for idx, sig in enumerate(msg["signals"]):
                bits = self._get_signal_bits(sig, msg["dlc"])
                for b in bits:
                    if 0 <= b < len(bit_map):
                        bit_map[b] = (sig["name"], idx)

            # Header with box chars
            hdr = "           "
            for b in range(7, -1, -1):
                hdr += f"  {b}   "
            L.append((hdr, "dim"))
            L.append(("           \u250c" + ("\u2500" * 5 + "\u252c") * 7 + "\u2500" * 5 + "\u2510", "dim"))

            for byte_n in range(msg["dlc"]):
                row = f"  Byte {byte_n:2d}  \u2502"
                bar_cols = [len(row) - 1]
                for bit in range(7, -1, -1):
                    bidx = byte_n * 8 + bit
                    entry = bit_map[bidx]
                    if entry:
                        abbr = entry[0][:4]
                        row += f" {abbr:<4}\u2502"
                    else:
                        row += "  \u00b7  \u2502"
                    bar_cols.append(len(row) - 1)
                L.append((row, [(c, 1, "dim") for c in bar_cols]))
                if byte_n < msg["dlc"] - 1:
                    L.append(("           \u251c" + ("\u2500" * 5 + "\u253c") * 7 + "\u2500" * 5 + "\u2524", "dim"))

            L.append(("           \u2514" + ("\u2500" * 5 + "\u2534") * 7 + "\u2500" * 5 + "\u2518", "dim"))
            L.append(self._blank())

        # Signals
        num_sigs = len(msg["signals"])
        L.append(self._section_header(f"SIGNALS ({num_sigs})  \u2502  j/k: navigate  Enter: expand"))
        L.append(self._blank())

        # Column header
        header = f"    {'Name':<30} {'Bits':>9} {'Factor':>8} {'Offset':>8} {'Unit':>8}"
        L.append((header, "dim"))
        L.append(("    " + "\u2500" * 67, "dim"))

        # Track where signal lines start so we can auto-scroll
        self._signal_line_start = len(L)
        # Clamp signal cursor
        if num_sigs > 0:
            self.signal_cursor = max(0, min(self.signal_cursor, num_sigs - 1))

        for idx, sig in enumerate(msg["signals"]):
            sig_key = f"{msg['id']}_{sig['name']}"
            sig_attrs = db["attributes"]["signals"].get(sig_key, {})
            long_name = sig_attrs.get("SignalLongName", "")
            order_char = "I" if sig["byte_order"] == "little_endian" else "M"
            sign_char = "S" if sig["is_signed"] else "U"

            name_display = sig["name"]
            if len(name_display) > 29:
                name_display = name_display[:26] + "..."
            bits_str = f"{sig['start_bit']}|{sig['bit_length']}{order_char}{sign_char}"

            pointer = "\u25b6 " if idx == self.signal_cursor else "  "
            line = f"  {pointer}{name_display:<30} {bits_str:>9} {sig['factor']:>8g} {sig['offset']:>8g} {sig['unit'] or '-':>8}"

            is_cursor = (idx == self.signal_cursor)
            is_expanded = (idx == self.expanded_signal)

            if is_cursor:
                L.append((line, curses.A_REVERSE))
            else:
                L.append((line, 0))

            if is_expanded:
                ew = 46
                L.append(("    \u250c" + "\u2500" * (ew - 2) + "\u2510", "green"))
                if long_name:
                    L.append(self._exp_row(f"Long Name:  {long_name}", ew))
                L.append(self._exp_row(f"Byte Order: {'Little Endian (Intel)' if sig['byte_order'] == 'little_endian' else 'Big Endian (Motorola)'}", ew))
                L.append(self._exp_row(f"Signed:     {'Yes' if sig['is_signed'] else 'No'}", ew))
                L.append(self._exp_row(f"Range:      [{sig['minimum']} .. {sig['maximum']}]", ew))
                L.append(self._exp_row(f"Formula:    raw * {sig['factor']} + {sig['offset']}", ew))
                if sig["receivers"]:
                    L.append(self._exp_row(f"Receivers:  {', '.join(sig['receivers'])}", ew))

                sig_comment = db["comments"]["signals"].get(sig_key, "")
                if sig_comment:
                    L.append(("    \u251c" + "\u2500" * (ew - 2) + "\u2524", "green"))
                    for wline in textwrap.wrap(sig_comment, ew - 4):
                        L.append(self._exp_row(wline, ew))

                val_desc = db["value_descriptions"].get(sig_key, {})
                if val_desc:
                    L.append(("    \u251c" + "\u2500" * (ew - 2) + "\u2524", "green"))
                    L.append(self._exp_row("Value Descriptions:", ew))
                    for v, d in sorted(val_desc.items()):
                        L.append(self._exp_row(f"  {v:>4} \u2192 {d}", ew))

                extra = {k: v for k, v in sig_attrs.items() if k != "SignalLongName"}
                if extra:
                    L.append(("    \u251c" + "\u2500" * (ew - 2) + "\u2524", "green"))
                    for k, v in extra.items():
                        L.append(self._exp_row(f"{k}: {v}", ew))

                L.append(("    \u2514" + "\u2500" * (ew - 2) + "\u2518", "green"))
                L.append(self._blank())

    def _exp_row(self, text, w):
        text = text[:w - 4]
        line = "    \u2502 " + text + " " * max(0, w - 4 - len(text)) + " \u2502"
        return (line, [(0, len(line), "green"), (4, 1, "dim"), (len(line) - 1, 1, "dim")])

    def _build_node_detail(self, node_name):
        db = self.db
        L = self.detail_lines
        msgs = self._get_messages()
        comment = db["comments"]["nodes"].get(node_name, "")
        tx_msgs = [m for m in msgs if m["sender"] == node_name]
        rx_msgs = [m for m in msgs if any(node_name in s["receivers"] for s in m["signals"])]
        bw = 50

        L.append(self._blank())
        L.append((f"  \u2588\u2588\u2588  {node_name}", "title"))
        L.append(("  " + "\u2500" * 50, "dim"))
        L.append(self._blank())

        # Stats box
        L.append(self._box_top(bw, "Node Info"))
        L.append(self._box_row(f"\u25cf TX Messages:  {len(tx_msgs)}", bw))
        L.append(self._box_row(f"\u25cf RX Messages:  {len(rx_msgs)}", bw))
        L.append(self._box_row(f"\u25cf TX Signals:   {sum(len(m['signals']) for m in tx_msgs)}", bw))
        L.append(self._box_bot(bw))

        if comment:
            L.append(self._blank())
            L.append(self._box_top(bw, "Comment"))
            for wline in textwrap.wrap(comment, bw - 4):
                L.append(self._box_row(wline, bw))
            L.append(self._box_bot(bw))
        L.append(self._blank())

        if tx_msgs:
            L.append(self._section_header("TRANSMITTED"))
            L.append(self._blank())
            L.append(("    " + f"{'ID':>7}  {'Name':<32} {'DLC':>3}  {'Sigs':>4}", "dim"))
            L.append(("    " + "\u2500" * 52, "dim"))
            for m in tx_msgs:
                L.append((f"    {m['hex_id']:>7}  {m['name']:<32} {m['dlc']:>3}  {len(m['signals']):>4}", 0))
            L.append(self._blank())

        if rx_msgs:
            L.append(self._section_header("RECEIVED"))
            L.append(self._blank())
            L.append(("    " + f"{'ID':>7}  {'Name':<32} {'DLC':>3}  {'Sigs':>4}", "dim"))
            L.append(("    " + "\u2500" * 52, "dim"))
            for m in rx_msgs:
                L.append((f"    {m['hex_id']:>7}  {m['name']:<32} {m['dlc']:>3}  {len(m['signals']):>4}", 0))

    def _build_vt_detail(self, vt_name):
        db = self.db
        L = self.detail_lines
        pairs = db["value_tables"].get(vt_name, {})
        bw = 44

        L.append(self._blank())
        L.append((f"  \u2588\u2588\u2588  {vt_name}", "title"))
        L.append(("  " + "\u2500" * 50, "dim"))
        L.append(self._blank())

        L.append(self._box_top(bw, f"{len(pairs)} entries"))
        for v, d in sorted(pairs.items()):
            L.append(self._box_row(f"{v:>5}  \u2192  {d}", bw))
        L.append(self._box_bot(bw))

    def _get_signal_bits(self, sig, dlc):
        bits = []
        if sig["byte_order"] == "little_endian":
            for i in range(sig["bit_length"]):
                bits.append(sig["start_bit"] + i)
        else:
            bit = sig["start_bit"]
            for i in range(sig["bit_length"]):
                bits.append(bit)
                byte_num = bit // 8
                bit_in_byte = bit % 8
                if bit_in_byte == 0:
                    bit = (byte_num + 1) * 8 + 7
                else:
                    bit -= 1
        return bits

    # ─── Drawing ──────────────────────────────────────────────────────

    def run(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        curses.use_default_colors()

        # Init color pairs
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)   # selected/cursor
        curses.init_pair(2, curses.COLOR_WHITE, -1)                    # accent (was cyan)
        curses.init_pair(3, curses.COLOR_GREEN, -1)                   # green
        curses.init_pair(4, curses.COLOR_YELLOW, -1)                  # yellow
        curses.init_pair(5, curses.COLOR_RED, -1)                     # red
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)                 # magenta
        curses.init_pair(7, curses.COLOR_WHITE, -1)                   # bright white
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)    # header bar
        curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_GREEN)   # active file tab
        curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_YELLOW) # active sidebar tab

        self.COL_SEL = curses.color_pair(1)
        self.COL_ACCENT = curses.color_pair(2)
        self.COL_GREEN = curses.color_pair(3)
        self.COL_YELLOW = curses.color_pair(4)
        self.COL_RED = curses.color_pair(5)
        self.COL_MAGENTA = curses.color_pair(6)
        self.COL_BRIGHT = curses.color_pair(7) | curses.A_BOLD
        self.COL_HEADER = curses.color_pair(8) | curses.A_BOLD
        self.COL_FILETAB = curses.color_pair(9) | curses.A_BOLD
        self.COL_SIDETAB = curses.color_pair(10) | curses.A_BOLD
        self.COL_DIM = curses.color_pair(7) | curses.A_DIM
        self.COL_TITLE = curses.color_pair(7) | curses.A_BOLD

        # Style map for detail lines
        self._style_map = {
            "header": self.COL_YELLOW | curses.A_BOLD,
            "title": self.COL_TITLE,
            "dim": self.COL_DIM,
            "accent": self.COL_ACCENT | curses.A_BOLD,
            "green": self.COL_GREEN,
        }

        while True:
            self.draw()
            key = stdscr.getch()
            if not self.handle_key(key):
                break

    def _resolve_style(self, attr):
        """Resolve a style: could be a curses attr int or a string key."""
        if isinstance(attr, str):
            return self._style_map.get(attr, 0)
        return attr

    def draw(self):
        # clear() not erase(): erase() leaves ghost cells at panel boundaries when switching messages
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()
        if h < 10 or w < 40:
            self.stdscr.addstr(0, 0, "Terminal too small")
            self.stdscr.refresh()
            return

        # Header bar (line 0)
        self._draw_header(w)

        # File tabs (line 1)
        self._draw_file_tabs(w)

        # Sidebar tabs (line 2)
        sidebar_w = min(40, w // 3)
        self._draw_sidebar_tabs(sidebar_w)

        # Divider under sidebar tabs
        try:
            self.stdscr.addstr(3, 0, "\u2500" * sidebar_w, self.COL_DIM)
        except curses.error:
            pass

        # Sidebar list (lines 4 to h-2)
        self._draw_sidebar(sidebar_w, 4, h - 2)

        # Detail panel
        detail_x = sidebar_w + 2
        detail_w = w - detail_x - 1
        if detail_w > 10:
            # Vertical separator
            for y in range(2, h - 1):
                try:
                    self.stdscr.addstr(y, sidebar_w + 1, "\u2502", self.COL_DIM)
                except curses.error:
                    pass
            self._draw_detail(detail_x, 2, detail_w, h - 3)

        # Status bar (last line)
        self._draw_status(h, w)

        self.stdscr.refresh()

    def _draw_header(self, w):
        # Full-width header bar
        bar = " " * w
        try:
            self.stdscr.addstr(0, 0, bar[:w-1], self.COL_HEADER)
        except curses.error:
            pass
        title = " \u25c6 DBC Viewer "
        try:
            self.stdscr.addstr(0, 0, title, self.COL_HEADER)
        except curses.error:
            pass
        if self.db:
            msgs = self._get_messages()
            sigs = sum(len(m["signals"]) for m in msgs)
            info = f"\u2502 {len(msgs)} msgs  {sigs} sigs  {len(self.db['nodes'])} nodes "
            pos = w - len(info) - 1
            if pos > len(title):
                try:
                    self.stdscr.addstr(0, pos, info, self.COL_HEADER)
                except curses.error:
                    pass

    def _draw_file_tabs(self, w):
        if not self.file_names:
            return
        labels = [f" {f.replace('.dbc', '')} " for f in self.file_names]

        # Scroll the tab strip so the active tab is always visible.
        start = 0
        while start <= self.active_file_idx:
            used = 1
            last_fit = start - 1
            for i in range(start, len(labels)):
                end = used + len(labels[i])
                if end > w - 1:
                    break
                last_fit = i
                used = end + 1
            if last_fit >= self.active_file_idx:
                break
            start += 1

        x = 1
        for i in range(start, len(labels)):
            label = labels[i]
            if x + len(label) > w - 1:
                break
            style = self.COL_FILETAB if i == self.active_file_idx else self.COL_DIM
            try:
                self.stdscr.addstr(1, x, label, style)
            except curses.error:
                pass
            x += len(label) + 1

    def _draw_sidebar_tabs(self, w):
        x = 0
        icons = ["\u25a0", "\u25cf", "\u2261", "\u2315", "\u2139"]
        for i, name in enumerate(self.TAB_NAMES):
            icon = icons[i] if i < len(icons) else " "
            label = f" {icon} {name} "
            if x + len(label) > w:
                break
            if i == self.sidebar_tab:
                try:
                    self.stdscr.addstr(2, x, label, self.COL_SIDETAB)
                except curses.error:
                    pass
            else:
                try:
                    self.stdscr.addstr(2, x, label, self.COL_DIM)
                except curses.error:
                    pass
            x += len(label)

    def _draw_sidebar(self, w, y_start, y_end):
        visible = y_end - y_start
        if visible <= 0:
            return

        # Adjust scroll
        if self.sidebar_cursor < self.sidebar_scroll:
            self.sidebar_scroll = self.sidebar_cursor
        if self.sidebar_cursor >= self.sidebar_scroll + visible:
            self.sidebar_scroll = self.sidebar_cursor - visible + 1

        focus_here = (self.focus == self.FOCUS_SIDEBAR)

        for vi in range(visible):
            idx = self.sidebar_scroll + vi
            y = y_start + vi
            if idx >= len(self.sidebar_items):
                break

            kind, data = self.sidebar_items[idx]
            is_selected = (idx == self.sidebar_cursor)

            if kind == "msg":
                id_str = data['hex_id']
                name = data['name']
                sig_count = len(data['signals'])
                line1 = f" {id_str:>7} \u2502 {name}"
                # Truncate and pad
                line = line1[:w - 1].ljust(w - 1)
            elif kind == "node":
                line = f" \u25cf {data}"[:w - 1].ljust(w - 1)
            elif kind == "vt":
                count = len(self.db["value_tables"].get(data, {}))
                line = f" \u2261 {data} ({count})"[:w - 1].ljust(w - 1)
            elif kind == "sig_ref":
                msg, sig = data
                line = f" {msg['hex_id']}:{sig['name']}"[:w - 1].ljust(w - 1)
            elif kind == "info":
                line = " \u2139  Database Overview"[:w - 1].ljust(w - 1)
            else:
                line = (" ???")[:w - 1].ljust(w - 1)

            try:
                if is_selected and focus_here:
                    self.stdscr.addstr(y, 0, line, self.COL_SEL)
                elif is_selected:
                    self.stdscr.addstr(y, 0, line, curses.A_REVERSE | curses.A_DIM)
                elif kind == "msg":
                    # Color the hex ID part differently
                    self.stdscr.addstr(y, 0, line, 0)
                    # Overlay the ID in yellow
                    id_part = f" {data['hex_id']:>7}"
                    self.stdscr.addstr(y, 0, id_part, self.COL_YELLOW)
                else:
                    self.stdscr.addstr(y, 0, line)
            except curses.error:
                pass

        # Sidebar scroll indicator
        total = len(self.sidebar_items)
        if total > visible and visible > 0:
            bar_h = max(1, visible * visible // total)
            max_s = max(1, total - visible)
            bar_y = int(self.sidebar_scroll / max_s * (visible - bar_h))
            for vi in range(visible):
                try:
                    ch = "\u2588" if bar_y <= vi < bar_y + bar_h else "\u2502"
                    style = self.COL_DIM if ch == "\u2502" else self.COL_YELLOW
                    self.stdscr.addstr(y_start + vi, w, ch, style)
                except curses.error:
                    pass

    def _draw_detail(self, x, y_start, w, visible):
        if not self.detail_lines:
            return

        # Clamp scroll
        max_scroll = max(0, len(self.detail_lines) - visible)
        self.detail_scroll = max(0, min(self.detail_scroll, max_scroll))

        for vi in range(visible):
            idx = self.detail_scroll + vi
            y = y_start + vi
            if idx >= len(self.detail_lines):
                break
            text, attr = self.detail_lines[idx]
            display = text[:w]
            if isinstance(attr, list):
                # List of (col_offset, length, style) overlays on a default-styled base
                try:
                    self.stdscr.addstr(y, x, display, 0)
                except curses.error:
                    pass
                for col_off, seg_len, seg_style in attr:
                    if col_off >= len(display):
                        continue
                    segment = display[col_off:col_off + seg_len]
                    try:
                        self.stdscr.addstr(y, x + col_off, segment, self._resolve_style(seg_style))
                    except curses.error:
                        pass
            else:
                try:
                    self.stdscr.addstr(y, x, display, self._resolve_style(attr))
                except curses.error:
                    pass

        # Scroll indicator (right edge)
        if len(self.detail_lines) > visible and visible > 0:
            bar_h = max(1, visible * visible // len(self.detail_lines))
            bar_y = int(self.detail_scroll / max(1, max_scroll) * (visible - bar_h))
            for vi in range(visible):
                try:
                    if bar_y <= vi < bar_y + bar_h:
                        self.stdscr.addstr(y_start + vi, x + w, "\u2588", self.COL_YELLOW)
                except curses.error:
                    pass

    def _draw_status(self, h, w):
        # Build left side
        focus_label = "SIDEBAR" if self.focus == self.FOCUS_SIDEBAR else "DETAIL "
        left = f" [{focus_label}] "

        if self.search_query:
            left += f"\u2315 {self.search_query}  \u2502  "

        items_count = len(self.sidebar_items)
        left += f"{items_count} items"
        if self.sidebar_items and self.sidebar_cursor < items_count:
            left += f" [{self.sidebar_cursor + 1}/{items_count}]"

        # Right side help
        right = " q:Quit  /:Search  f:File  Tab:Panel  Enter:Expand "

        padding = max(0, w - len(left) - len(right))
        full = (left + " " * padding + right)[:w - 1]
        try:
            self.stdscr.addstr(h - 1, 0, full, curses.A_REVERSE)
        except curses.error:
            pass

    # ─── Key Handling ─────────────────────────────────────────────────

    def handle_key(self, key):
        h, w = self.stdscr.getmaxyx()

        # Quit
        if key == ord('q') or key == 27:  # q or Esc
            if self.search_query or self.sidebar_tab == self.TAB_SEARCH:
                self.search_query = ""
                self.sidebar_tab = self.TAB_MESSAGES
                self._rebuild_sidebar()
                return True
            if self.expanded_signal >= 0:
                self.expanded_signal = -1
                self._build_detail()
                return True
            if self.focus == self.FOCUS_DETAIL:
                self.focus = self.FOCUS_SIDEBAR
                return True
            return False

        # Tab switching
        if key == 9:  # Tab
            self.focus = 1 - self.focus
            return True
        if key == 353:  # Shift+Tab
            self.focus = 1 - self.focus
            return True

        # Number keys for sidebar tabs
        for n in range(5):
            if key == ord(str(n + 1)):
                self.sidebar_tab = n
                self._rebuild_sidebar()
                return True

        # File switch
        if key == ord('f'):
            if len(self.file_names) > 1:
                self.active_file_idx = (self.active_file_idx + 1) % len(self.file_names)
                self._rebuild_sidebar()
            return True
        if key == ord('F'):
            if len(self.file_names) > 1:
                self.active_file_idx = (self.active_file_idx - 1) % len(self.file_names)
                self._rebuild_sidebar()
            return True

        # Search
        if key == ord('/'):
            self._do_search()
            return True

        if self.focus == self.FOCUS_SIDEBAR:
            return self._handle_sidebar_key(key)
        else:
            return self._handle_detail_key(key)

    def _handle_sidebar_key(self, key):
        if not self.sidebar_items:
            return True

        if key in (curses.KEY_UP, ord('k')):
            self.sidebar_cursor = max(0, self.sidebar_cursor - 1)
            self.expanded_signal = -1
            self.signal_cursor = 0
            self._build_detail()
        elif key in (curses.KEY_DOWN, ord('j')):
            self.sidebar_cursor = min(len(self.sidebar_items) - 1, self.sidebar_cursor + 1)
            self.expanded_signal = -1
            self.signal_cursor = 0
            self._build_detail()
        elif key in (curses.KEY_PPAGE,):
            self.sidebar_cursor = max(0, self.sidebar_cursor - 10)
            self.expanded_signal = -1
            self.signal_cursor = 0
            self._build_detail()
        elif key in (curses.KEY_NPAGE,):
            self.sidebar_cursor = min(len(self.sidebar_items) - 1, self.sidebar_cursor + 10)
            self.expanded_signal = -1
            self.signal_cursor = 0
            self._build_detail()
        elif key in (curses.KEY_HOME,):
            self.sidebar_cursor = 0
            self.expanded_signal = -1
            self.signal_cursor = 0
            self._build_detail()
        elif key in (curses.KEY_END,):
            self.sidebar_cursor = max(0, len(self.sidebar_items) - 1)
            self.expanded_signal = -1
            self.signal_cursor = 0
            self._build_detail()
        elif key in (10, curses.KEY_ENTER, ord('\n')):
            # Enter -> focus detail
            self.focus = self.FOCUS_DETAIL
            self.detail_scroll = 0

        return True

    def _handle_detail_key(self, key):
        # Check if we're viewing a message (has signals to navigate)
        has_signals = False
        num_sigs = 0
        if self.sidebar_items:
            kind, data = self.sidebar_items[min(self.sidebar_cursor, len(self.sidebar_items) - 1)]
            if kind == "msg":
                has_signals = len(data["signals"]) > 0
                num_sigs = len(data["signals"])

        if has_signals:
            # j/k and arrows move the signal cursor, not the scroll
            if key in (curses.KEY_UP, ord('k')):
                self.signal_cursor = max(0, self.signal_cursor - 1)
                self._build_detail()
                self._scroll_to_signal_cursor()
            elif key in (curses.KEY_DOWN, ord('j')):
                self.signal_cursor = min(num_sigs - 1, self.signal_cursor + 1)
                self._build_detail()
                self._scroll_to_signal_cursor()
            elif key in (curses.KEY_PPAGE,):
                self.signal_cursor = max(0, self.signal_cursor - 10)
                self._build_detail()
                self._scroll_to_signal_cursor()
            elif key in (curses.KEY_NPAGE,):
                self.signal_cursor = min(num_sigs - 1, self.signal_cursor + 10)
                self._build_detail()
                self._scroll_to_signal_cursor()
            elif key in (curses.KEY_HOME,):
                self.signal_cursor = 0
                self._build_detail()
                self._scroll_to_signal_cursor()
            elif key in (curses.KEY_END,):
                self.signal_cursor = max(0, num_sigs - 1)
                self._build_detail()
                self._scroll_to_signal_cursor()
            elif key in (10, curses.KEY_ENTER, ord('\n')):
                # Toggle expand on signal at cursor
                if self.expanded_signal == self.signal_cursor:
                    self.expanded_signal = -1
                else:
                    self.expanded_signal = self.signal_cursor
                self._build_detail()
                self._scroll_to_signal_cursor()
        else:
            # Non-message detail: plain scrolling
            if key in (curses.KEY_UP, ord('k')):
                self.detail_scroll = max(0, self.detail_scroll - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                self.detail_scroll += 1
            elif key in (curses.KEY_PPAGE,):
                self.detail_scroll = max(0, self.detail_scroll - 20)
            elif key in (curses.KEY_NPAGE,):
                self.detail_scroll += 20
            elif key in (curses.KEY_HOME,):
                self.detail_scroll = 0
            elif key in (curses.KEY_END,):
                self.detail_scroll = max(0, len(self.detail_lines) - 10)

        return True

    def _scroll_to_signal_cursor(self):
        """Auto-scroll the detail panel so the signal cursor is visible."""
        if not hasattr(self, '_signal_line_start'):
            return
        # Find the line index of the current signal cursor
        # We need to count through the detail lines to find which line has the cursor
        target_line = None
        for i, (text, attr) in enumerate(self.detail_lines):
            if attr == curses.A_REVERSE:  # Our cursor line
                target_line = i
                break
        if target_line is not None:
            h, w = self.stdscr.getmaxyx()
            visible = h - 5  # approximate visible area
            if target_line < self.detail_scroll:
                self.detail_scroll = target_line
            elif target_line >= self.detail_scroll + visible:
                self.detail_scroll = target_line - visible + 3

    def _do_search(self):
        """Enter search mode - read a search string."""
        curses.curs_set(1)
        h, w = self.stdscr.getmaxyx()
        self.stdscr.addstr(h - 1, 0, " " * (w - 1), curses.A_REVERSE)
        self.stdscr.addstr(h - 1, 0, " Search: ", curses.A_REVERSE)
        self.stdscr.refresh()

        query = ""
        while True:
            try:
                self.stdscr.addstr(h - 1, 9, query + " " * 20, curses.A_REVERSE)
                self.stdscr.move(h - 1, 9 + len(query))
                self.stdscr.refresh()
            except curses.error:
                pass

            ch = self.stdscr.getch()
            if ch in (10, curses.KEY_ENTER):
                break
            elif ch == 27:  # Esc
                query = ""
                break
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
            elif 32 <= ch < 127:
                query += chr(ch)

        curses.curs_set(0)
        self.search_query = query
        if query:
            self.sidebar_tab = self.TAB_SEARCH
        self._rebuild_sidebar()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DBC TUI Viewer - Terminal CAN database viewer")
    parser.add_argument("files", nargs="*", help="DBC files to load (default: all .dbc in current dir)")
    args = parser.parse_args()

    if args.files:
        dbc_files = args.files
    else:
        dbc_files = sorted(glob.glob("*.dbc")) + sorted(glob.glob("Archived/*.dbc"))

    if not dbc_files:
        print("No .dbc files found. Provide files as arguments or run from a directory with .dbc files.")
        sys.exit(1)

    databases = {}
    for filepath in dbc_files:
        filepath = os.path.abspath(filepath)
        print(f"  Parsing: {os.path.basename(filepath)}...")
        try:
            db = parse_dbc(filepath)
            databases[db["filename"]] = db
            msg_count = len([m for m in db["messages"].values() if m["name"] != "VECTOR__INDEPENDENT_SIG_MSG"])
            sig_count = sum(len(m["signals"]) for m in db["messages"].values())
            print(f"    -> {msg_count} messages, {sig_count} signals, {len(db['nodes'])} nodes")
        except Exception as e:
            print(f"    -> Error: {e}")

    if not databases:
        print("No databases loaded.")
        sys.exit(1)

    print("\n  Launching TUI...\n")
    app = DBCTui(databases)
    curses.wrapper(app.run)


if __name__ == "__main__":
    main()
