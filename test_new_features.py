"""Unit tests for recent fixes: list handling, cancel_recurring_series,
and snooze-relative time calculation.

Follows the same mocking pattern as test_recurring.py.
"""

import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

# Mock telegram and its submodules before importing bot
_context_types = MagicMock()
_context_types.DEFAULT_TYPE = type

telegram_mock = types.ModuleType("telegram")
telegram_mock.InlineKeyboardButton = MagicMock
telegram_mock.InlineKeyboardMarkup = MagicMock
telegram_mock.Update = MagicMock
telegram_ext_mock = types.ModuleType("telegram.ext")
telegram_ext_mock.Application = MagicMock
telegram_ext_mock.CallbackQueryHandler = MagicMock
telegram_ext_mock.CommandHandler = MagicMock
telegram_ext_mock.ContextTypes = _context_types
telegram_ext_mock.MessageHandler = MagicMock
telegram_ext_mock.filters = MagicMock

sys.modules["telegram"] = telegram_mock
sys.modules["telegram.ext"] = telegram_ext_mock

from bot import LLMProvider, ReminderBot, ReminderDB  # noqa: E402

TZ = ZoneInfo("Asia/Singapore")


@pytest.fixture
def db():
    """Create a fresh ReminderDB with a temp file for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = ReminderDB(db_path=path)
    yield database
    try:
        os.unlink(path)
    except PermissionError:
        pass


# ─── Helper: task_list normalization (extracted from message handler) ─────────

def _normalize_task_list(result: dict, user_input: str = "fallback input") -> list[str]:
    """Replicate the task_list normalization logic from the message handler."""
    task = result.get("task")
    tasks = result.get("tasks")
    if tasks and isinstance(tasks, list):
        task_list = [str(t).strip() for t in tasks if t and str(t).strip()]
        if not task_list:
            task_list = [task or user_input]
    else:
        task_list = [task or user_input]
    return task_list


# ─── Fix 1: List Handling (task_list normalization) ──────────────────────────

class TestTaskListNormalization:
    def test_tasks_array_multiple(self):
        """LLM returns tasks=["A", "B"] -> 2-item list."""
        result = {
            "action": "create",
            "task": None,
            "tasks": ["A", "B"],
            "category": "general",
            "scheduled_time": "2026-04-12T09:00:00",
        }
        task_list = _normalize_task_list(result)
        assert task_list == ["A", "B"]

    def test_single_task_backward_compat(self):
        """LLM returns task="buy milk", tasks=null -> 1-item list."""
        result = {
            "task": "buy milk",
            "tasks": None,
        }
        task_list = _normalize_task_list(result)
        assert task_list == ["buy milk"]

    def test_tasks_array_single_item(self):
        """LLM returns tasks=["only one"] -> 1-item list."""
        result = {
            "task": None,
            "tasks": ["only one"],
        }
        task_list = _normalize_task_list(result)
        assert task_list == ["only one"]

    def test_tasks_empty_array_falls_back_to_task(self):
        """tasks=[] should fall back to the task field."""
        result = {
            "task": "fallback task",
            "tasks": [],
        }
        task_list = _normalize_task_list(result)
        assert task_list == ["fallback task"]

    def test_tasks_empty_array_no_task_falls_back_to_input(self):
        """tasks=[] and task=None should fall back to user_input."""
        result = {
            "task": None,
            "tasks": [],
        }
        task_list = _normalize_task_list(result, user_input="raw text")
        assert task_list == ["raw text"]

    def test_tasks_with_whitespace_stripped(self):
        """Whitespace-only entries in tasks are filtered out."""
        result = {
            "task": None,
            "tasks": ["A", "  ", "B", ""],
        }
        task_list = _normalize_task_list(result)
        assert task_list == ["A", "B"]

    def test_no_task_no_tasks_uses_fallback(self):
        """Neither task nor tasks -> falls back to user_input."""
        result = {}
        task_list = _normalize_task_list(result, user_input="the raw message")
        assert task_list == ["the raw message"]

    def test_tasks_array_creates_multiple_db_rows(self, db):
        """End-to-end: multiple tasks all get inserted into DB."""
        tasks = ["Buy eggs", "Buy milk", "Buy bread"]
        scheduled_time = datetime(2026, 4, 12, 9, 0, tzinfo=TZ)
        ids = []
        for t in tasks:
            rid = db.add_reminder(
                task=t, category="general",
                scheduled_time=scheduled_time, created_by=1,
            )
            ids.append(rid)
        assert len(ids) == 3
        for rid, expected_task in zip(ids, tasks):
            r = db.get_reminder(rid)
            assert r["task"] == expected_task
            assert r["scheduled_time"] == scheduled_time.isoformat()


# ─── Fix 2: cancel_recurring_series ──────────────────────────────────────────

class TestCancelRecurringSeries:
    def test_cancel_from_parent(self, db):
        """Cancel via parent ID: parent + pending children all cancelled."""
        parent_id = db.add_reminder(
            task="vitamins", category="general",
            scheduled_time=datetime(2026, 4, 1, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="weekly",
        )
        child1 = db.add_reminder(
            task="vitamins", category="general",
            scheduled_time=datetime(2026, 4, 8, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="weekly",
            recurrence_parent_id=parent_id,
        )
        child2 = db.add_reminder(
            task="vitamins", category="general",
            scheduled_time=datetime(2026, 4, 15, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="weekly",
            recurrence_parent_id=parent_id,
        )
        success, count = db.cancel_recurring_series(parent_id)
        assert success is True
        assert count == 3  # parent + 2 children, all pending

        # All should have recurrence_pattern cleared
        for rid in [parent_id, child1, child2]:
            r = db.get_reminder(rid)
            assert r["recurrence_pattern"] is None
            assert r["status"] == "cancelled"

    def test_cancel_from_child(self, db):
        """Cancel via child ID -> finds root and cancels whole series."""
        parent_id = db.add_reminder(
            task="exercise", category="general",
            scheduled_time=datetime(2026, 4, 1, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="daily",
        )
        child_id = db.add_reminder(
            task="exercise", category="general",
            scheduled_time=datetime(2026, 4, 2, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="daily",
            recurrence_parent_id=parent_id,
        )
        success, count = db.cancel_recurring_series(child_id)
        assert success is True
        # Both parent (pending) and child (pending) should be cancelled
        assert count == 2
        assert db.get_reminder(parent_id)["recurrence_pattern"] is None
        assert db.get_reminder(child_id)["recurrence_pattern"] is None

    def test_cancel_nonexistent_id(self, db):
        """Non-existent ID -> (False, 0)."""
        success, count = db.cancel_recurring_series(99999)
        assert success is False
        assert count == 0

    def test_cancel_with_sent_parent(self, db):
        """Parent already sent, one child pending -> cancels the pending child."""
        parent_id = db.add_reminder(
            task="pills", category="general",
            scheduled_time=datetime(2026, 4, 1, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="daily",
        )
        # Simulate parent having been sent
        db.mark_sent(parent_id)

        child_id = db.add_reminder(
            task="pills", category="general",
            scheduled_time=datetime(2026, 4, 2, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="daily",
            recurrence_parent_id=parent_id,
        )
        success, count = db.cancel_recurring_series(parent_id)
        assert success is True
        assert count == 1  # only the pending child

        # Both should have pattern cleared
        assert db.get_reminder(parent_id)["recurrence_pattern"] is None
        assert db.get_reminder(child_id)["recurrence_pattern"] is None
        # Parent stays sent, child is cancelled
        assert db.get_reminder(parent_id)["status"] == "sent"
        assert db.get_reminder(child_id)["status"] == "cancelled"

    def test_cancel_idempotent(self, db):
        """Second call returns (True, 0) since no more pending."""
        parent_id = db.add_reminder(
            task="water plants", category="general",
            scheduled_time=datetime(2026, 4, 1, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="weekly",
        )
        child_id = db.add_reminder(
            task="water plants", category="general",
            scheduled_time=datetime(2026, 4, 8, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="weekly",
            recurrence_parent_id=parent_id,
        )
        db.cancel_recurring_series(parent_id)

        # Second call
        success, count = db.cancel_recurring_series(parent_id)
        assert success is True
        assert count == 0  # nothing left to cancel


# ─── Fix 3: Snooze Relative Time Calculation ─────────────────────────────────

class TestSnoozeRelativeTime:
    """Test the time calculation logic used by the snoozerel callback handler."""

    @staticmethod
    def _calc_snooze_time(
        scheduled_time_str: str,
        days: int,
        now: datetime,
        tz: ZoneInfo,
    ) -> datetime:
        """Replicate the snoozerel time calculation from button_callback."""
        orig_time = datetime.fromisoformat(scheduled_time_str)
        if orig_time.tzinfo is None:
            orig_time = orig_time.replace(tzinfo=tz)
        new_time = orig_time + timedelta(days=days)
        if new_time <= now:
            new_time = now + timedelta(days=days)
        return new_time

    def test_snooze_plus_1_day(self):
        """9am April 10 + 1d = 9am April 11."""
        now = datetime(2026, 4, 10, 15, 0, tzinfo=TZ)  # 3pm same day
        result = self._calc_snooze_time("2026-04-10T09:00:00", 1, now, TZ)
        assert result == datetime(2026, 4, 11, 9, 0, tzinfo=TZ)

    def test_snooze_plus_7_days(self):
        """9am April 10 + 7d = 9am April 17."""
        now = datetime(2026, 4, 10, 15, 0, tzinfo=TZ)
        result = self._calc_snooze_time("2026-04-10T09:00:00", 7, now, TZ)
        assert result == datetime(2026, 4, 17, 9, 0, tzinfo=TZ)

    def test_snooze_fallback_when_past(self):
        """If orig + 1d is in the past, falls back to now + 1d."""
        # Reminder was from April 5, now it's April 10 -> April 6 is in the past
        now = datetime(2026, 4, 10, 15, 0, tzinfo=TZ)
        result = self._calc_snooze_time("2026-04-05T09:00:00", 1, now, TZ)
        expected = now + timedelta(days=1)
        assert result == expected

    def test_snooze_exact_boundary_falls_back(self):
        """If orig + days == now exactly, still falls back (uses <= check)."""
        now = datetime(2026, 4, 11, 9, 0, tzinfo=TZ)
        result = self._calc_snooze_time("2026-04-10T09:00:00", 1, now, TZ)
        # orig + 1d = April 11 09:00 == now, so falls back to now + 1d
        expected = now + timedelta(days=1)
        assert result == expected

    def test_snooze_naive_timestamp_gets_tz(self):
        """A naive scheduled_time string gets the bot timezone applied."""
        now = datetime(2026, 4, 10, 15, 0, tzinfo=TZ)
        result = self._calc_snooze_time("2026-04-10T09:00:00", 1, now, TZ)
        assert result.tzinfo == TZ

    def test_snooze_aware_timestamp_preserved(self):
        """An already-aware scheduled_time preserves its timezone."""
        now = datetime(2026, 4, 10, 15, 0, tzinfo=TZ)
        result = self._calc_snooze_time("2026-04-10T09:00:00+08:00", 1, now, TZ)
        assert result == datetime(2026, 4, 11, 9, 0, tzinfo=TZ)


# ─── Fix 3 (issue #7): _restore_dropped_urls ─────────────────────────────────

class TestRestoreDroppedUrls:
    def test_missing_url_is_appended(self):
        """URL present in raw input but absent from the task gets appended."""
        user_input = "tmr: read this\nhttps://x.com/abc"
        task_list = ["read this"]
        result = ReminderBot._restore_dropped_urls(user_input, task_list)
        assert result == ["read this\nhttps://x.com/abc"]

    def test_url_already_present_unchanged(self):
        """If the LLM already preserved the URL, the list is untouched."""
        user_input = "tmr: read this\nhttps://x.com/abc"
        task_list = ["read this\nhttps://x.com/abc"]
        result = ReminderBot._restore_dropped_urls(user_input, task_list)
        assert result == task_list

    def test_two_missing_urls_both_appended_in_order(self):
        """Multiple missing URLs are appended in the order they appear."""
        user_input = "tmr: read these\nhttps://x.com/abc\nhttps://x.com/def"
        task_list = ["read these"]
        result = ReminderBot._restore_dropped_urls(user_input, task_list)
        assert result == ["read these\nhttps://x.com/abc\nhttps://x.com/def"]

    def test_no_urls_unchanged(self):
        """No URLs in the raw input -> task_list returned unchanged."""
        user_input = "call mom tomorrow at 3pm"
        task_list = ["call mom at 3pm"]
        result = ReminderBot._restore_dropped_urls(user_input, task_list)
        assert result == task_list

    def test_trailing_punctuation_not_reappended(self):
        """Input URL with trailing period still matches a clean URL in the task
        (rstrip of trailing punctuation avoids a spurious duplicate append)."""
        user_input = "read https://x.com/abc."
        task_list = ["read https://x.com/abc"]
        result = ReminderBot._restore_dropped_urls(user_input, task_list)
        assert result == task_list


# ─── Fix 4 (issue #9): LLMProvider retry/error helpers ───────────────────────

class TestFriendlyLlmError:
    def test_google_style_503_overloaded(self):
        """Google-style error body with status 503: message extracted, no braces,
        retry hint included."""
        body = (
            '{"error": {"message": "The model is overloaded. Please try again '
            'later.", "status": "UNAVAILABLE"}}'
        )
        msg = LLMProvider._friendly_llm_error(503, body)
        assert "503" in msg
        assert "The model is overloaded" in msg
        assert "retrying in a minute" in msg.lower()
        assert "{" not in msg

    def test_401_mentions_api_key(self):
        """Auth errors point the user at config.yaml regardless of body content."""
        msg = LLMProvider._friendly_llm_error(401, '{"error": "invalid api key"}')
        assert "API key" in msg

    def test_non_json_body_status_500(self):
        """Non-JSON body still produces a clean message with the status code."""
        msg = LLMProvider._friendly_llm_error(500, "Internal Server Error - upstream timeout")
        assert "500" in msg
        assert "{" not in msg
        assert "}" not in msg

    def test_status_none_network_failure(self):
        """status=None (pure network failure) uses the 'couldn't reach' phrasing."""
        msg = LLMProvider._friendly_llm_error(None, "Connection refused")
        assert "Couldn't reach" in msg


class TestRetryableStatuses:
    def test_retryable_statuses(self):
        assert 503 in LLMProvider.RETRYABLE_STATUSES
        assert 429 in LLMProvider.RETRYABLE_STATUSES

    def test_non_retryable_statuses(self):
        assert 400 not in LLMProvider.RETRYABLE_STATUSES
        assert 401 not in LLMProvider.RETRYABLE_STATUSES
