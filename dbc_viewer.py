#!/usr/bin/env python3
"""
DBC Viewer - A web-based CAN database file viewer for macOS.
Replacement for CANdb++ with no external dependencies.

Usage:
    python3 dbc_viewer.py                      # Auto-loads all .dbc files in current dir
    python3 dbc_viewer.py file1.dbc file2.dbc  # Load specific files
    python3 dbc_viewer.py path/to/folder       # Load all .dbc files under a folder (recursive)
    python3 dbc_viewer.py --port 9000          # Custom port
"""

import os
import re
import sys
import json
import glob
import html
import argparse
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ─── DBC Parser ──────────────────────────────────────────────────────────────

def parse_dbc(filepath):
    """Parse a .dbc file and return a structured dictionary."""
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
        "signal_groups": [],
    }

    lines = content.replace("\r\n", "\n").split("\n")

    # --- VERSION ---
    m = re.search(r'^VERSION\s+"(.*?)"', content, re.MULTILINE)
    if m:
        db["version"] = m.group(1)

    # --- BU_ (Nodes) ---
    m = re.search(r"^BU_\s*:(.*?)$", content, re.MULTILINE)
    if m:
        db["nodes"] = m.group(1).strip().split()

    # --- VAL_TABLE_ (Value Tables) ---
    for m in re.finditer(
        r"^VAL_TABLE_\s+(\S+)(.*?);", content, re.MULTILINE | re.DOTALL
    ):
        name = m.group(1)
        pairs = _parse_value_pairs(m.group(2))
        db["value_tables"][name] = pairs

    # --- BO_ / SG_ (Messages & Signals) ---
    # We parse line-by-line for messages and their signals
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Message definition
        bo_match = re.match(
            r"^BO_\s+(\d+)\s+(\S+)\s*:\s*(\d+)\s+(\S+)", line
        )
        if bo_match:
            msg_id = int(bo_match.group(1))
            msg_name = bo_match.group(2)
            msg_dlc = int(bo_match.group(3))
            msg_sender = bo_match.group(4)

            is_extended = (msg_id & 0x80000000) != 0
            can_id = msg_id & 0x1FFFFFFF if is_extended else msg_id & 0x7FF

            msg = {
                "id": msg_id,
                "can_id": can_id,
                "hex_id": f"0x{can_id:03X}",
                "name": msg_name,
                "dlc": msg_dlc,
                "sender": msg_sender,
                "is_extended": is_extended,
                "signals": [],
            }

            i += 1
            # Parse signals within this message
            while i < len(lines):
                sline = lines[i]
                sg_match = re.match(
                    r"^\s+SG_\s+(\S+)\s*(?:(\S+)\s+)?:\s*(\d+)\|(\d+)@([01])([+-])"
                    r"\s+\(([^,]+),([^)]+)\)\s+\[([^|]+)\|([^\]]+)\]\s+"
                    r'"([^"]*)"\s+(.*)',
                    sline,
                )
                if sg_match:
                    sig = {
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
                        "receivers": [
                            r.strip()
                            for r in sg_match.group(12).rstrip(";").split(",")
                            if r.strip()
                        ],
                    }
                    msg["signals"].append(sig)
                    i += 1
                else:
                    break

            db["messages"][msg_id] = msg
            continue

        i += 1

    # --- CM_ (Comments) ---
    # Comments can span multiple lines, ending with ";
    # Join all content and parse with regex
    for m in re.finditer(
        r'CM_\s+BU_\s+(\S+)\s+"(.*?)"\s*;', content, re.DOTALL
    ):
        db["comments"]["nodes"][m.group(1)] = m.group(2).replace("\n", " ").strip()

    for m in re.finditer(
        r'CM_\s+BO_\s+(\d+)\s+"(.*?)"\s*;', content, re.DOTALL
    ):
        db["comments"]["messages"][int(m.group(1))] = m.group(2).replace("\n", " ").strip()

    for m in re.finditer(
        r'CM_\s+SG_\s+(\d+)\s+(\S+)\s+"(.*?)"\s*;', content, re.DOTALL
    ):
        key = f"{m.group(1)}_{m.group(2)}"
        db["comments"]["signals"][key] = m.group(3).replace("\n", " ").strip()

    # --- VAL_ (Value Descriptions for Signals) ---
    for m in re.finditer(
        r"^VAL_\s+(\d+)\s+(\S+)(.*?);", content, re.MULTILINE | re.DOTALL
    ):
        msg_id = int(m.group(1))
        sig_name = m.group(2)
        pairs = _parse_value_pairs(m.group(3))
        key = f"{msg_id}_{sig_name}"
        db["value_descriptions"][key] = pairs

    # --- BA_DEF_ (Attribute Definitions) ---
    for m in re.finditer(
        r'^BA_DEF_\s+(BO_|SG_|BU_|)?\s*"(\S+)"\s+(.*?);',
        content,
        re.MULTILINE,
    ):
        db["attribute_definitions"].append(
            {
                "object_type": m.group(1).strip() if m.group(1) else "DB",
                "name": m.group(2),
                "definition": m.group(3).strip(),
            }
        )

    # --- BA_DEF_DEF_ (Attribute Defaults) ---
    for m in re.finditer(
        r'^BA_DEF_DEF_\s+"(\S+)"\s+(.*?);', content, re.MULTILINE
    ):
        db["attribute_defaults"][m.group(1)] = m.group(2).strip().strip('"')

    # --- BA_ (Attribute Values) ---
    for m in re.finditer(
        r'^BA_\s+"(\S+)"\s+BO_\s+(\d+)\s+(.*?);', content, re.MULTILINE
    ):
        msg_id = int(m.group(2))
        if msg_id not in db["attributes"]["messages"]:
            db["attributes"]["messages"][msg_id] = {}
        val = m.group(3).strip().strip('"')
        db["attributes"]["messages"][msg_id][m.group(1)] = val

    for m in re.finditer(
        r'^BA_\s+"(\S+)"\s+SG_\s+(\d+)\s+(\S+)\s+(.*?);',
        content,
        re.MULTILINE,
    ):
        key = f"{m.group(2)}_{m.group(3)}"
        if key not in db["attributes"]["signals"]:
            db["attributes"]["signals"][key] = {}
        db["attributes"]["signals"][key][m.group(1)] = m.group(4).strip().strip('"')

    # --- BO_TX_BU_ (Multiple Transmitters) ---
    for m in re.finditer(
        r"^BO_TX_BU_\s+(\d+)\s*:\s*(.*?);", content, re.MULTILINE
    ):
        msg_id = int(m.group(1))
        transmitters = [t.strip() for t in m.group(2).split(",") if t.strip()]
        if msg_id in db["messages"]:
            db["messages"][msg_id]["transmitters"] = transmitters

    return db


def _parse_value_pairs(text):
    """Parse value-description pairs like: 1 "On" 0 "Off" """
    pairs = {}
    for m in re.finditer(r'(\d+)\s+"([^"]*)"', text):
        pairs[int(m.group(1))] = m.group(2)
    return pairs


def db_to_json(db):
    """Convert parsed DB to JSON-serializable dict with string keys."""
    out = dict(db)
    # Convert int-keyed dicts to string-keyed for JSON
    out["messages"] = {}
    for msg_id, msg in db["messages"].items():
        out["messages"][str(msg_id)] = msg
    out["comments"] = {
        "nodes": db["comments"]["nodes"],
        "messages": {str(k): v for k, v in db["comments"]["messages"].items()},
        "signals": db["comments"]["signals"],
    }
    out["value_descriptions"] = db["value_descriptions"]
    out["attributes"] = {
        "messages": {str(k): v for k, v in db["attributes"]["messages"].items()},
        "signals": db["attributes"]["signals"],
        "nodes": db["attributes"]["nodes"],
    }
    return out


# ─── HTML Dashboard ──────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DBC Viewer</title>
<style>
:root {
  --bg: #111111;
  --bg2: #1a1a1a;
  --bg3: #252525;
  --border: #333333;
  --text: #e0e0e0;
  --text2: #909090;
  --accent: #e0e0e0;
  --accent2: #3a3a3a;
  --green: #4ade80;
  --orange: #fb923c;
  --red: #f87171;
  --yellow: #facc15;
  --purple: #a78bfa;
  --cyan: #22d3ee;
  --radius: 8px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  overflow: hidden;
  height: 100vh;
}

/* Header */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 24px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  height: 56px;
  flex-shrink: 0;
}
.header h1 {
  font-size: 18px;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: -0.3px;
}
.header h1 span { color: var(--text2); font-weight: 400; }

.file-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  flex: 1;
  margin: 0 24px;
}
.file-tab {
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
  color: var(--text2);
  background: transparent;
  border: 1px solid transparent;
  transition: all 0.15s;
}
.file-tab:hover { color: var(--text); background: var(--bg3); }
.file-tab.active {
  color: var(--accent);
  background: var(--bg3);
  border-color: var(--accent);
}

/* Layout */
.main {
  display: flex;
  height: calc(100vh - 56px);
}

/* Sidebar */
.sidebar {
  width: 360px;
  min-width: 280px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.search-box {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}
.search-box input {
  width: 100%;
  padding: 8px 12px;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s;
}
.search-box input:focus { border-color: var(--accent); }
.search-box input::placeholder { color: var(--text2); }

.sidebar-tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
}
.sidebar-tab {
  flex: 1;
  padding: 8px;
  text-align: center;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  color: var(--text2);
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
}
.sidebar-tab:hover { color: var(--text); }
.sidebar-tab.active { color: var(--accent); border-bottom-color: var(--accent); }

.sidebar-content {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

/* Message list */
.msg-item {
  padding: 10px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background 0.1s;
  margin-bottom: 2px;
}
.msg-item:hover { background: var(--bg3); }
.msg-item.active { background: var(--accent2); }
.msg-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.msg-id {
  font-size: 11px;
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--cyan);
  background: rgba(34, 211, 238, 0.1);
  padding: 1px 6px;
  border-radius: 4px;
}
.msg-meta {
  font-size: 11px;
  color: var(--text2);
  margin-top: 2px;
}
.msg-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 500;
}
.badge-signals { background: rgba(255, 255, 255, 0.08); color: var(--text); }
.badge-dlc { background: rgba(74, 222, 128, 0.15); color: var(--green); }

/* Node list */
.node-item {
  padding: 10px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  margin-bottom: 2px;
  transition: background 0.1s;
}
.node-item:hover { background: var(--bg3); }
.node-item.active { background: var(--accent2); }
.node-name { font-size: 13px; font-weight: 600; }
.node-comment { font-size: 11px; color: var(--text2); margin-top: 2px; }
.node-stats { font-size: 11px; color: var(--text2); }

/* Value table list */
.vt-item {
  padding: 8px 12px;
  border-radius: var(--radius);
  cursor: pointer;
  margin-bottom: 2px;
  transition: background 0.1s;
}
.vt-item:hover { background: var(--bg3); }
.vt-item.active { background: var(--accent2); }
.vt-name { font-size: 13px; font-weight: 500; }
.vt-count { font-size: 11px; color: var(--text2); }

/* Detail panel */
.detail {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.detail-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text2);
  font-size: 14px;
}

.detail-header {
  margin-bottom: 24px;
}
.detail-title {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.5px;
  margin-bottom: 8px;
}
.detail-subtitle {
  font-size: 13px;
  color: var(--text2);
}

/* Stats row */
.stats-row {
  display: flex;
  gap: 12px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.stat-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
  min-width: 140px;
  flex: 1;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -1px;
}
.stat-label {
  font-size: 12px;
  color: var(--text2);
  margin-top: 2px;
}
.stat-accent { color: #fff; }
.stat-green { color: var(--green); }
.stat-orange { color: var(--orange); }
.stat-purple { color: var(--purple); }
.stat-cyan { color: var(--cyan); }

/* Info grid */
.info-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.info-item {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 16px;
}
.info-label { font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; }
.info-value { font-size: 14px; font-weight: 500; margin-top: 4px; }

/* Section */
.section {
  margin-bottom: 24px;
}
.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}

/* Signal table */
.signal-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.signal-table th {
  text-align: left;
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  position: sticky;
  top: 0;
  z-index: 1;
}
.signal-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.signal-table tr { cursor: pointer; transition: background 0.1s; }
.signal-table tbody tr:hover { background: var(--bg3); }
.signal-table tr.expanded { background: var(--bg3); }

.sig-name { font-weight: 600; color: #fff; }
.sig-mono {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
}
.sig-unit {
  color: var(--yellow);
  font-size: 11px;
}
.sig-range {
  color: var(--text2);
  font-size: 11px;
}

/* Signal detail row */
.sig-detail-row td {
  padding: 0 !important;
  border-bottom: 1px solid var(--border);
}
.sig-detail {
  padding: 12px 16px 16px 16px;
  background: var(--bg2);
  display: none;
}
.sig-detail.show { display: block; }
.sig-detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}
.sig-detail-item {
  padding: 8px 12px;
  background: var(--bg3);
  border-radius: 6px;
}
.sig-detail-label { font-size: 10px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; }
.sig-detail-value { font-size: 13px; font-weight: 500; margin-top: 2px; }

.sig-comment {
  font-size: 12px;
  color: var(--text2);
  padding: 8px 12px;
  background: var(--bg3);
  border-radius: 6px;
  margin-bottom: 8px;
  line-height: 1.6;
  border-left: 3px solid var(--orange);
}

.val-desc-table {
  font-size: 12px;
  border-collapse: collapse;
  width: 100%;
  max-width: 400px;
}
.val-desc-table th {
  text-align: left;
  padding: 4px 12px;
  color: var(--text2);
  font-size: 11px;
  border-bottom: 1px solid var(--border);
}
.val-desc-table td {
  padding: 4px 12px;
  border-bottom: 1px solid rgba(46,51,72,0.5);
}
.val-desc-table .val-num {
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--cyan);
}

/* Bit layout */
.bit-layout {
  margin-bottom: 24px;
  overflow-x: auto;
}
.bit-grid {
  display: grid;
  gap: 2px;
  font-size: 10px;
  font-family: 'SF Mono', 'Fira Code', monospace;
}
.bit-row {
  display: flex;
  gap: 2px;
}
.bit-cell {
  width: 36px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  font-size: 9px;
  text-align: center;
  overflow: hidden;
}
.bit-header {
  background: transparent;
  color: var(--text2);
  font-weight: 600;
}
.bit-byte-label {
  width: 50px;
  background: transparent;
  color: var(--text2);
  font-weight: 500;
  justify-content: flex-end;
  padding-right: 8px;
}
.bit-used {
  color: #fff;
  font-size: 8px;
  cursor: pointer;
  position: relative;
}
.bit-used:hover { filter: brightness(1.2); }
.bit-empty {
  background: var(--bg3);
  color: var(--text2);
  opacity: 0.3;
}

/* Color palette for signals in bit layout */
.sig-color-0 { background: #b05ce6; }
.sig-color-1 { background: #059669; }
.sig-color-2 { background: #d97706; }
.sig-color-3 { background: #dc2626; }
.sig-color-4 { background: #7c3aed; }
.sig-color-5 { background: #0891b2; }
.sig-color-6 { background: #be185d; }
.sig-color-7 { background: #65a30d; }
.sig-color-8 { background: #c2410c; }
.sig-color-9 { background: #a16207; }
.sig-color-10 { background: #0e7490; }
.sig-color-11 { background: #b91c1c; }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--bg3); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--border); }

/* Tooltip */
.tooltip {
  position: fixed;
  background: var(--bg3);
  border: 1px solid var(--border);
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 11px;
  pointer-events: none;
  z-index: 100;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  max-width: 250px;
}

/* Comment block */
.comment-block {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-left: 3px solid var(--orange);
  border-radius: var(--radius);
  padding: 12px 16px;
  margin-bottom: 12px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text2);
}

/* Responsive */
@media (max-width: 900px) {
  .sidebar { width: 260px; min-width: 200px; }
  .stats-row { flex-direction: column; }
}
</style>
</head>
<body>

<div class="header">
  <h1>DBC Viewer <span>/ CAN Database</span></h1>
  <div class="file-tabs" id="fileTabs"></div>
</div>

<div class="main">
  <div class="sidebar">
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="Search messages, signals, nodes...">
    </div>
    <div class="sidebar-tabs">
      <div class="sidebar-tab active" data-tab="messages">Messages</div>
      <div class="sidebar-tab" data-tab="nodes">Nodes</div>
      <div class="sidebar-tab" data-tab="vtables">Value Tables</div>
    </div>
    <div class="sidebar-content" id="sidebarContent"></div>
  </div>
  <div class="detail" id="detailPanel">
    <div class="detail-empty">Select a file to begin browsing</div>
  </div>
</div>

<div class="tooltip" id="tooltip" style="display:none"></div>

<script>
// ─── State ──────────────────────────────────────────────────────────
let databases = {};       // filename -> parsed db
let activeFile = null;
let activeTab = "messages";
let selectedMsg = null;
let selectedNode = null;
let selectedVT = null;

// ─── Init ───────────────────────────────────────────────────────────
async function init() {
  const resp = await fetch("/api/databases");
  databases = await resp.json();
  const files = Object.keys(databases);
  if (files.length === 0) {
    document.getElementById("detailPanel").innerHTML =
      '<div class="detail-empty">No .dbc files found</div>';
    return;
  }
  renderFileTabs(files);
  selectFile(files[0]);
}

// ─── File Tabs ──────────────────────────────────────────────────────
function renderFileTabs(files) {
  const container = document.getElementById("fileTabs");
  container.innerHTML = files.map(f =>
    `<div class="file-tab" data-file="${f}">${f}</div>`
  ).join("");
  container.querySelectorAll(".file-tab").forEach(tab => {
    tab.onclick = () => selectFile(tab.dataset.file);
  });
}

function selectFile(filename) {
  activeFile = filename;
  selectedMsg = null;
  selectedNode = null;
  selectedVT = null;
  document.querySelectorAll(".file-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.file === filename)
  );
  renderSidebar();
  renderOverview();
}

// ─── Sidebar Tabs ───────────────────────────────────────────────────
document.querySelectorAll(".sidebar-tab").forEach(tab => {
  tab.onclick = () => {
    activeTab = tab.dataset.tab;
    document.querySelectorAll(".sidebar-tab").forEach(t =>
      t.classList.toggle("active", t === tab)
    );
    renderSidebar();
  };
});

document.getElementById("searchInput").addEventListener("input", () => renderSidebar());

// ─── Sidebar Render ─────────────────────────────────────────────────
function renderSidebar() {
  const db = databases[activeFile];
  if (!db) return;
  const query = document.getElementById("searchInput").value.toLowerCase();
  const container = document.getElementById("sidebarContent");

  if (activeTab === "messages") {
    const msgs = Object.values(db.messages)
      .filter(m => m.name !== "VECTOR__INDEPENDENT_SIG_MSG")
      .filter(m => {
        if (!query) return true;
        if (m.name.toLowerCase().includes(query)) return true;
        if (m.hex_id.toLowerCase().includes(query)) return true;
        if (m.sender.toLowerCase().includes(query)) return true;
        if (m.signals.some(s => s.name.toLowerCase().includes(query))) return true;
        return false;
      })
      .sort((a, b) => a.can_id - b.can_id);

    container.innerHTML = msgs.map(m => `
      <div class="msg-item ${selectedMsg === m.id ? 'active' : ''}" data-id="${m.id}">
        <div class="msg-name">
          <span class="msg-id">${m.hex_id}</span>
          ${esc(m.name)}
        </div>
        <div class="msg-meta">
          <span class="msg-badge badge-dlc">DLC ${m.dlc}</span>
          <span class="msg-badge badge-signals">${m.signals.length} sig${m.signals.length !== 1 ? 's' : ''}</span>
          &middot; ${esc(m.sender)}
        </div>
      </div>
    `).join("") || '<div style="padding:16px;color:var(--text2);">No messages match</div>';

    container.querySelectorAll(".msg-item").forEach(el => {
      el.onclick = () => {
        selectedMsg = parseInt(el.dataset.id);
        renderSidebar();
        renderMessageDetail(selectedMsg);
      };
    });

  } else if (activeTab === "nodes") {
    const nodes = db.nodes.filter(n =>
      !query || n.toLowerCase().includes(query)
    );
    container.innerHTML = nodes.map(n => {
      const comment = db.comments.nodes[n] || "";
      const txCount = Object.values(db.messages).filter(m => m.sender === n).length;
      const rxCount = Object.values(db.messages).filter(m =>
        m.signals.some(s => s.receivers.includes(n))
      ).length;
      return `
        <div class="node-item ${selectedNode === n ? 'active' : ''}" data-node="${esc(n)}">
          <div class="node-name">${esc(n)}</div>
          ${comment ? `<div class="node-comment">${esc(comment)}</div>` : ''}
          <div class="node-stats">TX: ${txCount} msgs &middot; RX: ${rxCount} msgs</div>
        </div>
      `;
    }).join("") || '<div style="padding:16px;color:var(--text2);">No nodes match</div>';

    container.querySelectorAll(".node-item").forEach(el => {
      el.onclick = () => {
        selectedNode = el.dataset.node;
        renderSidebar();
        renderNodeDetail(selectedNode);
      };
    });

  } else if (activeTab === "vtables") {
    const vts = Object.entries(db.value_tables)
      .filter(([name]) => !query || name.toLowerCase().includes(query))
      .sort((a, b) => a[0].localeCompare(b[0]));

    container.innerHTML = vts.map(([name, pairs]) => `
      <div class="vt-item ${selectedVT === name ? 'active' : ''}" data-vt="${esc(name)}">
        <div class="vt-name">${esc(name)}</div>
        <div class="vt-count">${Object.keys(pairs).length} values</div>
      </div>
    `).join("") || '<div style="padding:16px;color:var(--text2);">No value tables match</div>';

    container.querySelectorAll(".vt-item").forEach(el => {
      el.onclick = () => {
        selectedVT = el.dataset.vt;
        renderSidebar();
        renderValueTableDetail(selectedVT);
      };
    });
  }
}

// ─── Overview ───────────────────────────────────────────────────────
function renderOverview() {
  const db = databases[activeFile];
  if (!db) return;
  const panel = document.getElementById("detailPanel");
  const msgCount = Object.values(db.messages).filter(m => m.name !== "VECTOR__INDEPENDENT_SIG_MSG").length;
  const sigCount = Object.values(db.messages).reduce((acc, m) => acc + m.signals.length, 0);

  panel.innerHTML = `
    <div class="detail-header">
      <div class="detail-title">${esc(db.filename)}</div>
      <div class="detail-subtitle">${esc(db.filepath)}</div>
    </div>
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-value stat-accent">${msgCount}</div>
        <div class="stat-label">Messages</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-green">${sigCount}</div>
        <div class="stat-label">Signals</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-orange">${db.nodes.length}</div>
        <div class="stat-label">Nodes / ECUs</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-purple">${Object.keys(db.value_tables).length}</div>
        <div class="stat-label">Value Tables</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-cyan">${db.version || "N/A"}</div>
        <div class="stat-label">Version</div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">Nodes / ECUs</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;">
        ${db.nodes.map(n => `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:6px 14px;font-size:13px;font-weight:500;">${esc(n)}</div>`).join("")}
      </div>
    </div>
    <div class="section">
      <div class="section-title">All Messages</div>
      <table class="signal-table">
        <thead>
          <tr>
            <th>CAN ID</th>
            <th>Name</th>
            <th>DLC</th>
            <th>Sender</th>
            <th>Signals</th>
            <th>Cycle Time</th>
          </tr>
        </thead>
        <tbody>
          ${Object.values(db.messages)
            .filter(m => m.name !== "VECTOR__INDEPENDENT_SIG_MSG")
            .sort((a, b) => a.can_id - b.can_id)
            .map(m => {
              const attrs = db.attributes.messages[String(m.id)] || {};
              const cycle = attrs["GenMsgCycleTime"] || "-";
              return `<tr data-id="${m.id}" class="msg-overview-row">
                <td><span class="sig-mono" style="color:var(--cyan)">${m.hex_id}</span></td>
                <td style="font-weight:600">${esc(m.name)}</td>
                <td>${m.dlc}</td>
                <td>${esc(m.sender)}</td>
                <td>${m.signals.length}</td>
                <td>${cycle !== "-" ? cycle + " ms" : "-"}</td>
              </tr>`;
            }).join("")}
        </tbody>
      </table>
    </div>
  `;

  panel.querySelectorAll(".msg-overview-row").forEach(row => {
    row.onclick = () => {
      selectedMsg = parseInt(row.dataset.id);
      activeTab = "messages";
      document.querySelectorAll(".sidebar-tab").forEach(t =>
        t.classList.toggle("active", t.dataset.tab === "messages")
      );
      renderSidebar();
      renderMessageDetail(selectedMsg);
    };
  });
}

// ─── Message Detail ─────────────────────────────────────────────────
function renderMessageDetail(msgId) {
  const db = databases[activeFile];
  const msg = db.messages[String(msgId)];
  if (!msg) return;
  const panel = document.getElementById("detailPanel");
  const comment = db.comments.messages[String(msgId)] || "";
  const attrs = db.attributes.messages[String(msgId)] || {};

  // Build bit layout data
  const bitMap = buildBitMap(msg);

  panel.innerHTML = `
    <div class="detail-header">
      <div class="detail-title">${esc(msg.name)}</div>
      <div class="detail-subtitle">
        CAN ID: <strong>${msg.hex_id}</strong> (${msg.can_id})
        ${msg.is_extended ? ' &middot; Extended Frame' : ''}
        &middot; DLC: ${msg.dlc} &middot; Sender: ${esc(msg.sender)}
      </div>
    </div>
    ${comment ? `<div class="comment-block">${esc(comment)}</div>` : ""}
    <div class="info-grid">
      <div class="info-item">
        <div class="info-label">CAN ID</div>
        <div class="info-value" style="font-family:'SF Mono',monospace;color:var(--cyan)">${msg.hex_id} (${msg.can_id})</div>
      </div>
      <div class="info-item">
        <div class="info-label">Raw ID</div>
        <div class="info-value" style="font-family:'SF Mono',monospace;">${msg.id}</div>
      </div>
      <div class="info-item">
        <div class="info-label">DLC (bytes)</div>
        <div class="info-value">${msg.dlc}</div>
      </div>
      <div class="info-item">
        <div class="info-label">Sender</div>
        <div class="info-value">${esc(msg.sender)}</div>
      </div>
      ${attrs["GenMsgCycleTime"] ? `
      <div class="info-item">
        <div class="info-label">Cycle Time</div>
        <div class="info-value">${attrs["GenMsgCycleTime"]} ms</div>
      </div>` : ""}
      ${attrs["GenMsgSendType"] !== undefined ? `
      <div class="info-item">
        <div class="info-label">Send Type</div>
        <div class="info-value">${getSendType(attrs["GenMsgSendType"], db)}</div>
      </div>` : ""}
      <div class="info-item">
        <div class="info-label">Frame Type</div>
        <div class="info-value">${msg.is_extended ? "Extended (29-bit)" : "Standard (11-bit)"}</div>
      </div>
      <div class="info-item">
        <div class="info-label">Signals</div>
        <div class="info-value">${msg.signals.length}</div>
      </div>
      ${msg.transmitters && msg.transmitters.length > 0 ? `
      <div class="info-item">
        <div class="info-label">Transmitters</div>
        <div class="info-value">${msg.transmitters.map(t => esc(t)).join(", ")}</div>
      </div>` : ""}
    </div>

    ${msg.dlc > 0 ? `
    <div class="section">
      <div class="section-title">Bit Layout</div>
      <div class="bit-layout">${renderBitLayout(bitMap, msg.dlc)}</div>
    </div>` : ""}

    <div class="section">
      <div class="section-title">Signals (${msg.signals.length})</div>
      <table class="signal-table" id="signalTable">
        <thead>
          <tr>
            <th>Name</th>
            <th>Bit Pos</th>
            <th>Length</th>
            <th>Order</th>
            <th>Factor / Offset</th>
            <th>Range</th>
            <th>Unit</th>
            <th>Receivers</th>
          </tr>
        </thead>
        <tbody>
          ${msg.signals.map((s, idx) => {
            const sigKey = `${msgId}_${s.name}`;
            const sigComment = db.comments.signals[sigKey] || "";
            const sigAttrs = db.attributes.signals[sigKey] || {};
            const longName = sigAttrs["SignalLongName"] || "";
            const valDesc = db.value_descriptions[sigKey] || {};
            const hasDetail = sigComment || longName || Object.keys(valDesc).length > 0 || Object.keys(sigAttrs).length > 0;
            return `
              <tr class="sig-row" data-idx="${idx}">
                <td>
                  <span class="sig-name">${esc(s.name)}</span>
                  ${s.mux_indicator ? `<span style="color:var(--orange);font-size:11px;margin-left:4px;">${esc(s.mux_indicator)}</span>` : ""}
                  ${longName ? `<div style="font-size:11px;color:var(--text2);margin-top:1px;">${esc(longName)}</div>` : ""}
                  ${hasDetail ? '<span style="color:var(--text2);font-size:10px;margin-left:4px;">&#9660;</span>' : ""}
                </td>
                <td class="sig-mono">${s.start_bit}</td>
                <td class="sig-mono">${s.bit_length}</td>
                <td style="font-size:11px">${s.byte_order === "little_endian" ? "Intel" : "Motorola"}${s.is_signed ? " (S)" : " (U)"}</td>
                <td class="sig-mono">${s.factor} / ${s.offset}</td>
                <td class="sig-range">[${s.minimum} .. ${s.maximum}]</td>
                <td><span class="sig-unit">${esc(s.unit) || "-"}</span></td>
                <td style="font-size:11px;color:var(--text2)">${s.receivers.join(", ") || "-"}</td>
              </tr>
              <tr class="sig-detail-row" data-idx="${idx}">
                <td colspan="8">
                  <div class="sig-detail" id="sigDetail${idx}">
                    ${sigComment ? `<div class="sig-comment">${esc(sigComment)}</div>` : ""}
                    ${Object.keys(valDesc).length > 0 ? `
                      <table class="val-desc-table">
                        <thead><tr><th>Value</th><th>Description</th></tr></thead>
                        <tbody>
                          ${Object.entries(valDesc).sort((a,b) => parseInt(a[0]) - parseInt(b[0])).map(([v, d]) =>
                            `<tr><td class="val-num">${v}</td><td>${esc(d)}</td></tr>`
                          ).join("")}
                        </tbody>
                      </table>
                    ` : ""}
                    <div class="sig-detail-grid" style="margin-top:8px;">
                      <div class="sig-detail-item">
                        <div class="sig-detail-label">Physical Formula</div>
                        <div class="sig-detail-value">raw &times; ${s.factor} + ${s.offset}</div>
                      </div>
                      <div class="sig-detail-item">
                        <div class="sig-detail-label">Byte Order</div>
                        <div class="sig-detail-value">${s.byte_order === "little_endian" ? "Little Endian (Intel)" : "Big Endian (Motorola)"}</div>
                      </div>
                      <div class="sig-detail-item">
                        <div class="sig-detail-label">Signed</div>
                        <div class="sig-detail-value">${s.is_signed ? "Yes" : "No"}</div>
                      </div>
                      <div class="sig-detail-item">
                        <div class="sig-detail-label">Raw Bit Range</div>
                        <div class="sig-detail-value">Start: ${s.start_bit}, Length: ${s.bit_length}</div>
                      </div>
                      ${Object.entries(sigAttrs).filter(([k]) => k !== "SignalLongName").map(([k, v]) => `
                      <div class="sig-detail-item">
                        <div class="sig-detail-label">${esc(k)}</div>
                        <div class="sig-detail-value">${esc(v)}</div>
                      </div>`).join("")}
                    </div>
                  </div>
                </td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;

  // Toggle signal detail rows
  panel.querySelectorAll(".sig-row").forEach(row => {
    row.onclick = () => {
      const idx = row.dataset.idx;
      const detail = document.getElementById("sigDetail" + idx);
      if (detail) {
        detail.classList.toggle("show");
        row.classList.toggle("expanded");
      }
    };
  });
}

// ─── Node Detail ────────────────────────────────────────────────────
function renderNodeDetail(nodeName) {
  const db = databases[activeFile];
  const panel = document.getElementById("detailPanel");
  const comment = db.comments.nodes[nodeName] || "";
  const txMsgs = Object.values(db.messages).filter(m => m.sender === nodeName && m.name !== "VECTOR__INDEPENDENT_SIG_MSG").sort((a,b) => a.can_id - b.can_id);
  const rxMsgs = Object.values(db.messages).filter(m =>
    m.name !== "VECTOR__INDEPENDENT_SIG_MSG" && m.signals.some(s => s.receivers.includes(nodeName))
  ).sort((a,b) => a.can_id - b.can_id);

  panel.innerHTML = `
    <div class="detail-header">
      <div class="detail-title">${esc(nodeName)}</div>
      <div class="detail-subtitle">Node / ECU</div>
    </div>
    ${comment ? `<div class="comment-block">${esc(comment)}</div>` : ""}
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-value stat-accent">${txMsgs.length}</div>
        <div class="stat-label">TX Messages</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-green">${rxMsgs.length}</div>
        <div class="stat-label">RX Messages</div>
      </div>
      <div class="stat-card">
        <div class="stat-value stat-orange">${txMsgs.reduce((a,m) => a + m.signals.length, 0)}</div>
        <div class="stat-label">TX Signals</div>
      </div>
    </div>
    <div class="section">
      <div class="section-title">Transmitted Messages</div>
      ${renderMsgTable(txMsgs, db)}
    </div>
    <div class="section">
      <div class="section-title">Received Messages</div>
      ${renderMsgTable(rxMsgs, db)}
    </div>
  `;
  panel.querySelectorAll(".msg-link-row").forEach(row => {
    row.onclick = () => {
      selectedMsg = parseInt(row.dataset.id);
      activeTab = "messages";
      document.querySelectorAll(".sidebar-tab").forEach(t =>
        t.classList.toggle("active", t.dataset.tab === "messages")
      );
      renderSidebar();
      renderMessageDetail(selectedMsg);
    };
  });
}

function renderMsgTable(msgs, db) {
  if (msgs.length === 0) return '<div style="color:var(--text2);font-size:13px;padding:8px;">None</div>';
  return `
    <table class="signal-table">
      <thead><tr><th>CAN ID</th><th>Name</th><th>DLC</th><th>Signals</th><th>Sender</th></tr></thead>
      <tbody>
        ${msgs.map(m => `
          <tr class="msg-link-row" data-id="${m.id}">
            <td><span class="sig-mono" style="color:var(--cyan)">${m.hex_id}</span></td>
            <td style="font-weight:600">${esc(m.name)}</td>
            <td>${m.dlc}</td>
            <td>${m.signals.length}</td>
            <td style="color:var(--text2)">${esc(m.sender)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

// ─── Value Table Detail ─────────────────────────────────────────────
function renderValueTableDetail(vtName) {
  const db = databases[activeFile];
  const panel = document.getElementById("detailPanel");
  const pairs = db.value_tables[vtName] || {};
  const sorted = Object.entries(pairs).sort((a, b) => parseInt(a[0]) - parseInt(b[0]));

  panel.innerHTML = `
    <div class="detail-header">
      <div class="detail-title">${esc(vtName)}</div>
      <div class="detail-subtitle">Value Table &middot; ${sorted.length} entries</div>
    </div>
    <table class="signal-table" style="max-width:500px;">
      <thead><tr><th>Value</th><th>Description</th></tr></thead>
      <tbody>
        ${sorted.map(([v, d]) =>
          `<tr><td class="sig-mono" style="color:var(--cyan)">${v}</td><td>${esc(d)}</td></tr>`
        ).join("")}
      </tbody>
    </table>
  `;
}

// ─── Bit Map Builder ────────────────────────────────────────────────
function buildBitMap(msg) {
  const totalBits = msg.dlc * 8;
  const bitMap = new Array(totalBits).fill(null);

  msg.signals.forEach((sig, idx) => {
    const bits = getSignalBits(sig, msg.dlc);
    bits.forEach(b => {
      if (b >= 0 && b < totalBits) {
        bitMap[b] = { signal: sig, colorIdx: idx % 12 };
      }
    });
  });
  return bitMap;
}

function getSignalBits(sig, dlc) {
  const bits = [];
  if (sig.byte_order === "little_endian") {
    // Intel byte order
    for (let i = 0; i < sig.bit_length; i++) {
      bits.push(sig.start_bit + i);
    }
  } else {
    // Motorola byte order
    let bit = sig.start_bit;
    for (let i = 0; i < sig.bit_length; i++) {
      bits.push(bit);
      const byteNum = Math.floor(bit / 8);
      const bitInByte = bit % 8;
      if (bitInByte === 0) {
        bit = (byteNum + 1) * 8 + 7;
      } else {
        bit = bit - 1;
      }
    }
  }
  return bits;
}

function renderBitLayout(bitMap, dlc) {
  let html = '<div class="bit-grid">';
  // Header row
  html += '<div class="bit-row">';
  html += '<div class="bit-cell bit-byte-label"></div>';
  for (let b = 7; b >= 0; b--) {
    html += `<div class="bit-cell bit-header">Bit ${b}</div>`;
  }
  html += '</div>';

  for (let byte = 0; byte < dlc; byte++) {
    html += '<div class="bit-row">';
    html += `<div class="bit-cell bit-byte-label">Byte ${byte}</div>`;
    for (let bit = 7; bit >= 0; bit--) {
      const idx = byte * 8 + bit;
      const entry = bitMap[idx];
      if (entry) {
        const abbrev = entry.signal.name.length > 4
          ? entry.signal.name.substring(0, 4)
          : entry.signal.name;
        html += `<div class="bit-cell bit-used sig-color-${entry.colorIdx}"
                      title="${entry.signal.name} [${entry.signal.start_bit}|${entry.signal.bit_length}]"
                      data-sig="${entry.signal.name}">${abbrev}</div>`;
      } else {
        html += `<div class="bit-cell bit-empty">${idx}</div>`;
      }
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

// ─── Send Type Lookup ───────────────────────────────────────────────
function getSendType(val, db) {
  const types = ["FixedPeriodic","Event","EnabledPeriodic","NotUsed","NotUsed","EventPeriodic","NotUsed","NotUsed","NoMsgSendType"];
  const idx = parseInt(val);
  return types[idx] || val;
}

// ─── Utility ────────────────────────────────────────────────────────
function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ─── Tooltip for bit cells ──────────────────────────────────────────
document.addEventListener("mouseover", (e) => {
  const cell = e.target.closest(".bit-used");
  if (cell) {
    const tip = document.getElementById("tooltip");
    tip.textContent = cell.title;
    tip.style.display = "block";
    const rect = cell.getBoundingClientRect();
    tip.style.left = rect.left + "px";
    tip.style.top = (rect.bottom + 6) + "px";
  }
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest(".bit-used")) {
    document.getElementById("tooltip").style.display = "none";
  }
});

init();
</script>
</body>
</html>"""


# ─── HTTP Server ──────────────────────────────────────────────────────────────

class DBCHandler(BaseHTTPRequestHandler):
    databases = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))

        elif path == "/api/databases":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.databases).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Quieter logging
        pass


def _expand_dbc_paths(paths):
    """Expand args into .dbc file paths. Directory args recurse for .dbc/.DBC files."""
    result = []
    for p in paths:
        if os.path.isdir(p):
            matched = glob.glob(os.path.join(p, "**", "*.dbc"), recursive=True)
            matched += glob.glob(os.path.join(p, "**", "*.DBC"), recursive=True)
            result.extend(sorted(set(matched)))
        else:
            result.append(p)
    return result


def main():
    parser = argparse.ArgumentParser(description="DBC Viewer - Web-based CAN database viewer")
    parser.add_argument("files", nargs="*", help="DBC files or folders to load (default: all .dbc in current dir)")
    parser.add_argument("--port", type=int, default=8087, help="HTTP port (default: 8087)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    # Find DBC files
    if args.files:
        dbc_files = _expand_dbc_paths(args.files)
    else:
        dbc_files = sorted(glob.glob("*.dbc")) + sorted(glob.glob("Archived/*.dbc"))

    if not dbc_files:
        print("No .dbc files found. Provide files as arguments or run from a directory with .dbc files.")
        sys.exit(1)

    # Parse all files
    databases = {}
    for filepath in dbc_files:
        filepath = os.path.abspath(filepath)
        print(f"  Parsing: {os.path.basename(filepath)}")
        try:
            db = parse_dbc(filepath)
            data = db_to_json(db)
            databases[db["filename"]] = data
            msg_count = len([m for m in db["messages"].values() if m["name"] != "VECTOR__INDEPENDENT_SIG_MSG"])
            sig_count = sum(len(m["signals"]) for m in db["messages"].values())
            print(f"    -> {msg_count} messages, {sig_count} signals, {len(db['nodes'])} nodes")
        except Exception as e:
            print(f"    -> Error: {e}")

    DBCHandler.databases = databases

    # Start server - auto-find free port if taken
    port = args.port
    server = None
    for attempt in range(20):
        try:
            server = HTTPServer(("127.0.0.1", port), DBCHandler)
            break
        except OSError:
            print(f"  Port {port} in use, trying {port + 1}...")
            port += 1
    if server is None:
        print("  Could not find a free port.")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  DBC Viewer running at: {url}")
    print(f"  Press Ctrl+C to stop\n")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
