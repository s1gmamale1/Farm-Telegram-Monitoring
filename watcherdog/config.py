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


def _hermes_env_value(name):
    """Look up a single KEY from ~/.hermes/.env (where Hermes stores provider
    keys like OPENROUTER_API_KEY). Returns "" if absent/unreadable."""
    path = os.path.expanduser("~/.hermes/.env")
    return _parse_env_file(path).get(name, "")


class Config:
    """Resolved runtime configuration."""

    def __init__(self, values):
        def get(key, default=None):
            # env var wins over file value
            return os.environ.get(key, values.get(key, default))

        # ROOT lets tests (and embedders) point config at a sandbox dir; the
        # project-root path resolution below honours it. Falls back to the repo.
        root = get("ROOT") or _project_root()

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
        # Optional: reuse an already-authorized session STRING instead of a file
        # session (avoids a separate tools/tg_login.py). If blank and no file
        # session exists, the watcher falls back to the telegram-mcp's session.
        self.telegram_session_string = get("TELEGRAM_SESSION_STRING", "").strip()
        self.telegram_mcp_dir = os.path.expanduser(get("TELEGRAM_MCP_DIR", "~/Documents/telegram-mcp"))
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

        # --- MCP / MTProto watcher mode (current default; replaces GUI mode) ---
        # Runs as a USER account (Telethon) and proactively watches one folder of
        # bots, alerting the ibo chat; ibo's messages are answered by Hermes
        # (which has the Telegram MCP read-tools). See run_watcher.py.
        # The dialog FOLDER (filter) whose chats are monitored. Default "Farms"
        # holds the 24 SinFermera bots. Matched by name; WATCH_FOLDER_ID (a
        # numeric filter id, 0 = ignore) takes precedence when set.
        self.watch_folder = get("WATCH_FOLDER", "Farms").strip()
        wf_id = get("WATCH_FOLDER_ID", "").strip()
        self.watch_folder_id = int(wf_id) if wf_id.lstrip("-").isdigit() else 0
        # The chat(s) that receive proactive alerts AND whose messages are routed
        # to Hermes. A COMMA-SEPARATED allow-list of refs, each a numeric user id
        # (e.g. 1406109190) or an @username. The watcher RESPONDS to any user in
        # the list (in their own chat) and DMs proactive ALERTS to ALL of them.
        # Preferred key: ALLOWLIST (aliases ALLOW_LIST / ALLOWED_USERS); the legacy
        # IBO_CHAT_ID still works as a fallback. First non-empty wins.
        # `ibo_chat_id` keeps the primary (first) ref so single-recipient code and
        # a single configured value behave exactly as before.
        raw_ibo = next((v for v in (
            get("ALLOWLIST", "").strip(),
            get("ALLOW_LIST", "").strip(),
            get("ALLOWED_USERS", "").strip(),
            get("IBO_CHAT_ID", "").strip(),
        ) if v), "")
        # Clean each ref of surrounding whitespace and any JSON-array/quote
        # wrapping (so ALLOWLIST=[111, "222"] yields 111, 222 — not "[111"/'222"').
        # The trailing .strip() removes whitespace that bracket/quote removal
        # exposes (e.g. "[ 111 , 222 ]" -> "111","222", not " 111"/"222 " which
        # would fail numeric-id resolution). `-`/`@` survive (channel ids/usernames).
        self.ibo_chat_ids = [
            c for c in (u.strip().strip("[](){}\"'").strip() for u in raw_ibo.split(",")) if c
        ]
        self.ibo_chat_id = self.ibo_chat_ids[0] if self.ibo_chat_ids else ""
        # Seconds between proactive monitor sweeps of the watch folder.
        self.watch_poll_interval = float(get("WATCH_POLL_INTERVAL", "120"))
        # After reading a chat (monitor sweep or agent), acknowledge it as read
        # so its unread badge clears — reading via the API does NOT do this on its
        # own. Set false to leave unread markers untouched.
        self.mark_read_after_read = get("MARK_READ_AFTER_READ", "true").strip().lower() in ("1", "true", "yes")

        # --- Hermes panels & skills (docs/hermes/skills/) ---
        # Folder holding the panel control bots; the number in a chat's display
        # name is its Panel# (skill 0). Panels are driven via /start + inline
        # buttons.
        self.panels_folder = get("PANELS_FOLDER", "Panels").strip()
        # Always keep exactly this many launched accounts per panel (skill 4).
        self.accounts_per_panel = int(get("ACCOUNTS_PER_PANEL", "4"))
        # Chance (0..1) of sending a random sticker after a text reply (skill 6).
        self.sticker_chance = min(1.0, max(0.0, float(get("STICKER_CHANCE", "0.25"))))

        # --- Weekly drop stats -> Google Sheets (skill 5) ---
        # Wednesday 00:00: stop farms, pull Drops Stats per panel, buffer one row
        # per panel to DROP_STATS_DIR/<YYYY-Www>.json, then push to a sheet via
        # watcherdog/drop_sheets.py. See docs/hermes/skills/05-drop-stats.md.
        self.drop_stats_dir = resolve_path(get("DROP_STATS_DIR", "data/hermes/drop_stats"))
        # Google Sheets sink. drop_sheets.py reads these from the ENVIRONMENT; the
        # weekly job bridges them across before calling append_week(). The creds
        # path is resolved under the project root so a relative .env value works
        # regardless of the process's CWD (drop_sheets checks os.path.exists).
        creds = get("GSHEETS_CREDENTIALS", "").strip()
        self.gsheets_credentials = resolve_path(creds) if creds else ""
        self.gsheets_sheet_id = get("GSHEETS_SHEET_ID", "").strip()
        self.gsheets_tab = get("GSHEETS_TAB", "DropStats").strip() or "DropStats"

        # --- Recurring-error watchdog ---
        # Every RECURRING_ERROR_INTERVAL seconds, check the incident store: if the
        # SAME error (identical hash) has fired at least MIN_COUNT times within the
        # trailing WINDOW, alert ibo that it keeps happening. COOLDOWN suppresses
        # re-alerting the same recurring error for that many seconds.
        self.recurring_error_enabled = get("RECURRING_ERROR_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.recurring_error_interval = float(get("RECURRING_ERROR_INTERVAL", "900"))   # 15 min
        self.recurring_error_window = float(get("RECURRING_ERROR_WINDOW", "3600"))      # last 1 h
        self.recurring_error_min_count = max(2, int(get("RECURRING_ERROR_MIN_COUNT", "3")))
        self.recurring_error_cooldown = float(get("RECURRING_ERROR_COOLDOWN", "3600"))  # 1 h

        # Incident lifecycle tracking: follow up on an open issue until it
        # resolves (self-heals or we fix it) or is escalated after a give-up
        # window. See docs/superpowers/specs/2026-06-09-incident-lifecycle-tracking-design.md
        self.incident_tracking_enabled = get("INCIDENT_TRACKING_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.incident_followup_interval = float(get("INCIDENT_FOLLOWUP_INTERVAL", "900"))   # 15 min: nag + re-attempt tick
        self.incident_giveup_seconds = float(get("INCIDENT_GIVEUP_MINUTES", "60")) * 60.0   # escalate & stop nagging after this
        self.incident_max_fix_retries = max(0, int(get("INCIDENT_MAX_FIX_RETRIES", "2")))   # known-fix re-attempts before give-up

        # --- Special Forces group (@-mention auto-reply) ---
        # When this account is @-mentioned in the SPECIAL_FORCES_CHAT group, the
        # mention is handed to the agent and its answer is posted back IN the group
        # (read-only: the agent runs with actions disabled so untrusted group text
        # can never trigger a real action). Match by group title, @username, or id.
        # OPT-IN (default off): posting in a shared group AS the owner is surprising
        # to enable implicitly, so it must be turned on explicitly with
        # SPECIAL_FORCES_ENABLED=true.
        self.special_forces_enabled = get("SPECIAL_FORCES_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        self.special_forces_chat = get("SPECIAL_FORCES_CHAT", "Special Forces").strip()

        # --- Auto weekly digest (read-only report pushed to ibo) ---
        # A /weekly-style summary the agent compiles from the Farms folder and
        # sends to ibo on WEEKDAY at HOUR (local). weekday 6 = Sunday. This does
        # NOT stop farms — that's the separate Wednesday drop-stats job (skill 5).
        self.weekly_digest_enabled = get("WEEKLY_DIGEST_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.weekly_digest_weekday = int(get("WEEKLY_DIGEST_WEEKDAY", "6"))   # Sunday
        self.weekly_digest_hour = int(get("WEEKLY_DIGEST_HOUR", "18"))        # 18:00

        # --- Auto hourly report (pushed to a forum topic or chat) ---
        self.hourly_report_enabled = get("HOURLY_REPORT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.hourly_report_topic = get("HOURLY_REPORT_TOPIC", "").strip()
        raw_hourly_chat = get("HOURLY_REPORT_CHAT", "").strip()
        # Target precedence (first truthy wins): explicit HOURLY_REPORT_CHAT >
        # legacy TELEGRAM_CHAT_ID > allow-list primary (IBO_CHAT_ID/ALLOWLIST).
        # Without the allow-list fallback an alerts-only deploy left this "" and
        # run_hourly_report errored on get_entity("") every hour.
        self.hourly_report_chat = raw_hourly_chat or self.telegram_chat_id or self.ibo_chat_id
        # A bot counts as "quiet" in the hourly report when its last message is
        # older than this many minutes (was 30; now 60 = 1 hour).
        self.quiet_threshold_minutes = float(get("QUIET_THRESHOLD_MINUTES", "60"))

        # --- Self-contained conversation agent (answers ibo) ---
        # ibo's messages are answered by a small READ-ONLY tool-calling loop over
        # an OpenAI-compatible chat API (OpenRouter by default — the same model
        # Hermes uses). It reads Telegram through the watcher's own connection.
        self.agent_model = get("AGENT_MODEL", "deepseek/deepseek-v4-pro")
        self.agent_api_base = get("AGENT_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
        # Tool-call rounds per turn. The skill-2 action loop chains several calls
        # (lookup_fix -> screenshot -> read -> press button(s) -> log/report), so
        # this needs headroom beyond a read-only Q&A. If the budget is spent the
        # agent makes one final tool-less pass to answer from what it gathered
        # (agent.answer), so a long turn still replies instead of erroring.
        self.agent_max_steps = int(get("AGENT_MAX_STEPS", "12"))
        self.agent_timeout = float(get("AGENT_TIMEOUT", "120"))
        # History turns (user+assistant pairs) kept for context per ibo session.
        self.agent_history_turns = int(get("AGENT_HISTORY_TURNS", "8"))
        # API key: explicit AGENT_API_KEY wins, else OPENROUTER_API_KEY from the
        # environment, else from ~/.hermes/.env (where Hermes keeps it).
        agent_key = (get("AGENT_API_KEY", "").strip()
                     or os.environ.get("OPENROUTER_API_KEY", "").strip()
                     or _hermes_env_value("OPENROUTER_API_KEY").strip())
        self.agent_api_key = agent_key
        # Where the live ibo conversation is mirrored (tail-able).
        self.agent_chat_log = get(
            "AGENT_CHAT_LOG",
            os.path.join(os.path.dirname(self.db_path) or ".", "agent_chat.log"))

        # --- GUI (no-API) mode: drive the real Telegram app [LEGACY] ---
        # Superseded by the MCP/MTProto watcher above. Kept for reference only;
        # run_gui.py is no longer the supported entrypoint.
        # Sidebar name of the chat to type alerts into. "Saved Messages" = yourself.
        self.gui_alert_chat = get("GUI_ALERT_CHAT", "Saved Messages")
        self.gui_poll_interval = float(get("GUI_POLL_INTERVAL", "120"))
        # Between the (slow) 24-bot sweeps we keep polling JUST the alert chat on
        # this much shorter interval, so a message you text in ibo is read and
        # handed to Hermes within seconds instead of waiting for the next sweep.
        self.gui_reply_poll_interval = max(
            1.0, float(get("GUI_REPLY_POLL_INTERVAL", "5")))
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
        # Regex identifying a bot chat by name (1-2 digits — bots are 1..24 —
        # so an OCR smear like "221" resolves to 22, not a phantom bot).
        self.gui_bot_name_re = get("GUI_BOT_NAME_RE", r"sinf[ae]rmera\s*0*\d{1,2}")
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
        # Ignore messages older than this many minutes (stale → not worth acting on).
        self.gui_max_age_minutes = float(get("GUI_MAX_AGE_MINUTES", "120"))
        # Type replies character-by-character (human cadence) instead of pasting.
        self.gui_human_typing = get("GUI_HUMAN_TYPING", "true").strip().lower() in ("1", "true", "yes")
        # Global hotkey (key code) to pause/resume all GUI work. 109 = F10.
        self.gui_pause_keycode = int(get("GUI_PAUSE_KEYCODE", "109"))
        # File the activity log is mirrored to (so you can `tail -f` it even when
        # running in the foreground). Lives next to the DB by default.
        self.gui_run_log = get(
            "GUI_RUN_LOG",
            os.path.join(os.path.dirname(self.db_path) or ".", "gui_run.log"),
        )
        # On startup, auto-open a stacked column of Terminal windows down the
        # RIGHT side of the screen, each tailing one log (activity + Hermes chat).
        self.gui_monitor_terminals = get(
            "GUI_MONITOR_TERMINALS", "true").strip().lower() in ("1", "true", "yes")

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
        # A command prefix you can type from ANY account — even the same one
        # WatcherDog watches from — to ask Hermes a question it will reliably
        # answer. Same-account replies render as OUTGOING, so the left/right
        # x-heuristic alone can never see them; a prefixed message is matched
        # regardless of which side it lands on. Blank disables the prefix path.
        self.gui_command_prefix = get("GUI_COMMAND_PREFIX", "!dog").strip()
        # Vertical gap (px) above which two OCR lines belong to different chat rows.
        self.gui_row_gap_px = float(get("GUI_ROW_GAP_PX", "26"))
        # When a chat is open its name also shows in the conversation HEADER at
        # the very top — and clicking that title opens the profile panel instead
        # of the chat. Ignore name matches in the top this-fraction of the window
        # (the header band; the chat list's first row is always below it).
        self.gui_header_band_frac = float(get("GUI_HEADER_BAND_FRAC", "0.10"))
        # SAFETY: when false (default), detect + log but never type/send. Flip to
        # true only once you've watched a dry run and trust what it flags.
        self.gui_send_enabled = get("GUI_SEND_ENABLED", "false").strip().lower() in ("1", "true", "yes")

        # --- Silence / heartbeat detection ---
        # Alert when a bot that normally reports goes quiet this long (minutes).
        self.silence_threshold = float(get("SILENCE_THRESHOLD_MINUTES", "120")) * 60.0
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
        # The deterministic core is the runtime default: no model on the hot path.
        # When True (default), skip all model calls — deterministic routing, scripted
        # panel actions, command handlers, screenshots, reports, and alerts still work.
        # The model path is opt-in via DISABLE_AI=false.
        self.disable_ai = get("DISABLE_AI", "true").strip().lower() in ("1", "true", "yes")
        if self.disable_ai:
            self.analyze_unknown = False
            self.hermes_enabled = False

        # --- Hermes skills (skill 2: error handling) ---
        # The learned-fixes "brain" Hermes reads + appends (docs/hermes/skills/
        # 02-error-handling.md), and the AI-fix log it reports at end of day —
        # or immediately on startup if a crash/reboot left it non-empty.
        self.learned_fixes_path = resolve_path(
            get("LEARNED_FIXES_PATH", "data/hermes/learned_fixes.md"))
        self.daily_errors_path = resolve_path(
            get("DAILY_ERRORS_PATH", "data/hermes/daily_errors.jsonl"))
        # Local time (HH:MM) to send ibo the end-of-day AI-fix summary, after
        # which the log is cleared. Default just before midnight.
        self.daily_report_time = get("DAILY_REPORT_TIME", "23:59").strip()
        # When true, the conversation agent may DRIVE panels (send /start, press
        # inline buttons, screenshot) and apply fixes — not just read. Destructive
        # buttons (Kill/Restart/Reboot/Shutdown) still need an explicit ibo "yes".
        # Set false to keep the agent strictly read-only.
        self.agent_actions_enabled = get(
            "AGENT_ACTIONS_ENABLED", "true").strip().lower() in ("1", "true", "yes")

        # --- Deterministic panel monitoring & recovery (panel_rules.py) -------
        self.panel_rules_enabled = get(
            "PANEL_RULES_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.panel_target_accounts = int(get("PANEL_TARGET_ACCOUNTS", "4"))
        self.panel_overlaunch_minutes = float(get("PANEL_OVERLAUNCH_MINUTES", "15"))
        self.panel_idle_minutes = float(get("PANEL_IDLE_MINUTES", "10"))
        # A panel counts as DEAD only after TOTAL silence this long. ANY message
        # — incl. "Can't find match… changing batch" (working, just no games) —
        # resets the clock. 70m per the owner's spec (see Monitoring and Recovery
        # Rules.md → Alive vs dead). Pure timing; no screenshot/OCR/model needed.
        self.panel_stale_minutes = float(get("PANEL_STALE_MINUTES", "70"))
        self.panel_action_debounce_seconds = float(get("PANEL_ACTION_DEBOUNCE_SECONDS", "180"))
        self.panel_auto_recover = get(
            "PANEL_AUTO_RECOVER", "true").strip().lower() in ("1", "true", "yes")
        self.panel_auto_destructive = get(
            "PANEL_AUTO_DESTRUCTIVE", "true").strip().lower() in ("1", "true", "yes")
        self.panel_settle_seconds = float(get("PANEL_SETTLE_SECONDS", "4"))
        # After this many failed recovery attempts in one episode, stop the futile
        # Kill→Start loop and escalate the panel as a cold case (needs the per-PC
        # tool — a frozen RDP host can't be fixed from Telegram).
        self.panel_max_attempts = int(get("PANEL_MAX_ATTEMPTS", "3"))
        # R6 liveness probe: before declaring a silent panel DEAD, /start it and
        # wait this long for a reply. A reply proves the panel/PC is alive (silence
        # alone is not death — an idle panel answers /start instantly), so it is
        # NOT flagged. Set PANEL_PROBE_ENABLED=false to skip the probe (pure-timing
        # R6, the old behaviour). Non-destructive: /start only opens the menu.
        self.panel_probe_enabled = get(
            "PANEL_PROBE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        self.panel_probe_timeout = float(get("PANEL_PROBE_TIMEOUT", "15"))

        # --- Bot interface (the TALKING front-end; bot_interface.py) ----------
        # WatcherDog answers people through a Telegram BOT (logged in over MTProto
        # on the same event loop as the user account). The bot is what humans talk
        # to: it replies to slash-commands and questions in a group (and, if
        # allowed, DMs) — read-only, backed by the USER account's read tools. The
        # user account itself never speaks in groups; it only reads/manages the
        # SinFermera bots (which the Bot API is forbidden from reading).
        #
        # Split of responsibilities (who does what):
        #   • BOT  (Bot API)  — talk to humans: commands, Q&A, proactive alert DMs.
        #   • USER (MTProto)  — read the farm bots, sweep the folder, drive panels
        #                       and press inline buttons (a bot legally cannot).
        self.bot_enabled = get("BOT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        # The bot token. Reuses TELEGRAM_BOT_TOKEN (the project's bot slot).
        self.bot_token = self.telegram_bot_token
        # Bot's own MTProto session (separate from the user account's).
        self.bot_session = resolve_path(get("BOT_SESSION", "data/bot.session"))
        # Group(s) the bot answers in — comma-separated chat ids or @usernames.
        # Defaults to the Special Forces group when blank (the bot lives there).
        raw_bot_groups = get("BOT_GROUPS", "").strip()
        self.bot_groups = [g.strip() for g in raw_bot_groups.split(",") if g.strip()]
        # Restrict the bot to a single forum TOPIC: it only reacts to, and only
        # posts in, this topic — never any other topic of the group. Blank = no
        # restriction. Defaults to the hourly-report topic (Class A Farming).
        self.bot_topic = get("BOT_TOPIC", "").strip() or self.hourly_report_topic
        # Also answer private DMs to the bot (read-only Q&A from anyone).
        self.bot_answer_dms = get("BOT_ANSWER_DMS", "true").strip().lower() in ("1", "true", "yes")
        # Deliver proactive alerts (errors / silence / recovery / recurring /
        # daily / weekly digest) by having the BOT DM the owner, instead of the
        # user account DMing them. Falls back to the user account if the bot send
        # fails (e.g. the owner never pressed Start on the bot).
        self.bot_alerts = get("BOT_ALERTS", "true").strip().lower() in ("1", "true", "yes")
        # Numeric user id the bot DMs alerts to. Blank = resolve IBO_CHAT_ID via
        # the user account at startup. NOTE: a bot can only DM a user who has
        # pressed Start on it at least once.
        self.bot_alert_user_id = get("BOT_ALERT_USER_ID", "").strip()
        # Install WatcherDog's command menu on the bot at startup (BotFather
        # setMyCommands). Set false to leave the bot's existing menu untouched.
        self.bot_set_commands = get("BOT_SET_COMMANDS", "true").strip().lower() in ("1", "true", "yes")
        # Let the bot DRIVE panels (press buttons / send commands), not just read.
        # SAFETY: only the users in BOT_ACTION_USERS can trigger actions — the bot
        # lives in an untrusted group, so it must never act on a random member's
        # (or another bot's) message. Destructive buttons still follow the action
        # skill's confirm rules. Requires AGENT_ACTIONS_ENABLED too.
        self.bot_actions_enabled = get("BOT_ACTIONS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        # Comma-separated user ids / @usernames allowed to make the bot ACT.
        # Blank = the owner (IBO_CHAT_ID) plus the watcher's own user account.
        raw_action_users = get("BOT_ACTION_USERS", "").strip()
        self.bot_action_users = [u.strip() for u in raw_action_users.split(",") if u.strip()]
        # ADMINS may tell the bot (in natural language) to grant/revoke other
        # users' action access — the agent does it itself via grant_bot_access.
        # Blank = the owner (IBO_CHAT_ID) plus the watcher's own user account.
        raw_admins = get("BOT_ADMIN_USERS", "").strip()
        self.bot_admin_users = [u.strip() for u in raw_admins.split(",") if u.strip()]
        # Where the agent-granted access list is persisted (survives restarts).
        self.bot_access_path = resolve_path(get("BOT_ACCESS_PATH", "data/bot_access.json"))
        # Let an admin tell the bot to CHANGE WATCHERDOG'S OWN FILES (read/edit/
        # write within the project root; every write is backed up). Powerful and
        # risky — off by default; admin-gated when on. A restart applies code
        # changes.
        self.bot_self_edit_enabled = get("BOT_SELF_EDIT_ENABLED", "false").strip().lower() in ("1", "true", "yes")
        # Let the bot RESTART itself after a self-edit so code changes take effect
        # without a manual relaunch. Safe: it validates the whole project imports
        # first (rolling back the change if not), and a detached supervisor rolls
        # back + relaunches if the new code fails to come up healthy.
        self.bot_self_restart_enabled = get("BOT_SELF_RESTART_ENABLED", "true").strip().lower() in ("1", "true", "yes")
        _data_dir = os.path.dirname(self.db_path) or "."
        # Journal of pending self-edits (path + backup) so a failed restart can be
        # rolled back; and a health beacon the supervisor watches for after relaunch.
        self.self_edits_path = os.path.join(_data_dir, "self_edits.json")
        self.watcher_health_path = os.path.join(_data_dir, "watcher_healthy")
        # Where in-progress action tasks are persisted so the bot can RESUME them
        # after a restart. Each task is resumed at most BOT_TASK_MAX_RESUMES times
        # (so a task that keeps crashing can't loop forever).
        self.bot_task_path = resolve_path(get("BOT_TASK_PATH", "data/bot_tasks.json"))
        self.bot_task_max_resumes = max(0, int(get("BOT_TASK_MAX_RESUMES", "2")))
        # Live "working on it…" status message that edits as the task progresses,
        # then is deleted and replaced by the final answer.
        self.bot_progress_status = get("BOT_PROGRESS_STATUS", "true").strip().lower() in ("1", "true", "yes")
        # Multitasking: how many bot turns may run AT ONCE. Read-only Q&A/status
        # runs concurrently up to this cap so the bot never freezes while busy;
        # panel-driving ACTION turns still take turns among themselves (one acts
        # at a time) so they can't clash on the shared account.
        self.bot_max_concurrent = max(1, int(get("BOT_MAX_CONCURRENT", "3")))
        # How many bots the dispatch_bots fan-out works on AT ONCE (parallel sub-
        # agents). Different bots run simultaneously; same bot is serialized.
        self.fanout_concurrency = max(1, int(get("FANOUT_CONCURRENCY", "4")))

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


    def validate_watcher(self):
        """Problems specific to the MCP/MTProto watcher (run_watcher.py).

        The watcher sends as the user account (Telethon), so it needs only the
        API credentials and a target chat — not the bot token.
        """
        problems = []
        if not self.telegram_api_id:
            problems.append("TELEGRAM_API_ID is not set (get it from https://my.telegram.org)")
        if not self.telegram_api_hash:
            problems.append("TELEGRAM_API_HASH is not set (get it from https://my.telegram.org)")
        if not self.ibo_chat_ids:
            problems.append("ALLOWLIST is not set (comma-separated users that get "
                            "alerts / talk to Hermes; legacy key IBO_CHAT_ID also works)")
        return problems


def load_config(env_path=None):
    """Load configuration from the project's `.env` file (or a given path)."""
    if env_path is None:
        env_path = os.path.join(_project_root(), ".env")
    return Config(_parse_env_file(env_path))
