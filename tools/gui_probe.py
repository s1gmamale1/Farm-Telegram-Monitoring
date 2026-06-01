#!/usr/bin/env python3
"""Prove the screenshot+OCR read path: locate the Telegram window, capture it,
and run macOS Vision OCR. Read-only."""

from __future__ import annotations

import subprocess
import sys

import Quartz
from Foundation import NSURL
from Vision import VNImageRequestHandler, VNRecognizeTextRequest


def telegram_window():
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    best = None
    for w in wins:
        if w.get("kCGWindowOwnerName") == "Telegram":
            b = w["kCGWindowBounds"]
            # Pick the largest Telegram window (the main one, not tooltips).
            area = b["Width"] * b["Height"]
            if best is None or area > best[0]:
                best = (area, int(w["kCGWindowNumber"]), b)
    return best


def ocr(path):
    url = NSURL.fileURLWithPath_(path)
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    req = VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)  # 0 = accurate
    req.setUsesLanguageCorrection_(False)
    ok = handler.performRequests_error_([req], None)
    if not ok:
        return []
    out = []
    for obs in req.results():
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        bb = obs.boundingBox()  # normalized, origin bottom-left
        out.append((cand[0].string(), bb.origin.x, bb.origin.y, bb.size.width, bb.size.height))
    return out


def activate_telegram():
    import time

    from AppKit import NSWorkspace
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == "ru.keepcoder.Telegram":
            app.activateWithOptions_(1)  # NSApplicationActivateIgnoringOtherApps
            time.sleep(1.5)
            return True
    return False


def main():
    activate_telegram()
    tw = telegram_window()
    if not tw:
        print("No Telegram window found on screen (is it minimized/hidden?).")
        return 1
    _area, wid, b = tw
    print(f"Telegram window id={wid} bounds=({int(b['X'])},{int(b['Y'])}) {int(b['Width'])}x{int(b['Height'])}")

    out_path = "/tmp/tg_window.png"
    subprocess.run(["screencapture", "-x", "-o", f"-l{wid}", out_path], check=True)

    lines = ocr(out_path)
    print(f"OCR recognized {len(lines)} text fragments. Top-to-bottom sample:\n")
    # Sort by vertical position (Vision y origin is bottom-left, so higher y = top).
    for text, x, y, w, h in sorted(lines, key=lambda r: (-r[2], r[1]))[:60]:
        print(f"  x={x:.2f} y={y:.2f}  {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
