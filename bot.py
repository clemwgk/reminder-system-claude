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
    async def parse_reminder(self, user_input: str, current_time: datetime) -> dict:
        """
        Parse natural language reminder input.

        Returns dict with:
            - task: str (what to remind about)
            - category: str (general, bill, food_expiry)
            - scheduled_time: datetime or None
            - time_hint: str (original time reference from user)
            - error: str or None (if parsing failed)
        """
        pass


class GeminiProvider(LLMProvider):
    """Google Gemini API provider (free tier)."""

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash-lite"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    async def parse_reminder(self, user_input: str, current_time: datetime) -> dict:
        import aiohttp

        prompt = f"""You are a reminder parsing assistant. Parse the user's reminder request and extract structured information.

Current date and time: {current_time.strftime('%Y-%m-%d %H:%M:%S %A')} (Singapore Time)

User input: "{user_input}"

Respond with ONLY a JSON object (no markdown, no explanation) with these fields:
- "task": the thing to be reminded about (string)
- "category": one of "general", "bill", or "food_expiry" based on the content
- "scheduled_time": ISO format datetime if a specific time was mentioned, or null if not specified
- "time_hint": the original time reference from the user (e.g., "tomorrow", "next week", "3pm"), or null if none

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

Example response:
{{"task": "pay electricity bill", "category": "bill", "scheduled_time": "2024-01-15T09:00:00", "time_hint": "next week"}}
"""

        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 256,
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

    async def parse_reminder(self, user_input: str, current_time: datetime) -> dict:
        import aiohttp

        prompt = f"""You are a reminder parsing assistant. Parse the user's reminder request and extract structured information.

Current date and time: {current_time.strftime('%Y-%m-%d %H:%M:%S %A')} (Singapore Time)

User input: "{user_input}"

Respond with ONLY a JSON object (no markdown, no explanation) with these fields:
- "task": the thing to be reminded about (string)
- "category": one of "general", "bill", or "food_expiry" based on the content
- "scheduled_time": ISO format datetime if a specific time was mentioned, or null if not specified
- "time_hint": the original time reference from the user (e.g., "tomorrow", "next week", "3pm"), or null if none

Rules for category detection:
- "bill": anything related to payments, bills, subscriptions, dues, invoices
- "food_expiry": anything about food going bad, expiring, use by dates
- "general": everything else

Example response:
{{"task": "pay electricity bill", "category": "bill", "scheduled_time": "2024-01-15T09:00:00", "time_hint": "next week"}}
"""

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
                    sent_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status_time
                ON reminders (status, scheduled_time)
            """)
            conn.commit()

    def add_reminder(
        self,
        task: str,
        category: str,
        scheduled_time: datetime,
        created_by: int,
    ) -> int:
        """Add a new reminder. Returns the reminder ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (task, category, scheduled_time, created_at, created_by, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    task,
                    category,
                    scheduled_time.isoformat(),
                    datetime.now().isoformat(),
                    created_by,
                ),
            )
            conn.commit()
            return cursor.lastrowid

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
            "• 'pay credit card bill' (schedules for Saturday)\n"
            "• 'milk expires in 3 days' (reminds day before)\n\n"
            "Commands:\n"
            "/list - Show all pending reminders\n"
            "/cancel <id> - Cancel a reminder\n"
            "/delay <id> <hours> - Delay by X hours\n"
            "/help - Show this message\n\n"
            "Categories & defaults:\n"
            "• Bills → Saturday 9 AM\n"
            "• Food expiry → Day before, 9 AM & 6 PM\n"
            "• General → Next day 9 AM"
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

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle natural language reminder input."""
        user_id = update.effective_user.id

        if not self._is_authorized(user_id):
            await update.message.reply_text(
                f"You're not authorized. Your ID: {user_id}"
            )
            return

        user_input = update.message.text.strip()
        current_time = self._now()

        # Parse with LLM
        await update.message.reply_text("Processing...")

        result = await self.llm.parse_reminder(user_input, current_time)

        if "error" in result:
            await update.message.reply_text(
                f"Sorry, I couldn't understand that.\n"
                f"Error: {result['error']}\n\n"
                f"Try being more specific, like:\n"
                f"'remind me to call mom tomorrow at 3pm'"
            )
            return

        task = result.get("task", user_input)
        category = result.get("category", "general")
        scheduled_time_str = result.get("scheduled_time")
        time_hint = result.get("time_hint")

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

        # Handle single or multiple reminder times
        if isinstance(final_times, list):
            # Multiple reminders (e.g., food expiry)
            ids = []
            for t in final_times:
                rid = self.db.add_reminder(task, category, t, user_id)
                ids.append(rid)

            times_str = "\n".join(
                f"  • {t.strftime('%a %d %b, %H:%M')}" for t in final_times
            )
            await update.message.reply_text(
                f"Got it! I'll remind you:\n"
                f"'{task}'\n\n"
                f"Scheduled times:\n{times_str}\n\n"
                f"Category: {category}\n"
                f"IDs: {ids}"
            )
        else:
            # Single reminder
            reminder_id = self.db.add_reminder(task, category, final_times, user_id)
            time_str = final_times.strftime("%A %d %B, %H:%M")

            await update.message.reply_text(
                f"Got it! I'll remind you:\n"
                f"'{task}'\n\n"
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
                user_id = reminder["created_by"]
                message = (
                    f"🔔 Reminder!\n\n"
                    f"{reminder['task']}\n\n"
                    f"(ID: {reminder['id']}, Category: {reminder['category']})"
                )

                await app.bot.send_message(chat_id=user_id, text=message)
                self.db.mark_sent(reminder["id"])
                self.logger.info(f"Sent reminder {reminder['id']} to user {user_id}")

            except Exception as e:
                self.logger.error(f"Failed to send reminder {reminder['id']}: {e}")

    def run(self):
        """Start the bot."""
        app = Application.builder().token(self.config["telegram"]["bot_token"]).build()

        # Add handlers
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(CommandHandler("list", self.list_reminders))
        app.add_handler(CommandHandler("cancel", self.cancel_reminder))
        app.add_handler(CommandHandler("delay", self.delay_reminder))
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
