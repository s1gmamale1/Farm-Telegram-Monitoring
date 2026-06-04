#!/usr/bin/env bash
# One-time (idempotent) wiring of the Telegram MCP server into Hermes, so the
# Hermes agent can read Telegram AND drive the farm panels. Safe to re-run.
#
#   ./scripts/setup_hermes.sh
#
# It (1) registers the telegram MCP server (via a launcher wrapper that keeps the
# nested --directory flag out of Hermes's arg parser), and (2) restricts Hermes
# to a curated whitelist of telegram tools by writing
# `mcp_servers.telegram.tools.include` in ~/.hermes/config.yaml.
#
# The whitelist is reads + the panel-action tools the skills need (skills 0/3/4
# drive panels via /start + inline buttons; skill 6 sends stickers): send_message,
# list_inline_buttons, press_inline_button, download_media, send_sticker,
# get_sticker_sets. Everything else (delete/ban/admin/folder edits/…) stays off.
set -euo pipefail

HERMES="${HERMES_BIN:-$HOME/.local/bin/hermes}"
TELEGRAM_MCP_DIR="${TELEGRAM_MCP_DIR:-$HOME/Documents/telegram-mcp}"
HERMES_CONFIG="${HERMES_CONFIG:-$HOME/.hermes/config.yaml}"
SERVER_NAME="telegram"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/telegram_mcp_launch.sh"

# A python with PyYAML (Hermes's own venv has it); fall back to system python3.
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Tools we expose to Hermes: reads + the panel-action/sticker tools the skills need.
WHITELIST_TOOLS='["list_folders","get_folder","list_chats","get_chat","get_history","list_messages","get_messages","get_message_context","get_pinned_messages","search_messages","search_contacts","list_contacts","get_me","mark_as_read","send_message","list_inline_buttons","press_inline_button","download_media","send_sticker","get_sticker_sets"]'

echo "==> Hermes: $HERMES"
echo "==> Telegram MCP dir: $TELEGRAM_MCP_DIR"
chmod +x "$LAUNCHER"

if "$HERMES" mcp list 2>/dev/null | grep -qiE "^[[:space:]]*${SERVER_NAME}[[:space:]]"; then
  echo "==> MCP server '${SERVER_NAME}' already registered."
else
  echo "==> Registering MCP server '${SERVER_NAME}'…"
  # `hermes mcp add` prompts "Enable all tools?"; answer Y to save the server.
  # We immediately tighten the selection to the whitelist below.
  printf 'Y\n' | TELEGRAM_MCP_DIR="$TELEGRAM_MCP_DIR" "$HERMES" mcp add "$SERVER_NAME" \
    --command "$LAUNCHER" \
    --env "TELEGRAM_MCP_DIR=$TELEGRAM_MCP_DIR" >/dev/null
fi

echo "==> Restricting '${SERVER_NAME}' to the whitelisted tools…"
HERMES_CONFIG="$HERMES_CONFIG" WHITELIST_TOOLS="$WHITELIST_TOOLS" SERVER_NAME="$SERVER_NAME" "$PY" - <<'PYEOF'
import json, os, yaml
path = os.environ["HERMES_CONFIG"]
name = os.environ["SERVER_NAME"]
tools = json.loads(os.environ["WHITELIST_TOOLS"])
with open(path) as fh:
    cfg = yaml.safe_load(fh) or {}
srv = cfg.setdefault("mcp_servers", {}).setdefault(name, {})
srv["enabled"] = True
srv["tools"] = {"include": tools}   # whitelist: only these; everything else off
with open(path, "w") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)
print(f"   include = {tools}")
PYEOF

echo "==> Verifying…"
"$HERMES" mcp test "$SERVER_NAME" >/dev/null 2>&1 && echo "   mcp test: OK" || echo "   mcp test: FAILED (check uv + $TELEGRAM_MCP_DIR/.env)"
"$HERMES" tools list 2>/dev/null | grep -i "$SERVER_NAME" || true
echo "==> Done."
