#!/usr/bin/env python3
"""
Reminder Bot - A simple, privacy-conscious reminder system via Telegram.

Architecture designed for easy migration:
- LLM: Gemini (cloud) → Ollama (local)
- Hosting: Oracle Cloud → Raspberry Pi

Run with: python bot.py
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Copy config.example.yaml to config.yaml and fill in your values."
        )
    with open(path) as f:
        return yaml.safe_load(f)


# =============================================================================
# LLM ABSTRACTION LAYER (Swappable: Gemini ↔ Ollama)
# =============================================================================

class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def parse_reminder(
        self, user_input: str, current_time: datetime, recent_reminders: list[dict] = None
    ) -> dict:
        """
        Parse natural language reminder input.

        Returns dict with:
            - action: str (create, cancel, modify, duplicate, list)
            - task: str (what to remind about, for create/modify)
            - category: str (general, bill, food_expiry)
            - scheduled_time: datetime or None
            - time_hint: str (original time reference from user)
            - target_id: int or None (for cancel/modify - ID of reminder to act on)
            - shared: bool (True if "remind us" / notify all users)
            - error: str or None (if parsing failed)
        """
        pass

    def _build_prompt(
        self, user_input: str, current_time: datetime, recent_reminders: list[dict] = None
    ) -> str:
        """Build the common prompt for all providers."""
        recent_context = ""
        if recent_reminders:
            recent_lines = []
            for r in recent_reminders[:5]:  # Last 5 reminders
                recent_lines.append(f"  - ID {r['id']}: \"{r['task']}\" ({r['category']}) at {r['scheduled_time']}")
            recent_context = f"\n\nRecent reminders (for context):\n" + "\n".join(recent_lines)

        return f"""You are a reminder parsing assistant. Parse the user's message and determine what action they want to take.

Current date and time: {current_time.strftime('%Y-%m-%d %H:%M:%S %A')} (Singapore Time){recent_context}

User input: "{user_input}"

Respond with ONLY a JSON object (no markdown, no explanation) with these fields:
- "action": one of "create", "cancel", "modify", "duplicate", or "list"
- "task": the thing to be reminded about (string, required for create/modify, null for others)
- "category": one of "general", "bill", or "food_expiry" (for create/modify)
- "scheduled_time": ISO format datetime if a specific time was mentioned, or null
- "time_hint": the original time reference from the user, or null
- "target_id": the ID of an existing reminder to cancel/modify/duplicate, or null
- "shared": true if the user said "remind us" or wants to notify all users, false otherwise

ACTION DETECTION RULES (VERY IMPORTANT):
- "create": User wants a NEW reminder (e.g., "remind me to...", "set a reminder for...")
- "cancel": User wants to REMOVE/DELETE a reminder (e.g., "remove that", "cancel reminder 5", "delete the last one")
- "modify": User wants to CHANGE an existing reminder (e.g., "change reminder 3 to 5pm", "update the last one")
- "duplicate": User wants to COPY an existing reminder (e.g., "set one more of the same", "duplicate that", "another one like the last")
- "list": User wants to SEE their reminders (e.g., "show my reminders", "what reminders do I have")

TARGET IDENTIFICATION:
- If user says "the last one", "that one", "that reminder", "the previous one" → use the most recent reminder ID from context
- If user gives an ID number (e.g., "reminder 5", "cancel 3") → use that ID
- For duplicate: copy the task/category from the target, but use any new time specified

SHARED REMINDERS:
- If user says "remind us", "we need to", "remind both of us" → set shared: true
- Default is shared: false (only notify the person who created it)

Rules for category detection:
- "bill": anything related to payments, bills, subscriptions, dues, invoices
- "food_expiry": anything about food going bad, expiring, use by dates
- "general": everything else

Rules for scheduled_time:
- "tomorrow" = next day at 09:00
- "tomorrow morning" = next day at 09:00
- "tomorrow evening" = next day at 18:00
- "next week" = same day next week at 09:00
- "in X hours/minutes" = current time + X
- If only a time like "3pm" is given, assume today if it's still before that time, otherwise tomorrow
- If no time mentioned at all, set to null (the system will apply category defaults)

Example responses:
{{"action": "create", "task": "pay electricity bill", "category": "bill", "scheduled_time": null, "time_hint": null, "target_id": null, "shared": false}}
{{"action": "cancel", "task": null, "category": null, "scheduled_time": null, "time_hint": null, "target_id": 5, "shared": false}}
{{"action": "duplicate", "task": "check milk expiry", "category": "food_expiry", "scheduled_time": "2024-01-15T19:30:00", "time_hint": "7:30pm", "target_id": 4, "shared": false}}
"""


class GeminiProvider(LLMProvider):
    """Google Gemini API provider (free tier)."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    async def parse_reminder(
        self, user_input: str, current_time: datetime, recent_reminders: list[dict] = None
    ) -> dict:
        import aiohttp

        prompt = self._build_prompt(user_input, current_time, recent_reminders)
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 512,
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        return {"error": f"Gemini API error: {error_text}"}

                    data = await response.json()

            # Extract the text response
            text = data["candidates"][0]["content"]["parts"][0]["text"]

            # Clean up potential markdown code blocks
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]  # Remove first line
                text = text.rsplit("```", 1)[0]  # Remove last ```

            result = json.loads(text)
            return result

        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse LLM response: {e}"}
        except Exception as e:
            return {"error": f"LLM request failed: {e}"}


class OllamaProvider(LLMProvider):
    """Local Ollama provider (for Raspberry Pi / privacy)."""

    def __init__(self, host: str = "http://localhost:11434", model: str = "llama3.2"):
        self.host = host
        self.model = model

    async def parse_reminder(
        self, user_input: str, current_time: datetime, recent_reminders: list[dict] = None
    ) -> dict:
        import aiohttp

        prompt = self._build_prompt(user_input, current_time, recent_reminders)
        url = f"{self.host}/api/generate"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        return {"error": f"Ollama API error: {error_text}"}

                    data = await response.json()

            text = data.get("response", "").strip()

            # Clean up potential markdown code blocks
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]

            result = json.loads(text)
            return result

        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse LLM response: {e}"}
        except Exception as e:
            return {"error": f"LLM request failed: {e}"}


class OpenAIProvider(LLMProvider):
    """OpenAI API provider (gpt-4o-mini is very cheap)."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.openai.com/v1"

    async def parse_reminder(
        self, user_input: str, current_time: datetime, recent_reminders: list[dict] = None
    ) -> dict:
        import aiohttp

        prompt = self._build_prompt(user_input, current_time, recent_reminders)
        url = f"{self.base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 512,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        return {"error": f"OpenAI API error: {error_text}"}

                    data = await response.json()

            # Extract the text response
            text = data["choices"][0]["message"]["content"].strip()

            # Clean up potential markdown code blocks
            if text.startswith("```"):
                text = text.split("\n", 1)[1]  # Remove first line
                text = text.rsplit("```", 1)[0]  # Remove last ```

            result = json.loads(text)
            return result

        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse LLM response: {e}"}
        except Exception as e:
            return {"error": f"LLM request failed: {e}"}


class GroqProvider(LLMProvider):
    """Groq API provider (free tier with generous limits)."""

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.groq.com/openai/v1"

    async def parse_reminder(
        self, user_input: str, current_time: datetime, recent_reminders: list[dict] = None
    ) -> dict:
        import aiohttp

        prompt = self._build_prompt(user_input, current_time, recent_reminders)
        url = f"{self.base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 512,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        return {"error": f"Groq API error: {error_text}"}

                    data = await response.json()

            # Extract the text response (OpenAI-compatible format)
            text = data["choices"][0]["message"]["content"].strip()

            # Clean up potential markdown code blocks
            if text.startswith("```"):
                text = text.split("\n", 1)[1]  # Remove first line
                text = text.rsplit("```", 1)[0]  # Remove last ```

            result = json.loads(text)
            return result

        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse LLM response: {e}"}
        except Exception as e:
            return {"error": f"LLM request failed: {e}"}


def create_llm_provider(config: dict) -> LLMProvider:
    """Factory function to create the appropriate LLM provider."""
    provider_name = config["llm"]["provider"]

    if provider_name == "gemini":
        return GeminiProvider(
            api_key=config["llm"]["gemini"]["api_key"],
            model=config["llm"]["gemini"]["model"],
        )
    elif provider_name == "ollama":
        return OllamaProvider(
            host=config["llm"]["ollama"]["host"],
            model=config["llm"]["ollama"]["model"],
        )
    elif provider_name == "groq":
        return GroqProvider(
            api_key=config["llm"]["groq"]["api_key"],
            model=config["llm"]["groq"]["model"],
        )
    elif provider_name == "openai":
        return OpenAIProvider(
            api_key=config["llm"]["openai"]["api_key"],
            model=config["llm"]["openai"]["model"],
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")


# =============================================================================
# DATABASE LAYER
# =============================================================================

class ReminderDB:
    """SQLite database for storing reminders."""

    def __init__(self, db_path: str = "reminders.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    scheduled_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    sent_at TEXT,
                    notify_all INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status_time
                ON reminders (status, scheduled_time)
            """)
            # Add notify_all column if it doesn't exist (migration for existing DBs)
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN notify_all INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.commit()

    def add_reminder(
        self,
        task: str,
        category: str,
        scheduled_time: datetime,
        created_by: int,
        notify_all: bool = False,
    ) -> int:
        """Add a new reminder. Returns the reminder ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (task, category, scheduled_time, created_at, created_by, status, notify_all)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    task,
                    category,
                    scheduled_time.isoformat(),
                    datetime.now().isoformat(),
                    created_by,
                    1 if notify_all else 0,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_reminder(self, reminder_id: int) -> Optional[dict]:
        """Get a single reminder by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_recent_reminders(self, user_id: int = None, limit: int = 5) -> list[dict]:
        """Get recent reminders (for context in LLM parsing)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if user_id:
                rows = conn.execute(
                    """
                    SELECT * FROM reminders
                    WHERE created_by = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM reminders
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_pending_reminders(self) -> list[dict]:
        """Get all pending reminders."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'pending'
                ORDER BY scheduled_time ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_due_reminders(self, current_time: datetime) -> list[dict]:
        """Get reminders that are due (scheduled_time <= current_time)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'pending' AND scheduled_time <= ?
                ORDER BY scheduled_time ASC
                """,
                (current_time.isoformat(),),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_sent(self, reminder_id: int):
        """Mark a reminder as sent."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'sent', sent_at = ?
                WHERE id = ?
                """,
                (datetime.now().isoformat(), reminder_id),
            )
            conn.commit()

    def cancel_reminder(self, reminder_id: int) -> bool:
        """Cancel a pending reminder. Returns True if found and cancelled."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET status = 'cancelled'
                WHERE id = ? AND status = 'pending'
                """,
                (reminder_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delay_reminder(self, reminder_id: int, new_time: datetime) -> bool:
        """Delay a pending reminder to a new time. Returns True if successful."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET scheduled_time = ?
                WHERE id = ? AND status = 'pending'
                """,
                (new_time.isoformat(), reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def modify_reminder(
        self, reminder_id: int, new_time: datetime = None, new_task: str = None
    ) -> bool:
        """Modify a pending reminder. Returns True if successful."""
        updates = []
        params = []
        if new_time:
            updates.append("scheduled_time = ?")
            params.append(new_time.isoformat())
        if new_task:
            updates.append("task = ?")
            params.append(new_task)

        if not updates:
            return False

        params.append(reminder_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"""
                UPDATE reminders
                SET {', '.join(updates)}
                WHERE id = ? AND status = 'pending'
                """,
                params,
            )
            conn.commit()
            return cursor.rowcount > 0


# =============================================================================
# SCHEDULE RULES
# =============================================================================

class ScheduleResolver:
    """Applies default schedule rules based on reminder category."""

    def __init__(self, config: dict, timezone: ZoneInfo):
        self.defaults = config.get("schedule_defaults", {})
        self.tz = timezone

    def resolve_time(
        self,
        category: str,
        scheduled_time: Optional[datetime],
        current_time: datetime,
    ) -> datetime | list[datetime]:
        """
        Resolve the final scheduled time(s) for a reminder.

        If scheduled_time is provided, uses that.
        Otherwise, applies category-specific defaults.

        Returns a single datetime or list of datetimes (for multi-reminder categories like food_expiry).
        """
        if scheduled_time:
            # User specified a time, use it
            if isinstance(scheduled_time, str):
                scheduled_time = datetime.fromisoformat(scheduled_time)
            # Ensure timezone
            if scheduled_time.tzinfo is None:
                scheduled_time = scheduled_time.replace(tzinfo=self.tz)
            return scheduled_time

        # Apply category defaults
        if category == "bill":
            return self._next_saturday_morning(current_time)
        elif category == "food_expiry":
            # This is handled specially - returns multiple times
            return self._food_expiry_times(current_time)
        else:
            # General: next morning at default time
            return self._next_morning(current_time)

    def _next_morning(self, current_time: datetime) -> datetime:
        """Get next morning at default time."""
        default_time = self.defaults.get("general", {}).get("default_time", "09:00")
        hour, minute = map(int, default_time.split(":"))

        tomorrow = current_time + timedelta(days=1)
        return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _next_saturday_morning(self, current_time: datetime) -> datetime:
        """Get next Saturday morning."""
        bill_config = self.defaults.get("bill", {})
        target_day = bill_config.get("day", "saturday").lower()
        time_str = bill_config.get("time", "09:00")
        hour, minute = map(int, time_str.split(":"))

        days_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6
        }
        target_weekday = days_map.get(target_day, 5)  # Default Saturday

        days_ahead = target_weekday - current_time.weekday()
        if days_ahead <= 0:  # Target day already passed this week
            days_ahead += 7

        next_target = current_time + timedelta(days=days_ahead)
        return next_target.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _food_expiry_times(self, current_time: datetime) -> list[datetime]:
        """Get food expiry reminder times (day before, morning + evening)."""
        food_config = self.defaults.get("food_expiry", {})
        days_before = food_config.get("days_before", 1)
        times = food_config.get("times", ["09:00", "18:00"])

        reminder_day = current_time + timedelta(days=days_before)
        result = []

        for time_str in times:
            hour, minute = map(int, time_str.split(":"))
            reminder_time = reminder_day.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            result.append(reminder_time)

        return result


# =============================================================================
# TELEGRAM BOT
# =============================================================================

class ReminderBot:
    """Main Telegram bot class."""

    def __init__(self, config: dict):
        self.config = config
        self.tz = ZoneInfo(config.get("timezone", "Asia/Singapore"))
        self.authorized_users = set(config["telegram"]["authorized_users"])
        self.db = ReminderDB(config.get("database", {}).get("path", "reminders.db"))
        self.llm = create_llm_provider(config)
        self.scheduler = ScheduleResolver(config, self.tz)

        # Set up logging
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.INFO,
        )
        self.logger = logging.getLogger(__name__)

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        return user_id in self.authorized_users

    def _now(self) -> datetime:
        """Get current time in configured timezone."""
        return datetime.now(self.tz)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        user_id = update.effective_user.id

        if not self._is_authorized(user_id):
            await update.message.reply_text(
                f"You're not authorized to use this bot.\n"
                f"Your Telegram user ID is: {user_id}\n"
                f"Add this ID to config.yaml to authorize yourself."
            )
            return

        await update.message.reply_text(
            "Hello! I'm your reminder bot.\n\n"
            "Just tell me what to remind you about, like:\n"
            "• 'remind me to pay the electricity bill'\n"
            "• 'check if milk expires tomorrow'\n"
            "• 'call mom at 3pm'\n\n"
            "Commands:\n"
            "/list - Show all pending reminders\n"
            "/cancel <id> - Cancel a reminder\n"
            "/delay <id> <time> - Delay a reminder\n"
            "/help - Show this message"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        if not self._is_authorized(update.effective_user.id):
            return

        await update.message.reply_text(
            "Reminder Bot Help\n"
            "─────────────────\n\n"
            "Creating reminders:\n"
            "Just type naturally! Examples:\n"
            "• 'remind me to buy groceries tomorrow'\n"
            "• 'remind us to call the plumber' (notifies both)\n"
            "• 'pay credit card bill' (schedules for Saturday)\n"
            "• 'milk expires in 3 days' (reminds day before)\n\n"
            "Modifying reminders (natural language):\n"
            "• 'cancel that last reminder'\n"
            "• 'remove reminder 5'\n"
            "• 'duplicate that for 7pm'\n"
            "• 'change reminder 3 to tomorrow'\n\n"
            "Commands:\n"
            "/list - Show all pending reminders\n"
            "/cancel <id> - Cancel a reminder\n"
            "/delay <id> <hours> - Delay by X hours\n"
            "/snooze <id> [mins] - Snooze for 15 mins (or specify)\n"
            "/help - Show this message\n\n"
            "Categories & defaults:\n"
            "• Bills → Saturday 9 AM\n"
            "• Food expiry → Day before, 9 AM & 6 PM\n"
            "• General → Next day 9 AM\n\n"
            "Shared reminders:\n"
            "Say 'remind us' instead of 'remind me' to notify all users."
        )

    async def list_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command."""
        if not self._is_authorized(update.effective_user.id):
            return

        reminders = self.db.get_pending_reminders()

        if not reminders:
            await update.message.reply_text("No pending reminders.")
            return

        lines = ["Pending reminders:\n"]
        for r in reminders:
            scheduled = datetime.fromisoformat(r["scheduled_time"])
            time_str = scheduled.strftime("%a %d %b, %H:%M")
            lines.append(f"[{r['id']}] {r['task']}\n    📅 {time_str}")

        await update.message.reply_text("\n".join(lines))

    async def cancel_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command."""
        if not self._is_authorized(update.effective_user.id):
            return

        if not context.args:
            await update.message.reply_text("Usage: /cancel <reminder_id>")
            return

        try:
            reminder_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Invalid reminder ID. Use /list to see IDs.")
            return

        if self.db.cancel_reminder(reminder_id):
            await update.message.reply_text(f"Reminder [{reminder_id}] cancelled.")
        else:
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not found or already sent."
            )

    async def delay_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /delay command."""
        if not self._is_authorized(update.effective_user.id):
            return

        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /delay <reminder_id> <hours>\n"
                "Example: /delay 5 24  (delay reminder 5 by 24 hours)"
            )
            return

        try:
            reminder_id = int(context.args[0])
            hours = float(context.args[1])
        except ValueError:
            await update.message.reply_text("Invalid input. Use: /delay <id> <hours>")
            return

        # Get current reminder time and add delay
        reminders = self.db.get_pending_reminders()
        reminder = next((r for r in reminders if r["id"] == reminder_id), None)

        if not reminder:
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not found or already sent."
            )
            return

        current_time = datetime.fromisoformat(reminder["scheduled_time"])
        new_time = current_time + timedelta(hours=hours)

        if self.db.delay_reminder(reminder_id, new_time):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] delayed to {new_time.strftime('%a %d %b, %H:%M')}"
            )
        else:
            await update.message.reply_text("Failed to delay reminder.")

    async def snooze_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /snooze command - snooze for 15 minutes (or custom duration)."""
        if not self._is_authorized(update.effective_user.id):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /snooze <reminder_id> [minutes]\n"
                "Example: /snooze 5       (snooze 15 mins)\n"
                "Example: /snooze 5 30    (snooze 30 mins)"
            )
            return

        try:
            reminder_id = int(context.args[0])
            minutes = int(context.args[1]) if len(context.args) > 1 else 15
        except ValueError:
            await update.message.reply_text("Invalid input. Use: /snooze <id> [minutes]")
            return

        # Calculate new time from now (not from original scheduled time)
        new_time = self._now() + timedelta(minutes=minutes)

        if self.db.delay_reminder(reminder_id, new_time):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] snoozed for {minutes} minutes.\n"
                f"New time: {new_time.strftime('%H:%M')}"
            )
        else:
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not found or already sent."
            )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle natural language reminder input with smart action detection."""
        user_id = update.effective_user.id

        if not self._is_authorized(user_id):
            await update.message.reply_text(
                f"You're not authorized. Your ID: {user_id}"
            )
            return

        user_input = update.message.text.strip()
        user_input_lower = user_input.lower()
        current_time = self._now()

        # Get recent reminders for context
        recent_reminders = self.db.get_recent_reminders(user_id, limit=5)

        # KEYWORD FALLBACK: Detect obvious commands before calling LLM
        # This catches cases where LLM might misinterpret simple commands
        # BUT we must be careful not to catch "remind me to delete my emails" as a cancel

        # Check if this looks like a REMINDER CREATION (has "remind" before action words)
        is_reminder_creation = bool(re.match(r'^remind\s+(me|us)\s+to\s+', user_input_lower))

        # Check for "list" command - only exact matches, not "remind me to make a list"
        if not is_reminder_creation and user_input_lower in [
            "list", "show", "show reminders", "show my reminders",
            "list reminders", "what reminders", "my reminders"
        ]:
            reminders = self.db.get_pending_reminders()
            if not reminders:
                await update.message.reply_text("No pending reminders.")
            else:
                lines = ["Pending reminders:\n"]
                for r in reminders:
                    scheduled = datetime.fromisoformat(r["scheduled_time"])
                    time_str = scheduled.strftime("%a %d %b, %H:%M")
                    shared_icon = " 👥" if r.get("notify_all") else ""
                    lines.append(f"[{r['id']}] {r['task']}{shared_icon}\n    📅 {time_str}")
                await update.message.reply_text("\n".join(lines))
            return

        # Check for cancel/delete/remove commands
        # Only trigger if NOT a reminder creation like "remind me to cancel my subscription"
        cancel_keywords = ["cancel", "delete", "remove", "drop"]
        is_cancel = False
        if not is_reminder_creation:
            # Check if starts with cancel keyword
            is_cancel = any(user_input_lower.startswith(kw) for kw in cancel_keywords)
            # Also check for "cancel that", "delete the last", etc.
            if not is_cancel:
                is_cancel = any(kw in user_input_lower for kw in [
                    "cancel that", "delete that", "remove that",
                    "cancel the last", "delete the last", "remove the last",
                    "cancel reminder", "delete reminder", "remove reminder"
                ])

        if is_cancel:
            # Try to extract target ID
            target_id = None

            # Check for "last", "that", "previous" references
            if any(word in user_input_lower for word in ["last", "that", "previous", "the one"]):
                if recent_reminders:
                    target_id = recent_reminders[0]["id"]

            # Check for explicit ID number
            id_match = re.search(r'\b(\d+)\b', user_input)
            if id_match:
                target_id = int(id_match.group(1))

            if target_id:
                if self.db.cancel_reminder(target_id):
                    await update.message.reply_text(f"Cancelled reminder [{target_id}].")
                else:
                    await update.message.reply_text(
                        f"Reminder [{target_id}] not found or already sent."
                    )
            else:
                await update.message.reply_text(
                    "I couldn't figure out which reminder to cancel.\n"
                    "Try: 'cancel reminder 5' or 'cancel the last one'"
                )
            return

        # Parse with LLM (for more complex inputs)
        await update.message.reply_text("Processing...")

        result = await self.llm.parse_reminder(user_input, current_time, recent_reminders)

        if "error" in result:
            await update.message.reply_text(
                f"Sorry, I couldn't understand that.\n"
                f"Error: {result['error']}\n\n"
                f"Try being more specific, like:\n"
                f"'remind me to call mom tomorrow at 3pm'"
            )
            return

        action = result.get("action", "create")
        target_id = result.get("target_id")

        # Detect shared reminders - keyword fallback in case LLM misses it
        shared = result.get("shared", False)
        shared_keywords = ["remind us", "we need to", "we should", "remind both",
                          "notify us", "alert us", "tell us"]
        if any(kw in user_input_lower for kw in shared_keywords):
            shared = True

        # Handle different actions
        if action == "list":
            # Show pending reminders
            reminders = self.db.get_pending_reminders()
            if not reminders:
                await update.message.reply_text("No pending reminders.")
            else:
                lines = ["Pending reminders:\n"]
                for r in reminders:
                    scheduled = datetime.fromisoformat(r["scheduled_time"])
                    time_str = scheduled.strftime("%a %d %b, %H:%M")
                    shared_icon = "👥" if r.get("notify_all") else ""
                    lines.append(f"[{r['id']}] {r['task']} {shared_icon}\n    📅 {time_str}")
                await update.message.reply_text("\n".join(lines))
            return

        elif action == "cancel":
            # Cancel a reminder
            if not target_id:
                await update.message.reply_text(
                    "I couldn't figure out which reminder to cancel.\n"
                    "Try: 'cancel reminder 5' or use /cancel <id>"
                )
                return

            if self.db.cancel_reminder(target_id):
                await update.message.reply_text(f"Cancelled reminder [{target_id}].")
            else:
                await update.message.reply_text(
                    f"Reminder [{target_id}] not found or already sent."
                )
            return

        elif action == "modify":
            # Modify an existing reminder
            if not target_id:
                await update.message.reply_text(
                    "I couldn't figure out which reminder to modify.\n"
                    "Try: 'change reminder 5 to tomorrow' or use /delay <id> <hours>"
                )
                return

            # Parse new time if provided
            scheduled_time_str = result.get("scheduled_time")
            new_time = None
            if scheduled_time_str:
                try:
                    new_time = datetime.fromisoformat(scheduled_time_str)
                    if new_time.tzinfo is None:
                        new_time = new_time.replace(tzinfo=self.tz)
                except (ValueError, TypeError):
                    pass

            new_task = result.get("task")

            if self.db.modify_reminder(target_id, new_time=new_time, new_task=new_task):
                changes = []
                if new_time:
                    changes.append(f"time → {new_time.strftime('%a %d %b, %H:%M')}")
                if new_task:
                    changes.append(f"task → '{new_task}'")
                await update.message.reply_text(
                    f"Modified reminder [{target_id}]:\n" + "\n".join(changes)
                )
            else:
                await update.message.reply_text(
                    f"Reminder [{target_id}] not found or already sent."
                )
            return

        elif action == "duplicate":
            # Duplicate an existing reminder with optional new time
            if not target_id:
                await update.message.reply_text(
                    "I couldn't figure out which reminder to duplicate.\n"
                    "Try: 'duplicate reminder 5 for 7pm'"
                )
                return

            # Get the original reminder
            original = self.db.get_reminder(target_id)
            if not original:
                await update.message.reply_text(
                    f"Reminder [{target_id}] not found."
                )
                return

            # Use task/category from original, but new time if specified
            task = result.get("task") or original["task"]
            category = result.get("category") or original["category"]
            scheduled_time_str = result.get("scheduled_time")

            scheduled_time = None
            if scheduled_time_str:
                try:
                    scheduled_time = datetime.fromisoformat(scheduled_time_str)
                    if scheduled_time.tzinfo is None:
                        scheduled_time = scheduled_time.replace(tzinfo=self.tz)
                except (ValueError, TypeError):
                    pass

            # Resolve final time
            final_time = self.scheduler.resolve_time(category, scheduled_time, current_time)
            if isinstance(final_time, list):
                final_time = final_time[0]  # Just take first time for duplicates

            new_id = self.db.add_reminder(task, category, final_time, user_id, notify_all=shared)
            await update.message.reply_text(
                f"Duplicated reminder [{target_id}] → [{new_id}]\n"
                f"'{task}'\n"
                f"Scheduled: {final_time.strftime('%A %d %B, %H:%M')}"
            )
            return

        # Default: CREATE action
        task = result.get("task", user_input)
        category = result.get("category", "general")
        scheduled_time_str = result.get("scheduled_time")

        # Parse scheduled time if provided
        scheduled_time = None
        if scheduled_time_str:
            try:
                scheduled_time = datetime.fromisoformat(scheduled_time_str)
                if scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=self.tz)
            except (ValueError, TypeError):
                scheduled_time = None

        # Resolve final time(s) using schedule rules
        final_times = self.scheduler.resolve_time(category, scheduled_time, current_time)

        # Determine notification text
        notify_text = "you" if not shared else "everyone"

        # Handle single or multiple reminder times
        if isinstance(final_times, list):
            # Multiple reminders (e.g., food expiry)
            ids = []
            for t in final_times:
                rid = self.db.add_reminder(task, category, t, user_id, notify_all=shared)
                ids.append(rid)

            times_str = "\n".join(
                f"  • {t.strftime('%a %d %b, %H:%M')}" for t in final_times
            )
            shared_note = " 👥 (shared)" if shared else ""
            await update.message.reply_text(
                f"Got it! I'll remind {notify_text}:\n"
                f"'{task}'{shared_note}\n\n"
                f"Scheduled times:\n{times_str}\n\n"
                f"Category: {category}\n"
                f"IDs: {ids}"
            )
        else:
            # Single reminder
            reminder_id = self.db.add_reminder(task, category, final_times, user_id, notify_all=shared)
            time_str = final_times.strftime("%A %d %B, %H:%M")
            shared_note = " 👥 (shared)" if shared else ""

            await update.message.reply_text(
                f"Got it! I'll remind {notify_text}:\n"
                f"'{task}'{shared_note}\n\n"
                f"Scheduled: {time_str}\n"
                f"Category: {category}\n"
                f"ID: [{reminder_id}]"
            )

    async def send_due_reminders(self, app: Application):
        """Check for and send due reminders. Called by scheduler."""
        current_time = self._now()
        due_reminders = self.db.get_due_reminders(current_time)

        for reminder in due_reminders:
            try:
                # Determine recipients
                notify_all = reminder.get("notify_all", 0)
                if notify_all:
                    # Send to all authorized users
                    recipients = list(self.authorized_users)
                else:
                    # Send only to creator
                    recipients = [reminder["created_by"]]

                shared_note = " 👥" if notify_all else ""
                message = (
                    f"🔔 Reminder!{shared_note}\n\n"
                    f"{reminder['task']}\n\n"
                    f"(ID: {reminder['id']})\n"
                    f"💡 /snooze {reminder['id']} to snooze 15 min"
                )

                for user_id in recipients:
                    try:
                        await app.bot.send_message(chat_id=user_id, text=message)
                        self.logger.info(f"Sent reminder {reminder['id']} to user {user_id}")
                    except Exception as e:
                        self.logger.error(f"Failed to send reminder {reminder['id']} to {user_id}: {e}")

                self.db.mark_sent(reminder["id"])

            except Exception as e:
                self.logger.error(f"Failed to process reminder {reminder['id']}: {e}")

    def run(self):
        """Start the bot."""
        app = Application.builder().token(self.config["telegram"]["bot_token"]).build()

        # Add handlers
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(CommandHandler("list", self.list_reminders))
        app.add_handler(CommandHandler("cancel", self.cancel_reminder))
        app.add_handler(CommandHandler("delay", self.delay_reminder))
        app.add_handler(CommandHandler("snooze", self.snooze_reminder))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        # Set up scheduler job to check for due reminders every minute
        job_queue = app.job_queue
        job_queue.run_repeating(
            lambda ctx: asyncio.create_task(self.send_due_reminders(app)),
            interval=60,
            first=10,
        )

        self.logger.info("Bot started. Press Ctrl+C to stop.")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Main entry point."""
    # Determine config path
    config_path = os.environ.get("REMINDER_BOT_CONFIG", "config.yaml")

    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    bot = ReminderBot(config)
    bot.run()
    return 0


if __name__ == "__main__":
    exit(main())
