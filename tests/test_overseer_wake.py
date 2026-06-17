"""Tests for the host-side overseer WAKE wrapper (Component B).

The DECISION LOGIC is pure: `run_wake` takes an injected clock (`now`), an
injected `runner` (so a real agent is NEVER spawned), an injected `data_dir`
(so lock/cooldown/log live under `tmp_path` and the real `data/` is untouched),
and an injected `env` + `build_report` (so knobs and the probe outcome are
controllable without touching the process environment or a live watcher).

Two tests touch real processes: the process-group-kill test (real `_run_group`)
and the fork-storm single-flight test (os.fork). Both use tiny timeouts/barriers
so they stay fast.
"""
import json
import os
import signal
import time

import scripts.overseer_wake as ow


# --------------------------------------------------------------------------- #
# Helpers: a fake runner that records its argv + stdin and returns a canned rc;
# a cfg whose db_path lives under tmp_path (data_dir derives from it); a tiny
# log reader.

class FakeRunner:
    """Records the (argv, stdin_json) it was called with; returns (rc, elapsed).
    Stand-in for `_run_group` so no real agent process is ever spawned."""

    def __init__(self, rc=0, elapsed=0.1):
        self.calls = []
        self.rc = rc
        self.elapsed = elapsed

    def __call__(self, argv, stdin_json, timeout):
        self.calls.append({"argv": argv, "stdin": stdin_json, "timeout": timeout})
        return self.rc, self.elapsed

    @property
    def invoked(self):
        return len(self.calls) > 0


def _cfg(tmp_path):
    # data_dir derives from db_path's dirname → sandbox, never the real data/.
    import types
    return types.SimpleNamespace(db_path=str(tmp_path / "incidents.db"))


def _report(*, alive=True, wedged=False, stuck_bots=(), stuck_stale=None,
            healthy=None):
    """Build a probe-style report dict. `stuck_bots` drives flagged_stuck."""
    stale = stuck_stale if stuck_stale is not None else [
        {"bot": b, "open_min": 20} for b in stuck_bots]
    if healthy is None:
        healthy = alive and not wedged and not stuck_bots
    return {
        "process_alive": alive,
        "wedged": wedged,
        "healthy": healthy,
        "flagged": {"count": len(stuck_bots), "bots": list(stuck_bots)},
        "flagged_stuck": {"count": len(stuck_bots), "bots": list(stuck_bots),
                          "stale": stale},
    }


def _br(report, code):
    """A build_report stand-in returning a fixed (report, code)."""
    return lambda cfg, now, **kw: (report, code)


def _log_text(data_dir):
    p = os.path.join(data_dir, "overseer_wake.log")
    if not os.path.exists(p):
        return ""
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def _run(tmp_path, *, report, code, runner=None, env=None, now=10_000.0):
    """Drive run_wake with all seams injected; returns (result, runner)."""
    runner = runner or FakeRunner()
    env = {} if env is None else env
    result = ow.run_wake(
        _cfg(tmp_path), now,
        data_dir=str(tmp_path),
        runner=runner,
        env=env,
        build_report=_br(report, code),
    )
    return result, runner


# --------------------------------------------------------------------------- #
# Decision logic: when does the wrapper invoke the agent at all?

def test_healthy_code0_no_invoke(tmp_path):
    # code==0 (healthy / in-flight) → log "ok", never invoke, no lock acquired.
    result, runner = _run(tmp_path, report=_report(), code=0,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "ok"
    assert not runner.invoked
    assert " ok " in (" " + _log_text(str(tmp_path)).split("\n")[0] + " ")
    # No-op decision creates no lock file (single-flight guards only the wake).
    assert not os.path.exists(os.path.join(str(tmp_path), "overseer_wake.lock"))


def test_dead_watcher_invokes_and_bypasses_cooldown(tmp_path):
    # A DEAD watcher is urgent → invokes even though a same-key cooldown is set.
    runner = FakeRunner()
    rep = _report(alive=False, healthy=False)
    reason, key, urgent = ow._reason(rep)
    # Pre-stamp a fresh cooldown for this key — urgent must bypass it.
    ow._stamp_cooldown(str(tmp_path), key, 10_000.0)
    result, runner = _run(tmp_path, report=rep, code=1, runner=runner,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_wedged_watcher_invokes(tmp_path):
    result, runner = _run(tmp_path, report=_report(alive=True, wedged=True,
                                                   healthy=False), code=1,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_stuck_incident_invokes(tmp_path):
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera7"]), code=1,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_inflight_flagged_code0_no_invoke(tmp_path):
    # The probe says code 0 even though `flagged` is non-empty (core laddering);
    # the wrapper trusts the exit code and does NOT wake.
    rep = _report(stuck_bots=[])
    rep["flagged"] = {"count": 1, "bots": ["SinFermera7"]}
    result, runner = _run(tmp_path, report=rep, code=0,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "ok"
    assert not runner.invoked


# --------------------------------------------------------------------------- #
# Single-flight lock (fcntl.flock on a persistent fd — no PID-file race, the
# kernel drops the lock when the holder dies).

def test_lock_held_by_live_holder_skips(tmp_path):
    # Another holder owns the flock (a real fcntl.flock on the lock fd) → skip.
    import fcntl
    lock = os.path.join(str(tmp_path), "overseer_wake.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result, runner = _run(tmp_path,
                              report=_report(stuck_bots=["SinFermera7"]),
                              code=1, env={"OVERSEER_WAKE_CMD": "agent"})
        assert result["decision"] == "skip:inflight"
        assert not runner.invoked
        # The lock file persists (flock is advisory; we don't unlink a held one).
        assert os.path.exists(lock)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lock_not_held_invokes(tmp_path):
    # A bare lock FILE with stale pid TEXT inside but NO live flock holder → the
    # flock acquires cleanly and we wake. flock, not the file's contents, is what
    # makes a lock "held" — exactly the PID-reuse hole flock closes (a leftover
    # pid number, even a recycled live one, never blocks us).
    lock = os.path.join(str(tmp_path), "overseer_wake.lock")
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write("999999")   # stale pid text; nobody holds the flock
    result, runner = _run(tmp_path, report=_report(stuck_bots=["SinFermera7"]),
                          code=1, env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_lock_released_after_wake(tmp_path):
    # After a (fake, instant) wake the flock is released, so a second run also
    # wakes — the lock is not stuck held.
    r1, run1 = _run(tmp_path, report=_report(stuck_bots=["SinFermera7"]),
                    code=1, env={"OVERSEER_WAKE_CMD": "agent"})
    r2, run2 = _run(tmp_path, report=_report(alive=False, healthy=False),
                    code=1, env={"OVERSEER_WAKE_CMD": "agent"})
    assert r1["decision"] == "woke" and run1.invoked
    assert r2["decision"] == "woke" and run2.invoked


# --------------------------------------------------------------------------- #
# Keyed, stuck-only cooldown

def test_keyed_cooldown_different_bot_still_invokes(tmp_path):
    # Stamp a cooldown for SinFermera7, then a stuck incident for SinFermera14.
    # Different key → NOT suppressed → still wakes.
    ow._stamp_cooldown(str(tmp_path), "stuck:SinFermera7", 10_000.0)
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera14"]), code=1,
                          now=10_060.0,  # 60s later, well within cooldown
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_same_key_within_cooldown_skips(tmp_path):
    ow._stamp_cooldown(str(tmp_path), "stuck:SinFermera7", 10_000.0)
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera7"]), code=1,
                          now=10_060.0,  # 60s < 30min cooldown
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "skip:cooldown"
    assert not runner.invoked
    # A cooldown-skip is a no-op decision: it creates no lock file.
    assert not os.path.exists(os.path.join(str(tmp_path), "overseer_wake.lock"))


def test_same_key_after_cooldown_invokes(tmp_path):
    ow._stamp_cooldown(str(tmp_path), "stuck:SinFermera7", 10_000.0)
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera7"]), code=1,
                          now=10_000.0 + 31 * 60,  # past the default 30min
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_dead_within_cooldown_still_invokes(tmp_path):
    # Dead/wedged bypass the cooldown entirely (the "down" key is even stamped).
    ow._stamp_cooldown(str(tmp_path), "down", 10_000.0)
    result, runner = _run(tmp_path, report=_report(alive=False, healthy=False),
                          code=1, now=10_060.0,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_cooldown_corrupt_json_tolerated(tmp_path):
    # A corrupt cooldown file is treated as empty (never crash) → wakes.
    cd = os.path.join(str(tmp_path), "overseer_wake.cooldowns.json")
    with open(cd, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera7"]), code=1,
                          env={"OVERSEER_WAKE_CMD": "agent"})
    assert result["decision"] == "woke"
    assert runner.invoked


def test_cooldown_prunes_old_entries_on_write(tmp_path):
    # An entry older than ~1 day is pruned when we next stamp.
    old = 10_000.0 - (ow.COOLDOWN_PRUNE_S + 100)
    ow._stamp_cooldown(str(tmp_path), "stuck:Ancient", old)
    ow._stamp_cooldown(str(tmp_path), "stuck:Fresh", 10_000.0)
    data = ow._read_cooldowns(str(tmp_path))
    assert "stuck:Ancient" not in data
    assert "stuck:Fresh" in data


# --------------------------------------------------------------------------- #
# Pluggable exec contract

def test_cmd_unset_would_wake_no_invoke(tmp_path):
    # OVERSEER_WAKE_CMD unset → log "would-wake", no invoke, no lock left behind.
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera7"]), code=1,
                          env={})  # no OVERSEER_WAKE_CMD
    assert result["decision"] == "would-wake"
    assert not runner.invoked
    assert not os.path.exists(os.path.join(str(tmp_path), "overseer_wake.lock"))


def test_cmd_empty_would_wake_no_invoke(tmp_path):
    result, runner = _run(tmp_path,
                          report=_report(stuck_bots=["SinFermera7"]), code=1,
                          env={"OVERSEER_WAKE_CMD": "   "})
    assert result["decision"] == "would-wake"
    assert not runner.invoked
    assert not os.path.exists(os.path.join(str(tmp_path), "overseer_wake.lock"))


def test_invoke_argv_is_shlex_split_with_reason_last_and_json_stdin(tmp_path):
    # The EXACT contract: argv == shlex.split(cmd) + [reason]; stdin == report JSON.
    runner = FakeRunner()
    rep = _report(stuck_bots=["SinFermera7", "SinFermera14"])
    result, runner = _run(tmp_path, report=rep, code=1, runner=runner,
                          env={"OVERSEER_WAKE_CMD": "/bin/shim --flag value"})
    call = runner.calls[0]
    # bots SORTED in the human reason.
    assert call["argv"] == ["/bin/shim", "--flag", "value",
                            "stuck: SinFermera14,SinFermera7"]
    # stdin is the report as JSON.
    assert json.loads(call["stdin"]) == rep
    assert call["timeout"] == ow._timeout_s({"OVERSEER_WAKE_CMD": "x"})


def test_reason_and_key_for_stuck_bots_sorted(tmp_path):
    rep = _report(stuck_bots=["SinFermera14", "SinFermera7"])
    reason, key, urgent = ow._reason(rep)
    assert reason == "stuck: SinFermera14,SinFermera7"
    assert key == "stuck:SinFermera14,SinFermera7"  # sorted, stable
    assert urgent is False


def test_reason_normalizes_common_sinfermera_casing_typos(tmp_path):
    # Historical probe/log data sometimes carried `SInFermera##` casing, which
    # made cooldown keys distinct from the canonical `SinFermera##` spelling.
    rep = _report(stuck_bots=["SInFermera15", "SinFermera7"])
    reason, key, urgent = ow._reason(rep)
    assert reason == "stuck: SinFermera15,SinFermera7"
    assert key == "stuck:SinFermera15,SinFermera7"
    assert urgent is False


def test_reason_down_and_wedged(tmp_path):
    r_down, k_down, u_down = ow._reason(_report(alive=False, healthy=False))
    assert r_down == "watcher down" and k_down == "down" and u_down is True
    r_wedge, k_wedge, u_wedge = ow._reason(
        _report(alive=True, wedged=True, healthy=False))
    assert r_wedge == "wedged" and k_wedge == "wedged" and u_wedge is True


# --------------------------------------------------------------------------- #
# Error handling: build_report raises → synthesize + still wake

def test_build_report_raises_synthesizes_and_wakes(tmp_path):
    runner = FakeRunner()

    def _boom(cfg, now, **kw):
        raise RuntimeError("probe blew up")

    result = ow.run_wake(
        _cfg(tmp_path), 10_000.0,
        data_dir=str(tmp_path),
        runner=runner,
        env={"OVERSEER_WAKE_CMD": "agent"},
        build_report=_boom,
    )
    assert result["decision"] == "woke"
    assert runner.invoked
    call = runner.calls[0]
    # reason is the fixed "probe error"; argv has it last.
    assert call["argv"][-1] == "probe error"
    sent = json.loads(call["stdin"])
    assert sent["healthy"] is False
    assert "probe blew up" in sent["probe_error"]


def test_run_wake_never_raises_on_runner_failure(tmp_path):
    # A runner that explodes must be caught — the timer never crashes; the
    # outcome is specifically "error" (tightened: a regression flipping
    # error<->woke must be caught, not silently allowed).
    class Boom:
        def __call__(self, *a, **k):
            raise OSError("exec not found")
    result = ow.run_wake(
        _cfg(tmp_path), 10_000.0,
        data_dir=str(tmp_path),
        runner=Boom(),
        env={"OVERSEER_WAKE_CMD": "agent"},
        build_report=_br(_report(stuck_bots=["SinFermera7"]), 1),
    )
    assert result["decision"] == "error"
    # The flock is released even on the exception path (the fd is closed in
    # finally); a follow-up run can acquire and wake.
    follow, frun = _run(tmp_path, report=_report(alive=False, healthy=False),
                        code=1, env={"OVERSEER_WAKE_CMD": "agent"})
    assert follow["decision"] == "woke" and frun.invoked


# --------------------------------------------------------------------------- #
# Wake log: line format + truncation at the cap

def test_wake_log_line_format(tmp_path):
    runner = FakeRunner(rc=0, elapsed=1.5)
    _run(tmp_path, report=_report(stuck_bots=["SinFermera7"]), code=1,
         runner=runner, env={"OVERSEER_WAKE_CMD": "agent"})
    line = _log_text(str(tmp_path)).strip().splitlines()[-1]
    # ts decision reason bots rc elapsed_s
    assert "woke" in line
    assert "SinFermera7" in line
    assert "rc=0" in line


def test_wake_log_truncates_at_cap(tmp_path):
    logp = os.path.join(str(tmp_path), "overseer_wake.log")
    # Pre-fill well past the cap.
    with open(logp, "w", encoding="utf-8") as fh:
        fh.write("x" * (ow.LOG_MAX_BYTES + 500_000))
    ow._log_line(str(tmp_path), "ok", "healthy", "", "", "")
    size = os.path.getsize(logp)
    assert size <= ow.LOG_MAX_BYTES + 4096  # truncated to ~cap (+ the new line)


# --------------------------------------------------------------------------- #
# REAL process-group kill: the only test that spawns a subprocess.
# Launch a command whose child outlives the parent; on timeout the WHOLE group
# must die so no orphan survives.

def test_run_group_kills_whole_process_group_no_orphan(tmp_path):
    # `sh -c 'sleep 30 & echo $! >pidfile; sleep 30'` — the backgrounded child is
    # a separate process whose pid we capture; on timeout the wrapper killpg's the
    # group, so BOTH the shell and its backgrounded child must be gone.
    pidfile = tmp_path / "child.pid"
    argv = ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; sleep 30"]
    start = time.time()
    rc, elapsed = ow._run_group(argv, stdin_json="{}", timeout=1.0)
    took = time.time() - start
    # Returned promptly after the ~1s timeout (not after 30s).
    assert took < 10.0
    # The backgrounded child's pid was recorded; poll until it is gone.
    assert pidfile.exists(), "child never wrote its pid"
    child_pid = int(pidfile.read_text().strip())
    assert _wait_dead(child_pid, timeout=5.0), (
        f"orphan child {child_pid} survived the group kill")
    # rc signals an abnormal (signalled) termination, not a clean 0.
    assert rc != 0


# --------------------------------------------------------------------------- #
# TRUE-concurrency single-flight (regression for B1): a fork-storm of N wrappers
# racing on the SAME data_dir with an URGENT report (bypasses cooldown) must
# produce EXACTLY ONE wake. FAILS on the old O_EXCL+PID-file lock (2-3 winners
# slip through the created-but-empty write gap); PASSES with fcntl.flock.

def test_forkstorm_exactly_one_wake(tmp_path):
    n = 20
    data_dir = str(tmp_path)
    marker = tmp_path / "wakes.log"   # each real wake appends one line here
    barrier = tmp_path / "go"         # children spin until this appears

    def _runner(argv, stdin_json, timeout):
        # Inside the flock-held critical section: mark, then hold briefly so a
        # wrongly-overlapping sibling would be caught by the len==1 assertion.
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        time.sleep(0.15)
        return 0, 0.15

    def _child():
        import scripts.overseer_wake as cow
        rep = _report(alive=False, healthy=False)   # urgent → no cooldown gate
        deadline = time.time() + 10.0               # barrier-spin to race tightly
        while not barrier.exists() and time.time() < deadline:
            time.sleep(0.001)
        try:
            cow.run_wake(_cfg(tmp_path), 10_000.0, data_dir=data_dir,
                         runner=_runner, env={"OVERSEER_WAKE_CMD": "agent"},
                         build_report=_br(rep, 1))
        finally:
            os._exit(0)   # hard-exit; skip pytest teardown/atexit in the child

    pids = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:
            _child()     # never returns (os._exit)
        pids.append(pid)

    # Release all children at once, then reap them (bounded).
    barrier.write_text("go")
    deadline = time.time() + 20.0
    for pid in pids:
        while True:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                break
            if time.time() > deadline:
                # Last resort: don't hang the suite; signal + reap.
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
                break
            time.sleep(0.01)

    wakes = marker.read_text().splitlines() if marker.exists() else []
    assert len(wakes) == 1, (
        f"expected exactly ONE wake across {n} racing wrappers, got "
        f"{len(wakes)}: {wakes}")


# --------------------------------------------------------------------------- #
# small local utilities

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_dead(pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.02)
    return not _pid_alive(pid)
