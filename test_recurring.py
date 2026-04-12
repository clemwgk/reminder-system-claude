"""Unit tests for recurring reminders feature.

Tests the DB layer, collapse logic, count/end-date enforcement,
and /setrecurrence argument parsing — all without Telegram or LLM.
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
# Need to mock enough for the class definitions to load
_context_types = MagicMock()
_context_types.DEFAULT_TYPE = type  # needs to be a valid type annotation

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

from bot import ReminderBot, ReminderDB  # noqa: E402

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


# ─── 1. Schema Migration ───────────────────────────────────────────────────

class TestSchemaMigration:
    def test_new_columns_exist(self, db):
        """Verify recurrence_end_date and recurrence_remaining columns are created."""
        r_id = db.add_reminder(
            task="test",
            category="general",
            scheduled_time=datetime.now(TZ),
            created_by=1,
            recurrence_end_date="2026-12-31",
            recurrence_remaining=5,
        )
        reminder = db.get_reminder(r_id)
        assert reminder["recurrence_end_date"] == "2026-12-31"
        assert reminder["recurrence_remaining"] == 5

    def test_columns_default_to_null(self, db):
        """New columns should be NULL by default."""
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
        )
        reminder = db.get_reminder(r_id)
        assert reminder["recurrence_end_date"] is None
        assert reminder["recurrence_remaining"] is None


# ─── 2. add_reminder() with New Fields ─────────────────────────────────────

class TestAddReminder:
    def test_add_with_count(self, db):
        r_id = db.add_reminder(
            task="vitamins", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="daily", recurrence_remaining=4,
        )
        r = db.get_reminder(r_id)
        assert r["recurrence_pattern"] == "daily"
        assert r["recurrence_remaining"] == 4

    def test_add_with_end_date(self, db):
        r_id = db.add_reminder(
            task="exercise", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="weekly", recurrence_end_date="2026-12-31",
        )
        r = db.get_reminder(r_id)
        assert r["recurrence_pattern"] == "weekly"
        assert r["recurrence_end_date"] == "2026-12-31"

    def test_add_with_both(self, db):
        r_id = db.add_reminder(
            task="both", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="monthly",
            recurrence_remaining=3,
            recurrence_end_date="2027-06-30",
        )
        r = db.get_reminder(r_id)
        assert r["recurrence_remaining"] == 3
        assert r["recurrence_end_date"] == "2027-06-30"


# ─── 3. set_recurrence() ───────────────────────────────────────────────────

class TestSetRecurrence:
    def test_convert_onetime_to_recurring(self, db):
        """Convert a non-recurring reminder to recurring."""
        r_id = db.add_reminder(
            task="water plants", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
        )
        assert db.get_reminder(r_id)["recurrence_pattern"] is None

        db.set_recurrence(r_id, pattern="weekly")
        r = db.get_reminder(r_id)
        assert r["recurrence_pattern"] == "weekly"
        assert r["recurrence_parent_id"] is None  # should be root

    def test_change_cadence(self, db):
        """Change pattern from weekly to monthly."""
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="weekly",
        )
        db.set_recurrence(r_id, pattern="monthly", remaining=6)
        r = db.get_reminder(r_id)
        assert r["recurrence_pattern"] == "monthly"
        assert r["recurrence_remaining"] == 6

    def test_turn_off_recurrence(self, db):
        """Turn off recurrence (pattern=None)."""
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="daily", recurrence_remaining=3,
            recurrence_end_date="2026-12-31",
        )
        db.set_recurrence(r_id, pattern=None)
        r = db.get_reminder(r_id)
        assert r["recurrence_pattern"] is None
        assert r["recurrence_end_date"] is None
        assert r["recurrence_remaining"] is None

    def test_set_with_end_date(self, db):
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
        )
        db.set_recurrence(r_id, pattern="daily", end_date="2026-06-30")
        r = db.get_reminder(r_id)
        assert r["recurrence_pattern"] == "daily"
        assert r["recurrence_end_date"] == "2026-06-30"

    def test_clears_parent_id(self, db):
        """Setting recurrence should clear recurrence_parent_id (becomes root)."""
        r_id = db.add_reminder(
            task="child", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="daily", recurrence_parent_id=99,
        )
        assert db.get_reminder(r_id)["recurrence_parent_id"] == 99

        db.set_recurrence(r_id, pattern="weekly")
        assert db.get_reminder(r_id)["recurrence_parent_id"] is None


# ─── 4. /list Collapse Logic ───────────────────────────────────────────────

class TestCollapseRecurring:
    def test_non_recurring_pass_through(self):
        """Non-recurring reminders should not be filtered out."""
        reminders = [
            {"id": 1, "task": "a", "scheduled_time": "2026-03-21T09:00:00", "recurrence_pattern": None},
            {"id": 2, "task": "b", "scheduled_time": "2026-03-22T09:00:00", "recurrence_pattern": None},
        ]
        result = ReminderBot._collapse_recurring(reminders)
        assert len(result) == 2

    def test_collapse_series_to_one(self):
        """Multiple pending children of same series → keep only earliest."""
        reminders = [
            {"id": 10, "task": "vitamins", "scheduled_time": "2026-03-21T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": None,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 11, "task": "vitamins", "scheduled_time": "2026-03-22T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": 10,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 12, "task": "vitamins", "scheduled_time": "2026-03-23T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": 10,
             "recurrence_remaining": None, "recurrence_end_date": None},
        ]
        result = ReminderBot._collapse_recurring(reminders)
        recurring = [r for r in result if r.get("recurrence_pattern")]
        assert len(recurring) == 1
        assert recurring[0]["id"] == 10  # earliest

    def test_mixed_recurring_and_non_recurring(self):
        """Non-recurring reminders preserved alongside collapsed recurring."""
        reminders = [
            {"id": 1, "task": "groceries", "scheduled_time": "2026-03-20T09:00:00", "recurrence_pattern": None},
            {"id": 10, "task": "vitamins", "scheduled_time": "2026-03-21T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": None,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 11, "task": "vitamins", "scheduled_time": "2026-03-22T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": 10,
             "recurrence_remaining": None, "recurrence_end_date": None},
        ]
        result = ReminderBot._collapse_recurring(reminders)
        assert len(result) == 2  # 1 non-recurring + 1 collapsed recurring

    def test_multiple_series_collapsed_separately(self):
        """Two different recurring series → each collapses to 1 entry."""
        reminders = [
            {"id": 10, "task": "vitamins", "scheduled_time": "2026-03-21T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": None,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 11, "task": "vitamins", "scheduled_time": "2026-03-22T09:00:00",
             "recurrence_pattern": "daily", "recurrence_parent_id": 10,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 20, "task": "plants", "scheduled_time": "2026-03-24T09:00:00",
             "recurrence_pattern": "weekly", "recurrence_parent_id": None,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 21, "task": "plants", "scheduled_time": "2026-03-31T09:00:00",
             "recurrence_pattern": "weekly", "recurrence_parent_id": 20,
             "recurrence_remaining": None, "recurrence_end_date": None},
        ]
        result = ReminderBot._collapse_recurring(reminders)
        assert len(result) == 2

    def test_result_sorted_by_time(self):
        """Collapsed results should be sorted by scheduled_time."""
        reminders = [
            {"id": 20, "task": "plants", "scheduled_time": "2026-03-25T09:00:00",
             "recurrence_pattern": "weekly", "recurrence_parent_id": None,
             "recurrence_remaining": None, "recurrence_end_date": None},
            {"id": 1, "task": "groceries", "scheduled_time": "2026-03-20T09:00:00", "recurrence_pattern": None},
        ]
        result = ReminderBot._collapse_recurring(reminders)
        assert result[0]["id"] == 1
        assert result[1]["id"] == 20


# ─── 5. Count Decrement Logic ──────────────────────────────────────────────

class TestCountDecrement:
    """Test the logic that would run in send_due_reminders for count-based recurrence."""

    def _simulate_fire(self, db, reminder_id, tz):
        """Simulate what send_due_reminders does for recurring reminders."""
        reminder = db.get_reminder(reminder_id)
        recurrence_pattern = reminder.get("recurrence_pattern")
        if not recurrence_pattern:
            return None

        recurrence_remaining = reminder.get("recurrence_remaining")
        should_create_next = True

        if recurrence_remaining is not None and recurrence_remaining <= 1:
            should_create_next = False

        scheduled_time = datetime.fromisoformat(reminder["scheduled_time"])
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=tz)

        # Simple next time calculation for testing
        next_time = scheduled_time + timedelta(days=1)

        recurrence_end_date = reminder.get("recurrence_end_date")
        if recurrence_end_date and next_time:
            end_dt = datetime.strptime(recurrence_end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=tz
            )
            if next_time > end_dt:
                should_create_next = False

        # Mark as sent
        db.mark_sent(reminder_id)

        if next_time and should_create_next:
            parent_id = reminder.get("recurrence_parent_id") or reminder["id"]
            next_remaining = None
            if recurrence_remaining is not None:
                next_remaining = recurrence_remaining - 1
            new_id = db.add_reminder(
                task=reminder["task"],
                category=reminder["category"],
                scheduled_time=next_time,
                created_by=reminder["created_by"],
                recurrence_pattern=recurrence_pattern,
                recurrence_parent_id=parent_id,
                recurrence_end_date=recurrence_end_date,
                recurrence_remaining=next_remaining,
            )
            return new_id
        return None

    def test_count_decrements(self, db):
        """remaining=3 → fire → child has remaining=2."""
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="daily", recurrence_remaining=3,
        )
        child_id = self._simulate_fire(db, r_id, TZ)
        assert child_id is not None
        child = db.get_reminder(child_id)
        assert child["recurrence_remaining"] == 2

    def test_count_stops_at_one(self, db):
        """remaining=1 → fire → no child created (last occurrence)."""
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="daily", recurrence_remaining=1,
        )
        child_id = self._simulate_fire(db, r_id, TZ)
        assert child_id is None

    def test_infinite_always_creates(self, db):
        """remaining=None → fire → child created with remaining=None."""
        r_id = db.add_reminder(
            task="test", category="general",
            scheduled_time=datetime.now(TZ), created_by=1,
            recurrence_pattern="daily",
        )
        child_id = self._simulate_fire(db, r_id, TZ)
        assert child_id is not None
        child = db.get_reminder(child_id)
        assert child["recurrence_remaining"] is None


# ─── 6. End Date Boundary ──────────────────────────────────────────────────

class TestEndDateBoundary:
    def _simulate_fire(self, db, reminder_id, tz):
        """Same simulation as TestCountDecrement."""
        reminder = db.get_reminder(reminder_id)
        recurrence_pattern = reminder.get("recurrence_pattern")
        if not recurrence_pattern:
            return None

        recurrence_remaining = reminder.get("recurrence_remaining")
        should_create_next = True
        if recurrence_remaining is not None and recurrence_remaining <= 1:
            should_create_next = False

        scheduled_time = datetime.fromisoformat(reminder["scheduled_time"])
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=tz)
        next_time = scheduled_time + timedelta(days=1)

        recurrence_end_date = reminder.get("recurrence_end_date")
        if recurrence_end_date and next_time:
            end_dt = datetime.strptime(recurrence_end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=tz
            )
            if next_time > end_dt:
                should_create_next = False

        db.mark_sent(reminder_id)

        if next_time and should_create_next:
            parent_id = reminder.get("recurrence_parent_id") or reminder["id"]
            next_remaining = None
            if recurrence_remaining is not None:
                next_remaining = recurrence_remaining - 1
            new_id = db.add_reminder(
                task=reminder["task"], category=reminder["category"],
                scheduled_time=next_time, created_by=reminder["created_by"],
                recurrence_pattern=recurrence_pattern,
                recurrence_parent_id=parent_id,
                recurrence_end_date=recurrence_end_date,
                recurrence_remaining=next_remaining,
            )
            return new_id
        return None

    def test_within_boundary_creates_child(self, db):
        """Next occurrence before end date → child created."""
        r_id = db.add_reminder(
            task="exercise", category="general",
            scheduled_time=datetime(2026, 3, 20, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="daily",
            recurrence_end_date="2026-03-25",
        )
        child_id = self._simulate_fire(db, r_id, TZ)
        assert child_id is not None
        child = db.get_reminder(child_id)
        assert child["recurrence_end_date"] == "2026-03-25"

    def test_past_boundary_no_child(self, db):
        """Next occurrence after end date → no child created."""
        r_id = db.add_reminder(
            task="exercise", category="general",
            scheduled_time=datetime(2026, 3, 25, 9, 0, tzinfo=TZ),
            created_by=1,
            recurrence_pattern="daily",
            recurrence_end_date="2026-03-25",
        )
        child_id = self._simulate_fire(db, r_id, TZ)
        assert child_id is None


# ─── 7. _format_recurrence_info ─────────────────────────────────────────────

class TestFormatRecurrenceInfo:
    def test_no_pattern(self):
        assert ReminderBot._format_recurrence_info({"recurrence_pattern": None}) == ""

    def test_pattern_only(self):
        r = {"recurrence_pattern": "daily", "recurrence_remaining": None, "recurrence_end_date": None}
        assert ReminderBot._format_recurrence_info(r) == " 🔄 Daily"

    def test_with_remaining(self):
        r = {"recurrence_pattern": "weekly", "recurrence_remaining": 3, "recurrence_end_date": None}
        result = ReminderBot._format_recurrence_info(r)
        assert "Weekly" in result
        assert "3 remaining" in result

    def test_with_end_date(self):
        r = {"recurrence_pattern": "monthly", "recurrence_remaining": None, "recurrence_end_date": "2026-12-31"}
        result = ReminderBot._format_recurrence_info(r)
        assert "Monthly" in result
        assert "until 31 Dec 2026" in result


# ─── 8. _format_recurrence_confirmation ──────────────────────────────────────

class TestFormatRecurrenceConfirmation:
    def test_no_pattern(self):
        assert ReminderBot._format_recurrence_confirmation(None) == ""

    def test_pattern_only(self):
        result = ReminderBot._format_recurrence_confirmation("daily")
        assert "daily" in result
        assert "🔄" in result

    def test_with_count(self):
        result = ReminderBot._format_recurrence_confirmation("weekly", count=4)
        assert "4 times" in result

    def test_with_end_date(self):
        result = ReminderBot._format_recurrence_confirmation("monthly", end_date="2026-12-31")
        assert "until 31 Dec 2026" in result

    def test_with_both(self):
        result = ReminderBot._format_recurrence_confirmation("daily", count=5, end_date="2026-06-30")
        assert "5 times" in result
        assert "until 30 Jun 2026" in result


# ─── 9. /setrecurrence Argument Parsing ──────────────────────────────────────

class TestSetRecurrenceArgParsing:
    """Test the argument parsing logic extracted from the command handler.

    We test the parsing logic directly since we can't easily instantiate
    the full Telegram command handler.
    """

    @staticmethod
    def _parse_args(args: list[str]) -> dict:
        """Extract the parsing logic from set_recurrence_command."""
        import re

        VALID_PATTERNS = {"daily", "weekly", "biweekly", "monthly", "yearly", "weekdays", "weekends"}

        if len(args) < 2:
            return {"error": "not enough args"}

        try:
            reminder_id = int(args[0])
        except ValueError:
            return {"error": "invalid id"}

        pattern_arg = args[1].lower()

        if pattern_arg == "off":
            return {"id": reminder_id, "pattern": None, "end_date": None, "remaining": None}

        if pattern_arg not in VALID_PATTERNS:
            return {"error": f"unknown pattern: {pattern_arg}"}

        remaining_args = " ".join(args[2:]).lower()
        end_date = None
        remaining_count = None

        times_match = re.search(r'(\d+)\s*times?', remaining_args)
        if times_match:
            remaining_count = int(times_match.group(1))

        until_match = re.search(r'until\s+(.+)', remaining_args)
        if until_match:
            date_str = until_match.group(1).strip()
            date_str = re.sub(r'\d+\s*times?\s*', '', date_str).strip()
            try:
                parsed = datetime.strptime(date_str, "%Y-%m-%d")
                end_date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    parsed = datetime.strptime(date_str, "%d/%m/%Y")
                    end_date = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    end_of_match = re.match(r'end\s+of\s+(\d{4})', date_str)
                    if end_of_match:
                        end_date = f"{end_of_match.group(1)}-12-31"

        return {
            "id": reminder_id,
            "pattern": pattern_arg,
            "end_date": end_date,
            "remaining": remaining_count,
        }

    def test_simple_pattern(self):
        result = self._parse_args(["42", "weekly"])
        assert result == {"id": 42, "pattern": "weekly", "end_date": None, "remaining": None}

    def test_pattern_with_count(self):
        result = self._parse_args(["42", "monthly", "3", "times"])
        assert result == {"id": 42, "pattern": "monthly", "end_date": None, "remaining": 3}

    def test_pattern_with_end_date_iso(self):
        result = self._parse_args(["42", "daily", "until", "2026-12-31"])
        assert result == {"id": 42, "pattern": "daily", "end_date": "2026-12-31", "remaining": None}

    def test_pattern_with_end_date_dmy(self):
        result = self._parse_args(["42", "daily", "until", "31/12/2026"])
        assert result == {"id": 42, "pattern": "daily", "end_date": "2026-12-31", "remaining": None}

    def test_pattern_with_end_of_year(self):
        result = self._parse_args(["42", "weekly", "until", "end", "of", "2026"])
        assert result == {"id": 42, "pattern": "weekly", "end_date": "2026-12-31", "remaining": None}

    def test_off(self):
        result = self._parse_args(["42", "off"])
        assert result == {"id": 42, "pattern": None, "end_date": None, "remaining": None}

    def test_invalid_pattern(self):
        result = self._parse_args(["42", "hourly"])
        assert "error" in result

    def test_count_and_end_date(self):
        result = self._parse_args(["42", "weekly", "4", "times", "until", "2026-12-31"])
        assert result["remaining"] == 4
        assert result["end_date"] == "2026-12-31"
