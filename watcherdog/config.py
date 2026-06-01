"""Configuration loading for WatcherDogBot.

Reads a simple KEY=VALUE `.env` file (no third-party deps) and exposes the
values as a typed Config object. Environment variables override file values.
"""

from __future__ import annotations

import os

# Severity ranking used everywhere for "only notify at or above" comparisons.
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _project_root():
    # config.py lives in <root>/watcherdog/, so the root is one level up.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_env_file(path):
    """Parse a minimal `.env` file into a dict. Ignores blank lines and
    lines starting with '#'. Strips matching surrounding quotes."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            values[key] = val
    return values


class Config:
    """Resolved runtime configuration."""

    def __init__(self, values):
        root = _project_root()

        def get(key, default=None):
            # env var wins over file value
            return os.environ.get(key, values.get(key, default))

        def resolve_path(p):
            return p if os.path.isabs(p) else os.path.normpath(os.path.join(root, p))

        self.root = root

        # --- Telegram ---
        self.telegram_bot_token = get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = get("TELEGRAM_CHAT_ID", "").strip()
        # Optional forum-topic id; leave blank for normal chats.
        self.telegram_thread_id = get("TELEGRAM_THREAD_ID", "").strip()

        # --- Ollama ---
        self.ollama_url = get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.ollama_model = get("OLLAMA_MODEL", "huihui_ai/gemma-4-abliterated:e4b")
        self.ollama_timeout = float(get("OLLAMA_TIMEOUT", "120"))

        # --- Monitoring ---
        self.log_dir = resolve_path(get("LOG_DIR", "logs"))
        self.db_path = resolve_path(get("DB_PATH", "data/incidents.db"))
        self.offsets_path = resolve_path(get("OFFSETS_PATH", "data/offsets.json"))
        self.log_glob = get("LOG_GLOB", "*.log")
        self.poll_interval = float(get("POLL_INTERVAL", "2.0"))
        # How long an in-progress traceback can sit with no new lines before we
        # flush it as a complete incident (handles tracebacks split across reads).
        self.flush_idle_seconds = float(get("FLUSH_IDLE_SECONDS", "5.0"))

        # --- Telegram user-client (MTProto) watcher ---
        # Reads the group as a USER account, so it can see messages from other
        # bots (which the Bot API forbids). Credentials from my.telegram.org.
        api_id = get("TELEGRAM_API_ID", "").strip()
        self.telegram_api_id = int(api_id) if api_id.isdigit() else 0
        self.telegram_api_hash = get("TELEGRAM_API_HASH", "").strip()
        self.telegram_session = resolve_path(get("TELEGRAM_SESSION", "data/watcher.session"))
        # Comma-separated chat IDs or @usernames to monitor. Empty = every chat
        # the account is in (not recommended — name the SinFermera group).
        raw_watch = get("WATCH_CHATS", "").strip()
        self.watch_chats = [c.strip() for c in raw_watch.split(",") if c.strip()]
        # Where alerts are delivered (via the bot). Falls back to TELEGRAM_CHAT_ID.
        self.alert_chat_id = get("ALERT_CHAT_ID", "").strip() or self.telegram_chat_id
        # How alerts are sent:
        #   "user" — as YOUR account (MTProto) to a real person (ALERT_USER)
        #   "bot"  — via the watchdog bot to ALERT_CHAT_ID
        self.alert_via = get("ALERT_VIA", "user").strip().lower()
        if self.alert_via not in ("user", "bot"):
            self.alert_via = "user"
        # Recipient when ALERT_VIA=user. A @username, phone, or numeric id.
        # "me" = your own Saved Messages. Blank also falls back to "me" so we
        # never message an unintended contact.
        self.alert_user = get("ALERT_USER", "").strip() or "me"
        # If True, also AI-classify messages that match neither a known-good nor a
        # known-error pattern (catches novel failures at the cost of more Ollama calls).
        self.analyze_unknown = get("ANALYZE_UNKNOWN", "true").strip().lower() in ("1", "true", "yes")

        # --- GUI (no-API) mode: drive the real Telegram app ---
        # Sidebar name of the chat to type alerts into. "Saved Messages" = yourself.
        self.gui_alert_chat = get("GUI_ALERT_CHAT", "Saved Messages")
        self.gui_poll_interval = float(get("GUI_POLL_INTERVAL", "120"))
        # How Telegram sends: "return" (Send by Enter) or "cmd_return" (Cmd+Enter).
        self.gui_send_key = get("GUI_SEND_KEY", "return").strip().lower()
        # Right-pane fraction for locating the message input on click fallback.
        self.gui_input_x_frac = float(get("GUI_INPUT_X_FRAC", "0.66"))
        # Scroll the chat list to read bots that aren't currently visible.
        self.gui_scroll_steps = int(get("GUI_SCROLL_STEPS", "6"))
        # Reading: "each_chat" opens every bot chat and reads the conversation
        # (thorough); "sidebar" only reads chat-list previews (fast).
        self.gui_read_mode = get("GUI_READ_MODE", "each_chat").strip().lower()
        self.gui_max_bots = int(get("GUI_MAX_BOTS", "24"))
        self.gui_chat_load_wait = float(get("GUI_CHAT_LOAD_WAIT", "1.3"))
        # Max scroll iterations when enumerating the chat list to the end.
        self.gui_scroll_max = int(get("GUI_SCROLL_MAX", "40"))
        # Regex identifying a bot chat by name.
        self.gui_bot_name_re = get("GUI_BOT_NAME_RE", r"sinf[ae]rmera\s*0*\d+")
        # The "all good" status line sent to the alert chat (deduplicated).
        self.gui_status_message = get(
            "GUI_STATUS_MESSAGE", "✅ WatcherDog: everything working perfectly."
        )
        # Human-like smoothing of mouse moves / scrolling / typing. Off = the
        # old instant, deterministic actions (faster, but more robotic).
        self.gui_smooth_input = get("GUI_SMOOTH_INPUT", "true").strip().lower() in ("1", "true", "yes")
        # Unread detection: only open/deep-read chats showing a numeric unread
        # badge, instead of re-reading every chat whose preview text changed.
        self.gui_unread_only = get("GUI_UNREAD_ONLY", "true").strip().lower() in ("1", "true", "yes")
        # Also fall back to preview-text-change detection (union) so a missed
        # OCR badge can't make us skip a genuinely new message.
        self.gui_unread_fallback = get("GUI_UNREAD_FALLBACK", "true").strip().lower() in ("1", "true", "yes")
        # A digits-only sidebar fragment counts as an unread badge only when its
        # horizontal position within the chat-list span is at/beyond this
        # fraction (badges sit at the far right of the row).
        self.gui_unread_x_frac = float(get("GUI_UNREAD_X_FRAC", "0.5"))

        # --- Hermes two-way conversation ---
        # When enabled, replies in the alert chat are answered by your local
        # Hermes agent (it chats back, with context).
        self.hermes_enabled = get("HERMES_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.hermes_bin = get("HERMES_BIN", "/Users/macmini4/.local/bin/hermes")
        self.hermes_session = get("HERMES_SESSION", "watcherdog")
        self.hermes_timeout = float(get("HERMES_TIMEOUT", "180"))
        # Open a visible Terminal window tailing the Hermes chat log, so you can
        # watch the agent answer live. The headless call still does the work.
        self.hermes_terminal = get("HERMES_TERMINAL", "true").strip().lower() in ("1", "true", "yes")
        self.hermes_chat_log = get(
            "HERMES_CHAT_LOG",
            os.path.join(os.path.dirname(self.db_path) or ".", "hermes_chat.log"),
        )
        # In the conversation pane, fragments whose center-x fraction is below
        # this are treated as INCOMING (a reply); above it are our own messages.
        self.gui_incoming_x_threshold = float(get("GUI_INCOMING_X_THRESHOLD", "0.66"))
        # Vertical gap (px) above which two OCR lines belong to different chat rows.
        self.gui_row_gap_px = float(get("GUI_ROW_GAP_PX", "26"))
        # SAFETY: when false (default), detect + log but never type/send. Flip to
        # true only once you've watched a dry run and trust what it flags.
        self.gui_send_enabled = get("GUI_SEND_ENABLED", "false").strip().lower() in ("1", "true", "yes")

        # --- Silence / heartbeat detection ---
        # Alert when a bot that normally reports goes quiet this long (minutes).
        self.silence_threshold = float(get("SILENCE_THRESHOLD_MINUTES", "30")) * 60.0
        # How often to scan for newly-silent bots (seconds).
        self.silence_check_interval = float(get("SILENCE_CHECK_INTERVAL_SECONDS", "60"))
        self.heartbeat_path = resolve_path(get("HEARTBEAT_PATH", "data/heartbeats.json"))
        # Optional comma-separated bot names you always expect to be alive even
        # before they post (e.g. SinFermera3,SinFermera10). Usually leave blank —
        # bots are auto-learned the first time they post.
        raw_expected = get("EXPECTED_BOTS", "").strip()
        self.expected_bots = [b.strip() for b in raw_expected.split(",") if b.strip()]
        self.silence_enabled = get("SILENCE_ENABLED", "true").strip().lower() in ("1", "true", "yes")

        # --- Alerting policy ---
        self.min_severity = get("MIN_SEVERITY", "high").strip().lower()
        if self.min_severity not in SEVERITY_ORDER:
            self.min_severity = "high"
        # Suppress repeats of the same normalized error within this many seconds.
        self.dedupe_window = float(get("DEDUPE_WINDOW", "300"))
        # If True, skip the Ollama call and just forward raw errors (faster, no AI).
        self.disable_ai = get("DISABLE_AI", "false").strip().lower() in ("1", "true", "yes")

    def validate(self):
        """Return a list of human-readable problems (empty if all good)."""
        problems = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set")
        elif ":" not in self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN does not look like a bot token (missing ':')")
        if not self.telegram_chat_id:
            problems.append("TELEGRAM_CHAT_ID is not set")
        return problems

    def validate_mtproto(self):
        """Problems specific to the user-client (MTProto) watcher."""
        problems = self.validate()  # still need the bot token/chat for alerting
        if not self.telegram_api_id:
            problems.append("TELEGRAM_API_ID is not set (get it from https://my.telegram.org)")
        if not self.telegram_api_hash:
            problems.append("TELEGRAM_API_HASH is not set (get it from https://my.telegram.org)")
        return problems


def load_config(env_path=None):
    """Load configuration from the project's `.env` file (or a given path)."""
    if env_path is None:
        env_path = os.path.join(_project_root(), ".env")
    return Config(_parse_env_file(env_path))
