"""macOS GUI automation primitives — drive the real Telegram app like a person.

Reading is done by screenshotting the Telegram window and running on-device OCR
(Apple's Vision framework). Clicking and typing use synthetic CoreGraphics
events. No Telegram API, no api_id — just the GUI.

Requires: Screen Recording permission (to capture) and Accessibility permission
(to post mouse/keyboard events). Telegram must be visible/frontmost.
"""

from __future__ import annotations

import math
import random
import subprocess
import threading
import time

import Quartz
from AppKit import NSWorkspace
from Foundation import NSURL
from Vision import VNImageRequestHandler, VNRecognizeTextRequest

TELEGRAM_BUNDLE = "ru.keepcoder.Telegram"


# --- app / window -----------------------------------------------------------
def activate(bundle_id=TELEGRAM_BUNDLE, settle=1.2):
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == bundle_id:
            app.activateWithOptions_(1)  # NSApplicationActivateIgnoringOtherApps
            time.sleep(settle)
            return True
    return False


def window_bounds(owner_name="Telegram"):
    """Return (window_id, x, y, w, h) in screen points for the largest window
    owned by `owner_name`, or None."""
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    best = None
    for w in wins:
        if w.get("kCGWindowOwnerName") == owner_name:
            b = w["kCGWindowBounds"]
            area = b["Width"] * b["Height"]
            if best is None or area > best[0]:
                best = (area, int(w["kCGWindowNumber"]),
                        b["X"], b["Y"], b["Width"], b["Height"])
    if not best:
        return None
    _area, wid, x, y, ww, hh = best
    return (wid, x, y, ww, hh)


def capture_window(window_id, path="/tmp/wd_window.png"):
    _gate()
    subprocess.run(["screencapture", "-x", "-o", f"-l{window_id}", path],
                   check=True, capture_output=True)
    return path


# --- OCR --------------------------------------------------------------------
class Fragment:
    """One recognized piece of text with its center in SCREEN points."""
    __slots__ = ("text", "nx", "ny", "nw", "nh", "cx", "cy")

    def __init__(self, text, nx, ny, nw, nh, bounds):
        self.text = text
        self.nx, self.ny, self.nw, self.nh = nx, ny, nw, nh
        _wid, x, y, ww, hh = bounds
        # Vision origin is bottom-left and normalized; convert center to
        # top-left screen points.
        self.cx = x + (nx + nw / 2.0) * ww
        self.cy = y + (1.0 - (ny + nh / 2.0)) * hh

    def __repr__(self):
        return f"Fragment({self.text!r} @ {self.cx:.0f},{self.cy:.0f})"


def ocr_window(bounds, path="/tmp/wd_window.png"):
    """Capture the window and OCR it. Returns a list of Fragment (screen coords)."""
    capture_window(bounds[0], path)
    url = NSURL.fileURLWithPath_(path)
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    req = VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)            # accurate
    req.setUsesLanguageCorrection_(False)
    if not handler.performRequests_error_([req], None):
        return []
    frags = []
    for obs in req.results():
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        bb = obs.boundingBox()
        frags.append(Fragment(
            cand[0].string(),
            bb.origin.x, bb.origin.y, bb.size.width, bb.size.height, bounds,
        ))
    return frags


def split_columns(frags, divider=0.5):
    """Split fragments into (sidebar, conversation) by normalized x of center."""
    sidebar, convo = [], []
    for f in frags:
        (sidebar if (f.nx + f.nw / 2.0) < divider else convo).append(f)
    return sidebar, convo


# --- input (mouse / keyboard) ----------------------------------------------
# Human-like smoothing of mouse moves, scrolling and typing. Toggle with
# set_smooth(False) to fall back to instant, deterministic actions (useful for
# debugging or when you want the fastest possible scan).
_SMOOTH = True


def set_smooth(enabled):
    """Enable/disable human-like smoothing of GUI actions globally."""
    global _SMOOTH
    _SMOOTH = bool(enabled)


# --- pause / resume (F10) ---------------------------------------------------
# When paused, every GUI action blocks at the next event so the user can take
# over the Mac; pressing the hotkey again resumes exactly where it left off.
_PAUSED = threading.Event()        # set == paused


def pause():
    _PAUSED.set()


def resume():
    _PAUSED.clear()


def toggle_pause():
    resume() if _PAUSED.is_set() else pause()
    return _PAUSED.is_set()


def is_paused():
    return _PAUSED.is_set()


def _gate():
    """Block here while paused (checked before every input/capture)."""
    while _PAUSED.is_set():
        time.sleep(0.1)


def install_pause_hotkey(keycode=109, on_change=None):
    """Start a daemon thread that toggles pause when `keycode` is pressed
    (109 = F10). Uses key-state polling, so no event tap is needed — just the
    Accessibility permission we already have."""
    def _loop():
        prev = False
        last_toggle = 0.0
        while True:
            try:
                down = bool(Quartz.CGEventSourceKeyState(
                    Quartz.kCGEventSourceStateHIDSystemState, keycode))
            except Exception:
                down = False
            now = time.monotonic()
            # Debounce: only toggle on a fresh press edge, and never more than
            # once per 0.7s (a single physical tap can otherwise register as
            # several edges and pause→resume instantly).
            if down and not prev and (now - last_toggle) > 0.7:
                last_toggle = now
                state = toggle_pause()
                if on_change:
                    try:
                        on_change(state)
                    except Exception:
                        pass
            prev = down
            time.sleep(0.05)

    t = threading.Thread(target=_loop, daemon=True, name="pause-hotkey")
    t.start()
    return t


def _post(ev):
    _gate()
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _mouse_pos():
    """Current pointer location in screen points."""
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


def _move_instant(x, y):
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0))


def _smoothstep(t):
    """Ease-in-out (acceleration then deceleration)."""
    return t * t * (3.0 - 2.0 * t)


def move_mouse(x, y, duration=None, steps=None):
    """Glide the pointer to (x, y) along an eased, slightly-arced path with a
    little jitter — like a human hand rather than an instant teleport.

    With smoothing off (or a negligible distance) this is a single move event.
    """
    if not _SMOOTH:
        _move_instant(x, y)
        return
    sx, sy = _mouse_pos()
    dx, dy = x - sx, y - sy
    dist = math.hypot(dx, dy)
    if dist < 2.0:
        _move_instant(x, y)
        return
    if steps is None:
        steps = max(8, min(55, int(dist / 12)))
    if duration is None:
        duration = min(0.42, 0.10 + dist / 2600.0)
    # unit normal to the travel direction, for a gentle bow in the path
    nx, ny = -dy / dist, dx / dist
    arc = random.uniform(-1.0, 1.0) * min(16.0, dist * 0.08)
    dt = duration / steps
    for i in range(1, steps + 1):
        p = i / steps
        t = _smoothstep(p)
        bow = math.sin(math.pi * p) * arc        # peaks mid-path, zero at ends
        cx = sx + dx * t + nx * bow + random.uniform(-0.6, 0.6)
        cy = sy + dy * t + ny * bow + random.uniform(-0.6, 0.6)
        _move_instant(cx, cy)
        time.sleep(dt)
    _move_instant(x, y)                          # precise landing


def click(x, y, settle=0.4):
    move_mouse(x, y)
    time.sleep(random.uniform(0.04, 0.11) if _SMOOTH else 0.05)
    pt = (x, y)
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, pt,
                                         Quartz.kCGMouseButtonLeft))
    time.sleep(random.uniform(0.03, 0.09) if _SMOOTH else 0.0)
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, pt,
                                         Quartz.kCGMouseButtonLeft))
    time.sleep(settle)


def scroll(x, y, lines, settle=0.35):
    """Scroll the wheel by `lines` (negative = down) with the pointer at x,y.
    Moves the pointer over the target first so the scroll hits that view.

    With smoothing on, the wheel motion is broken into a few small ticks with
    short jittered pauses, so it reads as a hand-rolled scroll instead of one
    instantaneous jump."""
    move_mouse(x, y)
    time.sleep(0.05)
    lines = int(lines)
    if not _SMOOTH or lines == 0:
        _post(Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, lines))
        time.sleep(settle)
        return
    step = 1 if lines > 0 else -1
    remaining = lines
    while remaining != 0:
        chunk = step * min(3, abs(remaining))    # a few lines per tick
        _post(Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, chunk))
        remaining -= chunk
        time.sleep(random.uniform(0.015, 0.05))
    time.sleep(settle)


def scroll_to_top(x, y):
    """Scroll the view at x,y all the way up."""
    for _ in range(12):
        scroll(x, y, 10, settle=0.12)
    time.sleep(0.2)


def type_text(text):
    """Type a unicode string character-by-character (no Enter).

    The unicode string is set only on the key-DOWN event; the key-up is a bare
    event. Setting it on both can cause apps to drop or double characters.

    With smoothing on, the per-keystroke delay varies (with longer pauses after
    punctuation/spaces and the occasional brief hesitation) to mimic real
    human typing cadence rather than a metronomic stream.
    """
    for ch in text:
        down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(down, len(ch), ch)
        _post(down)
        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        _post(up)
        if not _SMOOTH:
            time.sleep(0.012)
            continue
        delay = random.uniform(0.03, 0.09)
        if ch in ".,!?;:":
            delay += random.uniform(0.05, 0.18)
        elif ch == " ":
            delay += random.uniform(0.0, 0.05)
        if random.random() < 0.03:               # occasional "thinking" pause
            delay += random.uniform(0.15, 0.40)
        time.sleep(delay)


_KEY_RETURN = 36
_KEY_ESCAPE = 53
_KEY_A = 0
_KEY_V = 9
_KEY_DELETE = 51


def press_key(keycode, settle=0.2):
    # Explicitly clear modifier flags so a preceding Cmd+V/Cmd+A doesn't leave
    # the Command flag set, which would turn a "plain" Return into Cmd+Return.
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(ev, 0)
        _post(ev)
    time.sleep(settle)


def press_key_cmd(keycode, settle=0.2):
    """Press a key with the Command modifier held (e.g. Cmd+A, Cmd+V)."""
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        _post(ev)
    time.sleep(settle)


def press_return():
    press_key(_KEY_RETURN)


def press_escape():
    press_key(_KEY_ESCAPE)


def set_clipboard(text):
    """Put text on the system clipboard (for paste-based input)."""
    from AppKit import NSPasteboard, NSPasteboardTypeString

    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def clear_input():
    """Select-all + delete. Only call when the message input is focused."""
    press_key_cmd(_KEY_A, settle=0.1)
    press_key(_KEY_DELETE, settle=0.15)


def paste():
    """Cmd+V — paste clipboard into the focused field."""
    press_key_cmd(_KEY_V, settle=0.2)


def send_cmd_return():
    """Cmd+Return — sends in Telegram's 'Send with Cmd+Enter' mode."""
    press_key_cmd(_KEY_RETURN, settle=0.4)


def send_plain_return():
    """Return — sends in Telegram's default 'Send by Enter' mode."""
    press_key(_KEY_RETURN, settle=0.4)
