# Launch-grace + RDP-bug auto-reboot

**Date:** 2026-06-12
**Status:** Approved (owner-dictated behavior; standing approval)
**Origin:** Live SF21 episode, day 1 of the deterministic-core deploy. The
watcher pressed relaunch 3× while the panel said `Status: Accounts launching…`
and declared a cold case at 17:38 — the minute 6 accounts came up ("All 6
accounts launched!" at 17:43). Separately, the panel's own replies carried
`➔ ❌ Error creating screenshot: screen grab failed` — the owner's known marker
for a bugged-out RDP window — and the watcher ignored it.

**Owner's prescription (verbatim):** "If ts happens for 30 mins straight,
symptoms accounts not launched in exact amount, or failed to launch accounts
or simply screenshot request attempt is failing, if the script that should
take care of it doesn't recover it than needs to Reboot PC -> Confirm and wait
for 15 mins and check again." — This authorizes the first AUTO-destructive
host action (Reboot PC), gated as below.

## C1 — Launch grace (fixes the false cold-case)

- `PanelState` += `launching_since: float|None`. `panel_rules.observe()` sets
  it when the parsed status contains `launching` (e.g. `Accounts launching...`)
  and clears it when the status is operational or no longer launching.
- `panel_rules.decide()`: between R1 (over-launch — still wins; a 6-up panel
  mid-launch is R1's existing business) and R2: if `launching_since` is set and
  `now - launching_since < PANEL_LAUNCH_GRACE_MINUTES*60` → `Decision("noop",
  reason="launch in progress")`. Launching BEYOND grace = stuck launch → rules
  resume normally (R2 etc.).
- Self-report-silence handler: replace the bool `_panel_responds` probe with a
  3-state + text probe (see Plumbing). If the probe reply's `Status:` line says
  launching → return "self-report: launch in progress" — **no attempt counted,
  no relaunch pressed, no cold case** — and ARM `launching_since` so the rules
  see it too. `All N accounts launched!` / operational status / fresh farm
  traffic clear the state (existing healthy-branch episode closure unchanged).

## C2 — RDP-bug signal

- Marker: case-insensitive substring `error creating screenshot` OR
  `screen grab failed` in any panel text the watcher reads (latest messages in
  `_evaluate_panel`, probe reply text in the self-report handler).
- `PanelState` += `rdp_bug_since: float|None` — set on first sight (per
  episode), NOT refreshed on every sight (it measures "bugged since"); cleared
  when the panel goes operational, when a watcher-side screenshot succeeds
  (R4 path), or when the episode resolves.

## C3 — Auto `Reboot PC → Confirm` (owner-authorized)

- **Trigger** (checked each sweep in `_evaluate_panel`, before any cold-case
  declaration): `rdp_bug_since` set AND `now - rdp_bug_since >=
  RDP_BUG_REBOOT_MINUTES*60` AND panel not operational AND not already
  rebooted this episode.
- **Gates:** `cfg.panel_auto_destructive` (the existing auto-Kill-all trust
  flag — flipping it off restores confirm-card-only behavior), `deliver`
  (dry-run never presses), once per episode (`reboot_attempted` latch).
- **Action:** new `tg_actions.press_button_then_confirm(client, chat, "reboot
  pc", timeout)` — open menu → click the button matching `reboot pc`
  (`🔄⚠️ Reboot PC` exists on the live panels) → await the panel's reply → if
  it carries a button matching `confirm`/`yes`/`✅` → click that → return the
  final reply. Wrapped as `panel_actions.reboot_pc(client, panel, cfg)`.
- **Then:** one-line ibo alert ("🔄 SFx — RDP bugged ≥30m, rebooting the PC;
  re-checking in 15m"), `daily_report.record(... fix="Reboot PC", ...)`,
  `reboot_ts` set; for `REBOOT_WAIT_MINUTES` the panel's evaluate/self-report
  paths return a quiet "post-reboot wait" (no attempts, no flags, no nags-feed
  updates).
- **After the wait:** the existing machinery decides — fresh operational
  traffic resolves the episode via the healthy branch (report reads `Fixed ✅`,
  resolution notes the reboot via `fix_attempted="reboot_pc"` on the open row
  when one exists); still broken → cold case needs-PC exactly as today
  (terminal; the reboot latch prevents loops).
- If the trigger fires while attempts are exhausted but `rdp_bug_since <
  30m`, HOLD (no cold case yet) — the reboot path supersedes the cold-case
  declaration while the RDP signal is live; panels with NO rdp signal cold-case
  exactly as today.

## Plumbing

- `tg_actions.panel_menu` return gains `"text"` (the menu message body) —
  additive; lets probes parse `Status:` and spot the screenshot-error line.
- New `_panel_probe(client, target_ref, cfg) -> (alive: True|False|None,
  text: str)` in mcp_watcher wrapping `panel_menu` with `_panel_responds`'s
  exact 3-state semantics; `_panel_responds` becomes a thin wrapper (R6
  callers unchanged).

## Config (3 new keys)

| key | default |
|---|---|
| `PANEL_LAUNCH_GRACE_MINUTES` | `15` |
| `RDP_BUG_REBOOT_MINUTES` | `30` |
| `REBOOT_WAIT_MINUTES` | `15` |

## Testing

- **Regression replay of the real SF21 timeline:** probe replies with
  `Accounts launching...` at T+0/T+4/T+8 → zero attempts burned, no cold case;
  at T+13 operational → episode closes clean.
- Grace expiry: launching for > grace → R2 resumes.
- R1 precedence: over-launch while launching still kills (existing rule wins).
- RDP marker: set on the ➔ error line (both ingestion points), NOT refreshed on
  re-sight, cleared on operational.
- Reboot trigger: fires only past 30m + not operational + auto-destructive on +
  deliver; once per episode; dry-run never presses; the two-press sequence
  presses `🔄⚠️ Reboot PC` then the confirm button (fake panel reply with
  buttons); post-reboot quiet window suppresses attempts/flags; after the
  window, healthy resolves / broken cold-cases.
- Cold-case hold: attempts exhausted + rdp signal at <30m → no cold case yet.
- Real `IncidentTracker` where rows are touched; mutation-verify the grace
  check, the 30m comparison, the once-per-episode latch, and the dry-run gate.

## Files

| file | change |
|---|---|
| `watcherdog/panel_rules.py` | `launching_since`/`rdp_bug_since`/`reboot_ts`/`reboot_attempted` on `PanelState`; `_is_launching`; grace check in `decide()`; observe() updates |
| `watcherdog/tg_actions.py` | `panel_menu` +text; `press_button_then_confirm` |
| `watcherdog/panel_actions.py` | `reboot_pc` |
| `watcherdog/mcp_watcher.py` | `_panel_probe`; self-report launch check; rdp-marker ingestion; reboot trigger + quiet window; cold-case hold |
| `watcherdog/config.py` | 3 keys |
| `tests/test_panel_rules.py` (+ a new `tests/test_rdp_reboot.py`) | per Testing |

## Execution

Inline: worktree → TDD → mutation-verify → reviewer pass (full — destructive
action path) → PR → merge-if-clean. After merge: restart the running watcher
to pick up the fix (stop → relaunch, same nohup command).
