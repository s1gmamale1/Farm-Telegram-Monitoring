"""Tests for watcherdog.task_store — persistent resumable task records."""

from __future__ import annotations

from watcherdog import task_store


def test_start_update_finish_roundtrip(tmp_path):
    p = str(tmp_path / "tasks.json")
    assert task_store.active(p) == []
    tid = task_store.start(p, chat_id=-100, user_id=42, request="close all accounts")
    assert isinstance(tid, int)
    act = task_store.active(p)
    assert len(act) == 1
    assert act[0]["request"] == "close all accounts"
    assert act[0]["status"] == "in_progress"
    task_store.update(p, tid, "pressing 'Stop' on SinFermera3")
    task_store.update(p, tid, "pressing 'Stop' on SinFermera4")
    assert task_store.active(p)[0]["progress"][-1] == "pressing 'Stop' on SinFermera4"
    task_store.finish(p, tid)
    assert task_store.active(p) == []


def test_ids_are_monotonic(tmp_path):
    p = str(tmp_path / "tasks.json")
    a = task_store.start(p, -1, 1, "a")
    task_store.finish(p, a)
    b = task_store.start(p, -1, 1, "b")
    assert b > a  # seq keeps climbing even after removal


def test_progress_is_capped(tmp_path):
    p = str(tmp_path / "tasks.json")
    tid = task_store.start(p, -1, 1, "x")
    for i in range(50):
        task_store.update(p, tid, f"step {i}")
    prog = task_store.active(p)[0]["progress"]
    assert len(prog) == task_store._MAX_PROGRESS
    assert prog[-1] == "step 49"  # newest kept


def test_bump_resume_counts(tmp_path):
    p = str(tmp_path / "tasks.json")
    tid = task_store.start(p, -1, 1, "x")
    assert task_store.bump_resume(p, tid) == 1
    assert task_store.bump_resume(p, tid) == 2
    assert task_store.active(p)[0]["resume_count"] == 2


def test_missing_file_is_empty(tmp_path):
    assert task_store.active(str(tmp_path / "nope.json")) == []
