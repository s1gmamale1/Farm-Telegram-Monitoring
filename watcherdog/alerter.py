"""Send alerts to Telegram via the Bot API directly (stdlib urllib).

Sending is intentionally self-contained: a watchdog must be able to warn you
even when other services (Hermes, the gateway, etc.) are down. We send as
plain text with no parse_mode so message content never trips MarkdownV2/HTML
escaping rules.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger("watcherdog.alerter")

_SEVERITY_EMOJI = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🟠",
    "critical": "🔴",
}


def format_alert(bot_name, severity, analysis, raw_excerpt):
    emoji = _SEVERITY_EMOJI.get(severity, "⚠️")
    lines = [
        f"{emoji} Bot Error Detected — {severity.upper()}",
        "",
        f"Bot: {bot_name}",
    ]
    summary = (analysis or {}).get("summary")
    root_cause = (analysis or {}).get("root_cause")
    fix = (analysis or {}).get("fix")
    if summary:
        lines += ["", f"Summary:\n{summary}"]
    if root_cause:
        lines += ["", f"Root cause:\n{root_cause}"]
    if fix:
        lines += ["", f"Suggested fix:\n{fix}"]
    excerpt = (raw_excerpt or "").strip()
    if excerpt:
        # Keep the alert well under Telegram's 4096-char limit.
        if len(excerpt) > 1200:
            excerpt = excerpt[-1200:]
            excerpt = "…" + excerpt
        lines += ["", "Error excerpt:", excerpt]
    msg = "\n".join(lines)
    # Final safety net: summary/root_cause/fix are uncapped LLM text; an over-limit
    # message raises MessageTooLong and the alert is lost. Keep the header + as much
    # body as fits, well under Telegram's 4096.
    LIMIT = 4000
    if len(msg) > LIMIT:
        msg = msg[:LIMIT - 1].rstrip() + "…"
    return msg


def format_novel_alert(bot_name, severity, analysis, raw_excerpt, recovery):
    """``format_alert`` plus one deterministic-recovery line (Phase 4).

    ``recovery`` is the ``novel_recovery.attempt`` outcome; ``skipped`` renders
    the plain alert unchanged."""
    base = format_alert(bot_name, severity, analysis, raw_excerpt)
    status = (recovery or {}).get("status")
    if status == "attempted":
        line = ("🛠 Novel error — ran the generic restart "
                "(kill all → relaunch); will verify next sweep.")
    elif status == "failed":
        step = (recovery or {}).get("failed_step", "?")
        line = f"🛠 Novel error — generic restart FAILED at '{step}'. Needs you."
    elif status == "human_needed":
        line = "🚫 Novel error in the ban/captcha class — not auto-restarting. Needs you."
    else:
        return base
    msg = f"{base}\n\n{line}"
    if len(msg) > 4000:
        # Trim the BASE, never the recovery line — it's the actionable part.
        base = base[:4000 - len(line) - 3].rstrip() + "…"
        msg = f"{base}\n\n{line}"
    return msg


def format_alert_oneline(bot_name, severity, analysis):
    """Single-line alert for GUI typing (newlines would send the message early)."""
    emoji = _SEVERITY_EMOJI.get(severity, "⚠️")
    a = analysis or {}
    parts = [f"{emoji} WatcherDog [{severity.upper()}] {bot_name}"]
    if a.get("summary"):
        parts.append(f"— {a['summary']}")
    if a.get("fix"):
        parts.append(f"| Fix: {a['fix']}")
    line = " ".join(parts)
    line = " ".join(line.split())  # collapse any stray newlines/whitespace
    return line[:900]


def format_silence_oneline(bot_name, silent_seconds):
    return f"🔕 WatcherDog: {bot_name} has gone SILENT for {_fmt_duration(silent_seconds)} — it may be down/banned."


def format_recovery_oneline(bot_name):
    return f"✅ WatcherDog: {bot_name} is back online."


class UserClientAlerter:
    """Sends alerts AS your own account (MTProto) to a real person.

    Presents the same interface as TelegramAlerter (.send / .send_alert) so the
    processing code doesn't care which sink is used. The send coroutine is
    scheduled onto the running event loop via run_coroutine_threadsafe, so this
    is safe to call from the worker thread where message processing happens.

    The `client` is duck-typed (any object with an async send_message(target,
    text)) — kept telethon-free so log-file mode stays dependency-free.
    """

    def __init__(self, client, loop, target):
        self.client = client
        self.loop = loop
        self.target = target

    async def _send(self, text):
        await self.client.send_message(self.target, text)
        return True

    def send(self, text):
        import asyncio

        try:
            fut = asyncio.run_coroutine_threadsafe(self._send(text), self.loop)
            return bool(fut.result(timeout=30))
        except Exception as exc:  # noqa: BLE001
            logger.warning("User-client send failed: %s", exc)
            return False

    def send_alert(self, bot_name, severity, analysis, raw_excerpt):
        return self.send(format_alert(bot_name, severity, analysis, raw_excerpt))


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def format_silence_alert(bot_name, silent_seconds):
    return (
        f"🔕 Bot went SILENT — {bot_name}\n\n"
        f"No messages for {_fmt_duration(silent_seconds)}. It may have crashed, "
        f"been banned, lost its connection, or stalled.\n\n"
        f"Suggested checks:\n"
        f"• Is the bot process / host still running?\n"
        f"• Account banned or hit a Steam Guard / captcha prompt?\n"
        f"• Network or proxy down?"
    )


def format_recovery_alert(bot_name):
    return f"✅ Back online — {bot_name} is reporting again."


def format_recurring_alert(group, window_minutes):
    """Alert for an error that keeps repeating (the recurring-error watchdog).

    ``group`` is one entry from ``IncidentStore.recurring`` (count, bots, the
    latest summary/excerpt).
    """
    bots = ", ".join(group.get("bots") or []) or "?"
    summary = (group.get("summary") or "").strip()
    if not summary:
        summary = (group.get("raw_excerpt") or "").strip()[:200]
    lines = [
        f"🔁 Recurring error — {group.get('count', '?')}× in the last "
        f"{int(window_minutes)} min",
        "",
        f"Bots: {bots}",
    ]
    if summary:
        lines += ["", summary]
    lines += ["", "It keeps happening and isn't clearing on its own — worth a look."]
    return "\n".join(lines)


def format_incident_resolved(bot, elapsed_seconds, *, we_fixed):
    """One-line closure when an open incident clears."""
    how = "fixed by WatcherDog" if we_fixed else "recovered on its own"
    return (f"✅ Resolved — {bot} is healthy again after "
            f"{_fmt_duration(elapsed_seconds)} ({how}).")


def format_incident_followup(bot, summary, elapsed_seconds, *, retrying):
    """Periodic 'still broken' nag while an incident stays open."""
    tail = "retrying the fix…" if retrying else "needs attention."
    head = f"⏳ {bot} — still unresolved after {_fmt_duration(elapsed_seconds)}"
    s = (summary or "").strip()
    if s:
        head += f"\n{s}"
    return f"{head}\n{tail}"


def format_incident_escalated(bot, summary, elapsed_seconds, *, needs_pc=False, retried=False):
    """Final give-up message. ``retried`` distinguishes 'we tried and stopped' from
    'there was never an automatic fix to try'."""
    need = "needs PC (power on / RDP)" if needs_pc else "needs manual attention"
    tail = "stopping auto-retries" if retried else "no automatic fix available"
    head = f"❌ {bot} — unresolved after {_fmt_duration(elapsed_seconds)}, {tail}"
    s = (summary or "").strip()
    if s:
        head += f"\n{s}"
    return f"{head} — {need}."


class TelegramAlerter:
    def __init__(self, bot_token, chat_id, thread_id=None, *, attempts=3):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.thread_id = thread_id or None
        self.attempts = attempts
        self._api = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def _post(self, text):
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if self.thread_id:
            try:
                payload["message_thread_id"] = int(self.thread_id)
            except (TypeError, ValueError):
                pass
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._api,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send(self, text):
        """Send text to Telegram with retry on transient failures.

        Returns True on success, False otherwise. Never raises.
        """
        for attempt in range(1, self.attempts + 1):
            try:
                result = self._post(text)
                if result.get("ok"):
                    return True
                logger.warning("Telegram API returned not-ok: %s", result)
                return False
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")
                except Exception:
                    pass
                logger.warning(
                    "Telegram HTTP %s on attempt %d/%d: %s",
                    exc.code, attempt, self.attempts, detail,
                )
                # 4xx (bad token/chat id) won't fix itself — stop early.
                if 400 <= exc.code < 500 and exc.code != 429:
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Telegram send error on attempt %d/%d: %s",
                    attempt, self.attempts, exc,
                )
            if attempt < self.attempts:
                time.sleep(2 ** attempt)
        return False

    def send_alert(self, bot_name, severity, analysis, raw_excerpt):
        return self.send(format_alert(bot_name, severity, analysis, raw_excerpt))
