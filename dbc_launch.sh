#!/bin/bash
# ──────────────────────────────────────────────────────────
#  DBC Viewer Launcher (tmux)
#  Run from any terminal - handles everything:
#    Left:  your shell
#    Right: DBC TUI viewer
#
#  Usage:  ./dbc_launch.sh
# ──────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"
FILES="${@}"

if [ -z "$FILES" ]; then
    TUI_CMD="cd '$DIR' && python3 dbc_tui.py"
else
    TUI_CMD="cd '$DIR' && python3 dbc_tui.py $FILES"
fi

if [ -n "$TMUX" ]; then
    # Already in tmux - just split
    tmux split-window -h "$TUI_CMD"
    tmux select-pane -L
else
    # Not in tmux - start tmux, split, and land in the shell
    tmux new-session -d -s dbc -c "$PWD" \; \
        set mouse on \; \
        set status-style "bg=black,fg=white" \; \
        set status-left " DBC Viewer " \; \
        set status-left-style "bg=white,fg=black,bold" \; \
        set status-right " q: close TUI " \; \
        set status-right-style "bg=black,fg=yellow" \; \
        set pane-border-style "fg=colour240" \; \
        set pane-active-border-style "fg=white" \; \
        split-window -h "$TUI_CMD" \; \
        select-pane -L \; \
        attach
fi
