"""Deterministic recovery decision engine (R1-R6). Pure — no I/O, no model.

`observe()` advances per-panel timers from a fresh PanelStatus; `decide()` reads
status + status_age + timers and returns a Decision. R4 (black screenshot) is a
caller-side follow-up to a failing R2 (see mcp_watcher._evaluate_panel).
See docs/wiki/components/Monitoring and Recovery Rules.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PanelState:
    over_launch_since: float | None = None
    last_score: str | None = None
    last_score_ts: float | None = None
    last_action_ts: float | None = None
    r2_attempted_ts: float | None = None
    flag_alerted: bool = False   # latch: a cold-case flag was already alerted
    # Recovery-episode tracking: drives the `Panel | Issue | Fixed/Not` report
    # and the retry-cap that escalates a futile loop to a cold case (needs PC).
    recover_attempts: int = 0
    episode_issue: str | None = None
    coldcase_reported: bool = False
    last_probe_ts: float | None = None   # last R6 /start liveness probe (debounce)
    last_msg_ts: float | None = None     # ts of the panel's latest INCOMING message
                                         # seen by _evaluate_panel (gap detection)


@dataclass
class Decision:
    kind: str
    actions: list = field(default_factory=list)
    reason: str = ""
    destructive: bool = False
    cold_case: bool = False
    healthy: bool = False   # set on the "healthy" noop so the caller can report a fix


# Status substrings that mean the accounts are up and WORKING — never a relaunch
# trigger. "LIVE" is idle-operational; "Searching game…" is actively hunting a
# match; match/playing states are in-game. Anything else we can READ (e.g.
# "OFFLINE") is treated as not operational and restores to four (R2).
_OPERATIONAL_STATUS = ("live", "search", "match", "in game", "in-game", "playing")
# Explicit DOWN markers WIN over any operational keyword — a readable down-state
# like "Not live" / "Offline (was live)" contains "live" but must still relaunch.
_DOWN_STATUS = ("offline", "not live", "stopped", "dead", "down", "error",
                "crash", "frozen", "disconnect", "no connection")


def _is_operational(status):
    """The panel/accounts are up and working (LIVE, searching, or in a match).

    An explicit down-marker (OFFLINE, "not live", stopped, …) always wins — so a
    readable down-state still relaunches (R2) even though its text happens to
    contain a substring like "live"."""
    s = (status.status or "").lower()
    if any(k in s for k in _DOWN_STATUS):
        return False
    return status.in_match or any(k in s for k in _OPERATIONAL_STATUS)


def _is_searching(status):
    """Panel is actively looking for a game — a healthy state with no match yet."""
    return "search" in (status.status or "").lower()


def observe(status, state, now, cfg):
    """Advance timers from a fresh status. Returns the (mutated) state."""
    if status is None or status.launched is None:
        return state
    target = int(getattr(cfg, "panel_target_accounts", 4))
    if status.launched > target:
        if state.over_launch_since is None:
            state.over_launch_since = now
    else:
        state.over_launch_since = None
    if status.score != state.last_score:
        state.last_score = status.score
        state.last_score_ts = now
    return state


def decide(status, status_age, state, now, cfg):
    """Return the recovery Decision. Precedence: R6 -> R1 -> R2 -> R3 -> noop."""
    target = int(getattr(cfg, "panel_target_accounts", 4))
    overlaunch_s = float(getattr(cfg, "panel_overlaunch_minutes", 15)) * 60.0
    idle_s = float(getattr(cfg, "panel_idle_minutes", 10)) * 60.0
    stale_s = float(getattr(cfg, "panel_stale_minutes", 70)) * 60.0

    if status is None or status_age is None or status_age > stale_s:
        return Decision("flag", reason="panel/PC down or status stale — needs per-PC API",
                        cold_case=True)
    if status.launched is None:
        # Not a parseable status card (a normal drop/match/warmup post, or a
        # non-panel chat). Defer to the normal monitoring path — never act or
        # flag on a message that simply isn't a status card.
        return Decision("noop", reason="no status card")

    if status.launched > target:
        since = state.over_launch_since
        if since is not None and (now - since) >= overlaunch_s:
            return Decision("sequence",
                            actions=["kill_all", "select_unfarmed", "start_selected"],
                            reason=f"{status.launched}>{target} for >{overlaunch_s/60:.0f}m",
                            destructive=True)
        return Decision("noop", reason=f"over-launch observed ({status.launched}); waiting")

    # R2 — under target, or a status we can READ that is NOT operational (e.g.
    # OFFLINE). "LIVE" / "Searching game…" / in-match all count as operational and
    # do NOT relaunch. A status we CANNOT read (status.status is None) is never
    # inferred as down — it falls through to the healthy/idle checks below.
    if status.launched < target or (status.status is not None and not _is_operational(status)):
        return Decision("sequence", actions=["select_unfarmed", "start_selected"],
                        reason=f"launched={status.launched}, status={status.status!r}")

    # R3 — idle/stuck: only when the panel is operational.
    if _is_operational(status):
        if not status.in_match:
            # Up but not even searching for a game -> kick it. If it's ALREADY
            # searching, that's the healthy working state — leave it alone.
            if not _is_searching(status):
                return Decision("sequence", actions=["make_lobbies"],
                                reason="up but no map/score")
        elif (state.last_score == status.score and state.last_score_ts is not None
                and (now - state.last_score_ts) >= idle_s):
            return Decision("sequence", actions=["make_lobbies"],
                            reason=f"score unchanged >{idle_s/60:.0f}m")

    return Decision("noop", reason="healthy", healthy=True)
