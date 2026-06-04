#!/usr/bin/env bash
# Launcher for the Telegram MCP server, used by Hermes (`hermes mcp add`).
# A wrapper (rather than passing `uv run --directory …` as MCP args) keeps the
# nested --directory flag out of Hermes's own argument parser, and `uv run`
# sets the working directory so the server finds its .env (TELEGRAM_SESSION_STRING).
exec uv run --directory "${TELEGRAM_MCP_DIR:-$HOME/Documents/telegram-mcp}" telegram-mcp
