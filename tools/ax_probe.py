#!/usr/bin/env python3
"""Probe whether the native Telegram app exposes its UI (chat list, messages)
via the macOS Accessibility API. Read-only — clicks nothing, types nothing."""

from __future__ import annotations

import sys

from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyAttributeNames,
    AXUIElementCreateApplication,
)
from AppKit import NSWorkspace


def pid_for(bundle_id):
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == bundle_id:
            return app.processIdentifier()
    return None


def attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def names(el):
    err, val = AXUIElementCopyAttributeNames(el, None)
    return list(val) if err == 0 else []


def walk(el, depth, max_depth, counter, hits):
    if el is None or depth > max_depth:
        return
    role = attr(el, "AXRole") or "?"
    title = attr(el, "AXTitle")
    value = attr(el, "AXValue")
    desc = attr(el, "AXDescription")
    text = None
    for cand in (value, title, desc):
        if isinstance(cand, str) and cand.strip():
            text = cand.strip()
            break
    counter[0] += 1
    if text and len(text) > 1:
        hits.append((depth, role, text[:90]))
    children = attr(el, "AXChildren") or []
    for child in children:
        walk(child, depth + 1, max_depth, counter, hits)


def main():
    print("AXIsProcessTrusted:", AXIsProcessTrusted())
    if not AXIsProcessTrusted():
        print("\n>>> Accessibility permission NOT granted to this process.")
        print(">>> Grant it in System Settings > Privacy & Security > Accessibility,")
        print(">>> then re-run. (The app to enable is your Terminal / the python binary.)")
        return 1

    pid = pid_for("ru.keepcoder.Telegram")
    if not pid:
        print("Telegram not running.")
        return 1
    print("Telegram pid:", pid)

    app = AXUIElementCreateApplication(pid)
    windows = attr(app, "AXWindows") or []
    print("windows:", len(windows))

    counter = [0]
    hits = []
    for w in windows:
        walk(w, 0, 22, counter, hits)

    print(f"visited {counter[0]} elements; {len(hits)} carried text\n")
    print("--- sample of readable text elements (depth | role | text) ---")
    for depth, role, text in hits[:80]:
        print(f"{depth:>2} | {role:<22} | {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
