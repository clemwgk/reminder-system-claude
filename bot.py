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
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

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
- "action": one of "create", "cancel", "modify", "list", or "search"
- "task": the thing to be reminded about (string, required for create/modify when there's ONE task; null when "tasks" array is used or for non-create actions). IMPORTANT: preserve time references that are PART OF THE CONTENT (e.g., "car wash at 1130am" — the 1130am is when the appointment is, not when to remind). Only remove the scheduling time reference (the one extracted to scheduled_time).
- "tasks": array of task strings when the user provides a LIST of separate tasks (numbered/bulleted), or null. All tasks in the list share the same scheduled_time, category, recurrence, and shared fields. When "tasks" is non-null, set "task" to null.
- "category": one of "general", "bill", or "food_expiry" (for create/modify)
- "scheduled_time": ISO format datetime if a specific time was mentioned, or null
- "time_hint": the original time reference from the user, or null
- "target_id": the ID of an existing reminder to cancel/modify, or null
- "shared": true if the user said "remind us" or wants to notify all users, false otherwise
- "time_confidence": 0-100 score for how confident you are about scheduled_time (null if no time)
- "recurrence": recurrence pattern if user wants a repeating reminder, or null. Valid patterns: "daily", "weekly", "biweekly", "monthly", "yearly", "weekdays", "weekends"
- "recurrence_count": integer number of times to repeat, or null. E.g., "every week 4 times" → 4, "every month for 3 months" → 3
- "recurrence_end_date": ISO date (YYYY-MM-DD) when recurrence should stop, or null. E.g., "every day until December 2026" → "2026-12-31", "weekly until end of 2026" → "2026-12-31"
- "search_query": the search term for action "search" (null for other actions)

ACTION DETECTION RULES (VERY IMPORTANT):
- "create": User wants a NEW reminder (e.g., "remind me to...", "set a reminder for..."). This is the DEFAULT action.
- "cancel": User wants to REMOVE/DELETE a reminder (e.g., "remove that", "cancel reminder 5", "delete the last one")
- "modify": User wants to CHANGE an existing reminder (e.g., "change reminder 3 to 5pm", "update the last one")
- "list": User wants to SEE their reminders (e.g., "show my reminders", "what reminders do I have")
- "search": User wants to FIND specific reminders (e.g., "do I have a reminder for X?", "find reminder about X", "search for X", "is there a reminder for X")

IMPORTANT: If the user's message could be a new reminder task, always use "create". Only use cancel/modify/list when explicitly requested.

MULTI-TASK LISTS (added because the LLM previously returned a single "task" string when users sent numbered lists like "10pm: 1. X 2. Y", causing only one reminder to be created instead of one per item):
- If the user provides a numbered or bulleted list of SEPARATE tasks (e.g. "10pm:\n1. Buy milk\n2. Call mom"), extract each as its own entry in the "tasks" array and set "task" to null.
- All items in "tasks" share the same scheduled_time, category, recurrence, and shared fields.
- Do NOT split a single sentence that merely mentions multiple things (e.g. "buy eggs and milk" is ONE task, not two).
- A list of one item should still use "task" (not "tasks").

TASK CONTENT PRESERVATION (added because the LLM was stripping ALL time references from task text, not just the scheduling one — e.g. "tmr 1030am: car wash at 1130am" became task "car wash" instead of "car wash at 1130am", losing the appointment time which is meaningful content):
- When a message contains MULTIPLE time references, only ONE of them is the scheduling time (the one extracted to scheduled_time). Other time references are CONTENT and must stay in the task text.
- The scheduling time is typically the first time reference, or the one introduced with "remind me", "tmr", "tomorrow", or a colon separator (e.g. "10am: call mom" — 10am is scheduling, rest is content).
- Content-time examples that must be KEPT in the task text: "car wash at 1130am", "meeting at 3pm", "dentist at 2", "movie at 8pm", "pick up kids at 5".
- Example: "tmr 1030am: car wash at 1130am" → scheduled_time = tomorrow 10:30, task = "car wash at 1130am"
- Example: "remind me at 9am to call mom at 3pm" → scheduled_time = 09:00, task = "call mom at 3pm"
- If the whole message is a single task with one time (e.g. "call mom at 3pm"), the time is both the schedule AND the only content — the task can drop it to just "call mom".

TARGET IDENTIFICATION:
- If user says "the last one", "that one", "that reminder", "the previous one" → use the most recent reminder ID from context
- If user gives an ID number (e.g., "reminder 5", "cancel 3") → use that ID

SHARED REMINDERS:
- If user says "remind us", "we need to", "remind both of us" → set shared: true
- Default is shared: false (only notify the person who created it)

RECURRING REMINDERS:
- "every day", "daily", "each day" → recurrence: "daily"
- "every week", "weekly", "each week" → recurrence: "weekly"
- "every two weeks", "biweekly", "fortnightly" → recurrence: "biweekly"
- "every month", "monthly" → recurrence: "monthly"
- "every year", "yearly", "annually" → recurrence: "yearly"
- "every weekday", "on weekdays", "monday to friday" → recurrence: "weekdays"
- "every weekend", "on weekends", "saturdays and sundays" → recurrence: "weekends"
- If no recurring pattern detected → recurrence: null

RECURRENCE END CONSTRAINTS:
- "every week 4 times", "weekly for 4 weeks" → recurrence_count: 4
- "every month for 3 months", "monthly 3 times" → recurrence_count: 3
- "every day until December 2026" → recurrence_end_date: "2026-12-31"
- "weekly until end of 2026" → recurrence_end_date: "2026-12-31"
- "daily until 2026-06-30" → recurrence_end_date: "2026-06-30"
- If no count or end date mentioned → recurrence_count: null, recurrence_end_date: null (infinite)

Rules for category detection:
- "bill": anything related to payments, bills, subscriptions, dues, invoices
- "food_expiry": anything about food going bad, expiring, use by dates
- "general": everything else

Rules for scheduled_time:
- "tomorrow" or "tmr" = next day at 09:00
- "tomorrow morning" = next day at 09:00
- "tomorrow afternoon" = next day at 14:00
- "tomorrow evening" = next day at 18:00
- "tomorrow night" = next day at 20:00
- "today morning" or "this morning" = today at 09:00, or tomorrow 09:00 if already past
- "today afternoon" or "this afternoon" = today at 14:00, or tomorrow 14:00 if already past
- "today evening" or "this evening" = today at 18:00, or tomorrow 18:00 if already past
- "tonight" / "tonite" / "2nite" = today at 20:00, or tomorrow 20:00 if already past
- "next week" or "nxt wk" = same day next week at 09:00
- "in X hours/minutes" = current time + X
- If only a time like "3pm" is given, assume today if it's still before that time, otherwise tomorrow
- If no time mentioned at all, set to null (the system will apply category defaults)

Time-of-day defaults:
- morning = 09:00
- afternoon = 14:00
- evening = 18:00
- night = 20:00

Relative time with "before":
- "X days/weeks before [date/month]" = subtract X days/weeks from that date
- "2 weeks before March" = Feb 15 (2 weeks before March 1)
- "1 week before Christmas" = Dec 18 (1 week before Dec 25)
- "3 days before Friday" = Tuesday of the same week
- When a month is mentioned without a day, assume the 1st of that month

Common shorthands to recognize:
- "tmr" = tomorrow
- "nxt wk" = next week
- "2nite" or "tonite" = tonight (same day at 20:00)
- "aft" = afternoon (same day at 14:00)
- Day abbreviations: "mon", "tue", "wed", "thu", "fri", "sat", "sun" = that day of the week
  - If the day has already passed this week, schedule for NEXT week's occurrence
  - Example: If today is Monday and user says "fri 3pm", schedule for Friday of THIS week
  - Example: If today is Saturday and user says "fri 3pm", schedule for Friday of NEXT week

IMPORTANT: When a day of the week is mentioned, VERIFY that your scheduled_time actually falls on that day. Double-check your date calculation.

LANGUAGE NOTE (Singaporean English):
Users may use Singaporean English patterns. Key differences from US/UK English:

1. PREPOSITION OMISSION: "to", "on", "at" often dropped before times/places
   - "remind me Friday" = "remind me on Friday"
   - "go back Florence" = "go back to Florence"
   - "reach there 3pm" = "reach there at 3pm"

2. SUBJECT DROPPING in conditionals/context:
   - "if go back" = "if I go back"
   - "need buy groceries" = "I need to buy groceries"

3. TIME/PLACE FRONTING: Time often comes first without preposition
   - "Friday remind me to..." = "On Friday, remind me to..."
   - "Tomorrow if got time..." = "Tomorrow, if I have time..."
   - "11am check skincare" = "at 11am, check skincare" (IMPORTANT: extract 11:00)
   - "3pm call mom" = "at 3pm, call mom" (IMPORTANT: extract 15:00)
   - "6:30pm dinner" = "at 6:30pm, dinner" (IMPORTANT: extract 18:30)

Always extract the time reference even when prepositions are missing.
CRITICAL: When a time like "11am" or "3pm" appears at the START of the message, you MUST extract it as scheduled_time.

TIME CONFIDENCE SCORING (0-100):
Rate how confident YOU are that your scheduled_time matches what the USER INTENDED.
Ask yourself: "Could the user have meant a different date/time? Did I interpret ambiguous references correctly?"
- 90-100: Certain - explicit datetime or unambiguous reference
- 70-89: Confident - clear reference, unlikely to be wrong
- 40-69: Moderate - some ambiguity but reasonable interpretation
- 20-39: Uncertain - multiple valid interpretations possible
- 0-19: Guessing - low certainty, user should verify
- null: No time was mentioned

Example responses:
{{"action": "create", "task": "pay electricity bill", "category": "bill", "scheduled_time": null, "time_hint": null, "target_id": null, "shared": false, "time_confidence": null, "recurrence": null, "search_query": null}}
{{"action": "cancel", "task": null, "category": null, "scheduled_time": null, "time_hint": null, "target_id": 5, "shared": false, "time_confidence": null, "recurrence": null, "search_query": null}}
{{"action": "create", "task": "buy groceries", "category": "general", "scheduled_time": "2024-01-16T09:00:00", "time_hint": "tmr", "target_id": null, "shared": false, "time_confidence": 95, "recurrence": null, "search_query": null}}
{{"action": "create", "task": "bring milk if go back to Florence", "category": "general", "scheduled_time": "2024-01-24T09:00:00", "time_hint": "Friday", "target_id": null, "shared": false, "time_confidence": 85, "recurrence": null, "search_query": null}}
{{"action": "create", "task": "take vitamins", "category": "general", "scheduled_time": "2024-01-16T09:00:00", "time_hint": "9am", "target_id": null, "shared": false, "time_confidence": 90, "recurrence": "daily", "search_query": null}}
{{"action": "create", "task": "water plants", "category": "general", "scheduled_time": "2024-01-20T10:00:00", "time_hint": "Saturday 10am", "target_id": null, "shared": false, "time_confidence": 85, "recurrence": "weekly", "search_query": null}}
{{"action": "search", "task": null, "category": null, "scheduled_time": null, "time_hint": null, "target_id": null, "shared": false, "time_confidence": null, "recurrence": null, "search_query": "pediatrician"}}
{{"action": "create", "task": "car wash at 1130am", "tasks": null, "category": "general", "scheduled_time": "2024-01-16T10:30:00", "time_hint": "tmr 1030am", "target_id": null, "shared": false, "time_confidence": 95, "recurrence": null, "search_query": null}}
{{"action": "create", "task": null, "tasks": ["Pack clothes to bring for mom", "Pack airtumtec wipes"], "category": "general", "scheduled_time": "2024-01-15T22:00:00", "time_hint": "10pm", "target_id": null, "shared": false, "time_confidence": 90, "recurrence": null, "search_query": null}}
"""

    def _parse_llm_json(self, text: str) -> dict:
        """Parse JSON from model output, tolerating extra wrapper text."""
        cleaned = text.strip()

        # Clean up potential markdown code blocks
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            cleaned = cleaned.rsplit("```", 1)[0].strip()

        decoder = json.JSONDecoder()

        # First try strict parse from start
        try:
            parsed, _ = decoder.raw_decode(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Fallback: locate first JSON object when preface text exists
        start = cleaned.find("{")
        if start >= 0:
            parsed, _ = decoder.raw_decode(cleaned[start:])
            if isinstance(parsed, dict):
                return parsed

        raise json.JSONDecodeError("No JSON object found", cleaned, 0)


class GeminiProvider(LLMProvider):
    """Google Gemini API provider (free tier)."""

    def __init__(self, api_key: str, model: str = "gemini-3.1-flash-lite-preview", fallback_model: str = None):
        self.api_key = api_key
        self.model = model
        self.fallback_model = fallback_model
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
                        # Try fallback model if available
                        if self.fallback_model:
                            logger.warning(
                                f"Primary model {self.model} failed (HTTP {response.status}), "
                                f"falling back to {self.fallback_model}"
                            )
                            fallback_url = f"{self.base_url}/models/{self.fallback_model}:generateContent?key={self.api_key}"
                            async with session.post(fallback_url, json=payload) as fallback_response:
                                if fallback_response.status != 200:
                                    fallback_error = await fallback_response.text()
                                    return {"error": f"Gemini API error (both models failed): {fallback_error}"}
                                data = await fallback_response.json()
                        else:
                            return {"error": f"Gemini API error: {error_text}"}
                    else:
                        data = await response.json()

            # Extract the text response
            text = data["candidates"][0]["content"]["parts"][0]["text"]

            result = self._parse_llm_json(text)
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

            result = self._parse_llm_json(text)
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

            result = self._parse_llm_json(text)
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

            result = self._parse_llm_json(text)
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
            fallback_model=config["llm"]["gemini"].get("fallback_model"),
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
            # Add acknowledged_at column for tracking Done button presses
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN acknowledged_at TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Add time_confidence column for LLM confidence scoring
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN time_confidence INTEGER")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Add recurrence columns for recurring reminders
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN recurrence_pattern TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN recurrence_parent_id INTEGER")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Add recurrence end constraints for bounded recurring reminders
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN recurrence_end_date TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                conn.execute("ALTER TABLE reminders ADD COLUMN recurrence_remaining INTEGER")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Create user_preferences table for per-user settings
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    daily_summary_enabled INTEGER NOT NULL DEFAULT 1
                )
            """)
            conn.commit()

    def add_reminder(
        self,
        task: str,
        category: str,
        scheduled_time: datetime,
        created_by: int,
        notify_all: bool = False,
        time_confidence: int = None,
        recurrence_pattern: str = None,
        recurrence_parent_id: int = None,
        recurrence_end_date: str = None,
        recurrence_remaining: int = None,
    ) -> int:
        """Add a new reminder. Returns the reminder ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (task, category, scheduled_time, created_at, created_by, status, notify_all, time_confidence, recurrence_pattern, recurrence_parent_id, recurrence_end_date, recurrence_remaining)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task,
                    category,
                    scheduled_time.isoformat(),
                    datetime.now().isoformat(),
                    created_by,
                    1 if notify_all else 0,
                    time_confidence,
                    recurrence_pattern,
                    recurrence_parent_id,
                    recurrence_end_date,
                    recurrence_remaining,
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

    def snooze_reminder(self, reminder_id: int, new_time: datetime) -> bool:
        """Snooze a sent reminder - resets to pending with new time. Returns True if successful."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET scheduled_time = ?, status = 'pending', sent_at = NULL, acknowledged_at = NULL
                WHERE id = ? AND status = 'sent'
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

    def acknowledge_reminder(self, reminder_id: int) -> bool:
        """Mark a reminder as acknowledged (Done button pressed). Returns True if successful."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET acknowledged_at = ?
                WHERE id = ? AND status = 'sent'
                """,
                (datetime.now().isoformat(), reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_unacknowledged_reminders(self, user_id: int, days_cap: int = 7) -> list[dict]:
        """Get sent reminders that haven't been acknowledged (for recap).

        Only includes reminders sent within the last `days_cap` days.
        """
        cutoff = (datetime.now() - timedelta(days=days_cap)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'sent'
                AND acknowledged_at IS NULL
                AND (created_by = ? OR notify_all = 1)
                AND sent_at >= ?
                ORDER BY sent_at ASC
                """,
                (user_id, cutoff),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_pending_reminders_for_user(self, user_id: int) -> list[dict]:
        """Get pending reminders visible to a specific user (own + shared)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'pending'
                AND (created_by = ? OR notify_all = 1)
                ORDER BY scheduled_time ASC
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_reminders_for_user(self, user_id: int, query: str) -> list[dict]:
        """Search pending reminders by task text, visible to a specific user (own + shared)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'pending'
                AND (created_by = ? OR notify_all = 1)
                AND task LIKE ?
                ORDER BY scheduled_time ASC
                """,
                (user_id, f"%{query}%"),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_todays_reminders_for_user(self, user_id: int, today_start: datetime, today_end: datetime) -> list[dict]:
        """Get pending reminders scheduled for today, visible to a specific user."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'pending'
                AND (created_by = ? OR notify_all = 1)
                AND scheduled_time >= ?
                AND scheduled_time < ?
                ORDER BY scheduled_time ASC
                """,
                (user_id, today_start.isoformat(), today_end.isoformat()),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_user_preference(self, user_id: int, key: str, default=None):
        """Get a user preference value."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return dict(row).get(key, default)
            return default

    def set_user_preference(self, user_id: int, key: str, value):
        """Set a user preference value. Creates row if doesn't exist."""
        with sqlite3.connect(self.db_path) as conn:
            # Check if user exists
            existing = conn.execute(
                "SELECT 1 FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    f"UPDATE user_preferences SET {key} = ? WHERE user_id = ?",
                    (value, user_id),
                )
            else:
                conn.execute(
                    f"INSERT INTO user_preferences (user_id, {key}) VALUES (?, ?)",
                    (user_id, value),
                )
            conn.commit()

    def is_daily_summary_enabled(self, user_id: int) -> bool:
        """Check if daily summary is enabled for a user. Default is True."""
        result = self.get_user_preference(user_id, "daily_summary_enabled", 1)
        return result == 1

    def get_recurring_reminders_for_user(self, user_id: int) -> list[dict]:
        """Get all reminders with recurrence patterns (active recurring series)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM reminders
                WHERE (created_by = ? OR notify_all = 1)
                AND recurrence_pattern IS NOT NULL
                AND recurrence_parent_id IS NULL
                AND status != 'cancelled'
                ORDER BY scheduled_time ASC
                """,
                (user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def stop_recurrence(self, reminder_id: int) -> bool:
        """Stop a recurring reminder by clearing its recurrence pattern."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE reminders
                SET recurrence_pattern = NULL
                WHERE id = ?
                """,
                (reminder_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def cancel_recurring_series(self, reminder_id: int) -> tuple[bool, int]:
        """Stop a recurring series and cancel all pending instances.
        Returns (success, cancelled_count).

        Recurring reminders form a chain: when one fires, send_due_reminders() creates
        a new 'pending' child row with recurrence_parent_id pointing to the series root.
        This method finds the root from any member (parent or child), clears
        recurrence_pattern on the entire chain (preventing new children from being
        spawned), and cancels all still-pending members in one go.

        Needed because /cancel on a 'sent' recurring reminder previously just said
        "already sent or cancelled" — users had no intuitive way to stop the whole series.
        """
        reminder = self.get_reminder(reminder_id)
        if not reminder:
            return False, 0
        parent_id = reminder.get("recurrence_parent_id") or reminder["id"]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE reminders SET recurrence_pattern = NULL WHERE id = ? OR recurrence_parent_id = ?",
                (parent_id, parent_id),
            )
            cursor = conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE status = 'pending' AND (id = ? OR recurrence_parent_id = ?)",
                (parent_id, parent_id),
            )
            conn.commit()
            return True, cursor.rowcount

    def set_recurrence(self, reminder_id: int, pattern: str = None, end_date: str = None, remaining: int = None) -> bool:
        """Set or update recurrence on a reminder. Pass pattern=None to turn off recurrence."""
        with sqlite3.connect(self.db_path) as conn:
            if pattern is None:
                # Turn off recurrence
                cursor = conn.execute(
                    """
                    UPDATE reminders
                    SET recurrence_pattern = NULL, recurrence_end_date = NULL, recurrence_remaining = NULL
                    WHERE id = ?
                    """,
                    (reminder_id,),
                )
            else:
                # Set/update recurrence — also clear recurrence_parent_id so this becomes a root
                cursor = conn.execute(
                    """
                    UPDATE reminders
                    SET recurrence_pattern = ?, recurrence_end_date = ?, recurrence_remaining = ?,
                        recurrence_parent_id = NULL
                    WHERE id = ?
                    """,
                    (pattern, end_date, remaining, reminder_id),
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

    def _can_access(self, user_id: int, reminder: dict) -> bool:
        """Check if user can access a reminder (own reminders + shared reminders)."""
        return reminder["created_by"] == user_id or reminder.get("notify_all", 0) == 1

    def _now(self) -> datetime:
        """Get current time in configured timezone."""
        return datetime.now(self.tz)

    def _calculate_next_recurrence(
        self, current_time: datetime, pattern: str
    ) -> Optional[datetime]:
        """Calculate the next occurrence based on recurrence pattern.

        Patterns: daily, weekly, biweekly, monthly, yearly, weekdays, weekends
        """
        if not pattern:
            return None

        pattern = pattern.lower()

        if pattern == "daily":
            return current_time + timedelta(days=1)

        elif pattern == "weekly":
            return current_time + timedelta(weeks=1)

        elif pattern == "biweekly":
            return current_time + timedelta(weeks=2)

        elif pattern == "monthly":
            # Add one month (handle varying month lengths)
            month = current_time.month + 1
            year = current_time.year
            if month > 12:
                month = 1
                year += 1
            # Handle edge case: if day doesn't exist in target month (e.g., Jan 31 -> Feb)
            day = min(current_time.day, 28)  # Safe for all months
            return current_time.replace(year=year, month=month, day=day)

        elif pattern == "yearly":
            return current_time.replace(year=current_time.year + 1)

        elif pattern == "weekdays":
            # Next weekday (Mon-Fri)
            next_day = current_time + timedelta(days=1)
            while next_day.weekday() >= 5:  # Saturday=5, Sunday=6
                next_day += timedelta(days=1)
            return next_day

        elif pattern == "weekends":
            # Next weekend day (Sat-Sun)
            next_day = current_time + timedelta(days=1)
            while next_day.weekday() < 5:  # Mon-Fri = 0-4
                next_day += timedelta(days=1)
            return next_day

        return None

    # Day name mappings for post-processing validation
    DAY_NAMES = {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    }

    # Relative day mappings (0 = today, 1 = tomorrow)
    RELATIVE_DAYS = {
        "today": 0, "tdy": 0,
        "tonight": 0, "tonite": 0, "2nite": 0,
        "tomorrow": 1, "tmr": 1, "tml": 1, "tmrw": 1,
    }

    # Two-word relative phrases (all map to today)
    RELATIVE_PHRASES = {
        "this morning": 0,
        "this afternoon": 0,
        "this evening": 0,
    }

    # Time pattern for validation (matches 11am, 3pm, 6:30pm, 855am, 1030pm, etc.)
    TIME_PATTERN = re.compile(r'\b(\d{1,4})(?::(\d{2}))?\s*(am|pm)\b', re.IGNORECASE)

    def _get_words_to_check(self, user_input: str) -> list[str]:
        """Get the words to check for day names (limited scope to avoid false positives).

        - Standard: check words[0:2]
        - "remind me/us": check words[2:4]
        - If "this" is found in the slice, extend by one word to capture
          two-word phrases like "this afternoon", "this evening"
        """
        words = user_input.lower().split()
        if len(words) >= 3 and words[0] == "remind" and words[1] in ("me", "us"):
            result = words[2:4]
            if "this" in result and len(words) > 4:
                result = words[2:5]
        else:
            result = words[:2]
            if "this" in result and len(words) > 2:
                result = words[:3]
        return result

    def _validate_day_of_week(
        self, user_input: str, scheduled_time: datetime, current_time: datetime
    ) -> tuple[datetime, bool, bool]:
        """Validate and correct day reference if LLM got it wrong.

        Handles:
        - Day names: mon, tue, wed, thu, fri, sat, sun (and variants)
        - Relative days: today, tdy, tonight, tomorrow, tmr, etc.
        - Relative phrases: "this morning", "this afternoon", "this evening"

        Returns (corrected_time, was_corrected, day_reference_found).
        Only triggers when a day reference is found in the first 2 words
        (or words 2-4 after "remind me/us"). Extends by 1 word if "this" is found.
        """
        words_to_check = self._get_words_to_check(user_input)

        # Check consecutive pairs for two-word phrases (e.g., "this afternoon")
        for i in range(len(words_to_check) - 1):
            phrase = f"{words_to_check[i]} {words_to_check[i + 1]}"
            if phrase in self.RELATIVE_PHRASES:
                days_offset = self.RELATIVE_PHRASES[phrase]
                expected_date = (current_time + timedelta(days=days_offset)).date()

                if scheduled_time.date() == expected_date:
                    return scheduled_time, False, True

                self.logger.warning(
                    f"Relative phrase mismatch: user said '{phrase}' (expected {expected_date}) "
                    f"but LLM scheduled for {scheduled_time.date()}. Correcting..."
                )

                corrected_time = scheduled_time.replace(
                    year=expected_date.year,
                    month=expected_date.month,
                    day=expected_date.day,
                )

                self.logger.info(
                    f"Corrected scheduled time: {scheduled_time.strftime('%Y-%m-%d %H:%M')} → "
                    f"{corrected_time.strftime('%Y-%m-%d %H:%M')}"
                )
                return corrected_time, True, True

        # Check each word for day names or relative days
        for word in words_to_check:
            clean_word = re.sub(r'[^\w]', '', word)

            # Check for day-of-week names (mon, tue, wed, etc.)
            if clean_word in self.DAY_NAMES:
                target_weekday = self.DAY_NAMES[clean_word]

                # Check if scheduled_time falls on the correct day
                if scheduled_time.weekday() == target_weekday:
                    return scheduled_time, False, True

                # LLM got it wrong - calculate the correct date
                self.logger.warning(
                    f"Day-of-week mismatch: user said '{clean_word}' (weekday {target_weekday}) "
                    f"but LLM scheduled for {scheduled_time.strftime('%A')} (weekday {scheduled_time.weekday()}). "
                    f"Correcting..."
                )

                current_weekday = current_time.weekday()
                days_ahead = target_weekday - current_weekday
                if days_ahead <= 0:
                    days_ahead += 7

                correct_date = current_time.date() + timedelta(days=days_ahead)
                corrected_time = scheduled_time.replace(
                    year=correct_date.year,
                    month=correct_date.month,
                    day=correct_date.day,
                )

                self.logger.info(
                    f"Corrected scheduled time: {scheduled_time.strftime('%Y-%m-%d %H:%M')} → "
                    f"{corrected_time.strftime('%Y-%m-%d %H:%M')}"
                )
                return corrected_time, True, True

            # Check for relative days (today, tomorrow, etc.)
            if clean_word in self.RELATIVE_DAYS:
                days_offset = self.RELATIVE_DAYS[clean_word]
                expected_date = (current_time + timedelta(days=days_offset)).date()

                # Check if scheduled_time falls on the correct date
                if scheduled_time.date() == expected_date:
                    return scheduled_time, False, True

                # LLM got it wrong - correct to the expected date
                self.logger.warning(
                    f"Relative day mismatch: user said '{clean_word}' (expected {expected_date}) "
                    f"but LLM scheduled for {scheduled_time.date()}. Correcting..."
                )

                corrected_time = scheduled_time.replace(
                    year=expected_date.year,
                    month=expected_date.month,
                    day=expected_date.day,
                )

                self.logger.info(
                    f"Corrected scheduled time: {scheduled_time.strftime('%Y-%m-%d %H:%M')} → "
                    f"{corrected_time.strftime('%Y-%m-%d %H:%M')}"
                )
                return corrected_time, True, True

        # No day reference found in scope - no validation needed
        return scheduled_time, False, False

    def _validate_time_of_day(
        self, user_input: str, scheduled_time: Optional[datetime],
        current_time: datetime, day_reference_found: bool = True
    ) -> tuple[Optional[datetime], bool]:
        """Validate and correct time-of-day if LLM missed or got it wrong.

        Scans first few words of user input for explicit time patterns (11am, 3pm, etc.).
        If found and different from scheduled_time (or scheduled_time is None), corrects it.

        When day_reference_found is False (no day specified in scoped words), also validates
        the date: defaults to today if the time hasn't passed, tomorrow if it has.

        Returns (corrected_time, was_corrected).
        """
        words_to_check = self._get_words_to_check(user_input)
        text_to_check = " ".join(w.rstrip(":;,") for w in words_to_check)

        match = self.TIME_PATTERN.search(text_to_check)
        if not match:
            # No explicit time found in scope - no validation needed
            return scheduled_time, False

        # Parse the matched time (supports shorthand like 855am → 8:55am, 1030pm → 10:30pm)
        raw_hour = match.group(1)
        if match.group(2):
            # Explicit colon format: "8:55am"
            hour = int(raw_hour)
            minute = int(match.group(2))
        elif len(raw_hour) >= 3:
            # Shorthand: "855am" → 8:55, "1030pm" → 10:30
            minute = int(raw_hour[-2:])
            hour = int(raw_hour[:-2])
        else:
            # Simple: "8am" → 8:00
            hour = int(raw_hour)
            minute = 0
        ampm = match.group(3).lower()

        # Validate parsed values
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return scheduled_time, False

        # Convert to 24-hour format
        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0

        # Check if scheduled_time matches
        if scheduled_time is not None:
            if scheduled_time.hour == hour and scheduled_time.minute == minute:
                # Time matches, but if no day reference was found, validate the date too
                if not day_reference_found:
                    expected = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    expected_date = current_time.date() if expected > current_time else (current_time + timedelta(days=1)).date()
                    if scheduled_time.date() != expected_date:
                        self.logger.warning(
                            f"Date correction for time-only input: user said '{match.group(0)}' "
                            f"(no day reference), expected date {expected_date} "
                            f"but LLM scheduled for {scheduled_time.date()}. Correcting..."
                        )
                        corrected_time = scheduled_time.replace(
                            year=expected_date.year,
                            month=expected_date.month,
                            day=expected_date.day,
                        )
                        self.logger.info(
                            f"Date corrected to: {corrected_time.strftime('%Y-%m-%d %H:%M')}"
                        )
                        return corrected_time, True
                return scheduled_time, False

            # Time mismatch - correct it
            self.logger.warning(
                f"Time-of-day mismatch: user said '{match.group(0)}' ({hour:02d}:{minute:02d}) "
                f"but LLM scheduled for {scheduled_time.strftime('%H:%M')}. Correcting..."
            )
            corrected_time = scheduled_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If no day reference was found, also correct the date
            if not day_reference_found:
                expected = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                expected_date = current_time.date() if expected > current_time else (current_time + timedelta(days=1)).date()
                corrected_time = corrected_time.replace(
                    year=expected_date.year,
                    month=expected_date.month,
                    day=expected_date.day,
                )
        else:
            # No scheduled_time from LLM - create one for today or tomorrow
            self.logger.warning(
                f"Time extraction: user said '{match.group(0)}' but LLM returned null. "
                f"Setting time to {hour:02d}:{minute:02d}..."
            )
            corrected_time = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If the time has already passed today, schedule for tomorrow
            if corrected_time <= current_time:
                corrected_time += timedelta(days=1)

        self.logger.info(
            f"Time corrected to: {corrected_time.strftime('%Y-%m-%d %H:%M')}"
        )
        return corrected_time, True

    def _parse_explicit_time(self, time_str: str) -> Optional[datetime]:
        """Parse explicit time format for /settime command.

        Supports:
        - DD/MM HH:MM (24hr): 15/02 18:00
        - DD/MM/YY HH:MM (24hr): 15/02/26 18:00
        - DD/MM HHam/pm: 15/02 6pm, 15/02 6:30pm
        - Day name + time: thursday 9pm, fri 18:00
        - today/tomorrow + time: today 9pm, tmr 18:00
        - HH:MM (24hr, today): 18:00
        - HHam/pm (today): 6pm, 6:30pm
        """
        time_str = time_str.strip().lower()
        now = self._now()

        # Normalize: remove extra spaces, handle "6 pm" -> "6pm"
        time_str = re.sub(r'\s+(am|pm)', r'\1', time_str)

        # Try to split into date and time parts
        parts = time_str.split()

        if len(parts) == 2:
            # Date + time: "15/02 18:00" or "thursday 9pm"
            date_part, time_part = parts
        elif len(parts) == 1:
            # Time only: "18:00" or "6pm"
            date_part = None
            time_part = parts[0]
        else:
            return None

        # Parse time part (handles both 24hr and am/pm)
        hour, minute = None, 0
        # Try am/pm formats: 6pm, 6:30pm, 11am, 11:30am
        ampm_match = re.match(r'^(\d{1,2})(?::(\d{2}))?(am|pm)$', time_part)
        if ampm_match:
            hour = int(ampm_match.group(1))
            minute = int(ampm_match.group(2)) if ampm_match.group(2) else 0
            if ampm_match.group(3) == 'pm' and hour != 12:
                hour += 12
            elif ampm_match.group(3) == 'am' and hour == 12:
                hour = 0
        else:
            # Try 24hr format: 18:00, 9:00
            time_match = re.match(r'^(\d{1,2}):(\d{2})$', time_part)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))

        if hour is None or hour > 23 or minute > 59:
            return None

        # Parse date part
        if date_part:
            # Try DD/MM/YY
            date_match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2})$', date_part)
            if date_match:
                day = int(date_match.group(1))
                month = int(date_match.group(2))
                year = 2000 + int(date_match.group(3))
            else:
                # Try DD/MM (assume current year, or next year if date passed)
                date_match = re.match(r'^(\d{1,2})/(\d{1,2})$', date_part)
                if date_match:
                    day = int(date_match.group(1))
                    month = int(date_match.group(2))
                    year = now.year
                    # If date already passed this year, use next year
                    try:
                        candidate = datetime(year, month, day, hour, minute, tzinfo=self.tz)
                        if candidate < now:
                            year += 1
                    except ValueError:
                        return None
                elif date_part in self.DAY_NAMES:
                    # Day name: thursday, fri, etc. → next nearest occurrence
                    target_weekday = self.DAY_NAMES[date_part]
                    current_weekday = now.weekday()
                    days_ahead = target_weekday - current_weekday
                    if days_ahead <= 0:
                        days_ahead += 7
                    target_date = now.date() + timedelta(days=days_ahead)
                    day, month, year = target_date.day, target_date.month, target_date.year
                elif date_part in self.RELATIVE_DAYS:
                    # Relative day: today, tdy, tomorrow, tmr, etc.
                    days_offset = self.RELATIVE_DAYS[date_part]
                    target_date = (now + timedelta(days=days_offset)).date()
                    day, month, year = target_date.day, target_date.month, target_date.year
                else:
                    return None
        else:
            # Time only - use today
            day, month, year = now.day, now.month, now.year

        try:
            result = datetime(year, month, day, hour, minute, tzinfo=self.tz)
            return result
        except ValueError:
            return None

    async def _get_reminder_id_from_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
        """Extract reminder ID from a replied-to bot message."""
        if not update.message.reply_to_message:
            return None
        replied_msg = update.message.reply_to_message
        # Check if replying to a bot message
        bot_user = await context.bot.get_me()
        if not replied_msg.from_user or replied_msg.from_user.id != bot_user.id:
            return None
        # Try to extract reminder ID from the message
        # Matches: "ID: [48]", "(ID: 48)", "ID: 48"
        id_match = re.search(r'ID[:\s]*\[?(\d+)\]?', replied_msg.text or "")
        if id_match:
            return int(id_match.group(1))
        return None

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
            "Recurring reminders:\n"
            "• 'remind me every day at 9am to take vitamins'\n"
            "• 'remind me weekly on Saturday to water plants'\n"
            "• 'remind me every week to water plants 4 times'\n"
            "• 'remind me daily to exercise until end of 2026'\n"
            "• Patterns: daily, weekly, biweekly, monthly, yearly, weekdays, weekends\n"
            "• Limit with count (N times) or end date (until ...)\n\n"
            "Searching reminders:\n"
            "• 'do I have a reminder for pediatrician?'\n"
            "• 'find reminder about groceries'\n\n"
            "Modifying reminders (natural language):\n"
            "• 'cancel that last reminder'\n"
            "• 'remove reminder 5'\n"
            "• 'change reminder 3 to tomorrow'\n\n"
            "Commands:\n"
            "/list - Show your pending reminders\n"
            "/recurring - Show your recurring reminders\n"
            "/stoprecurring <id> - Stop a recurring reminder\n"
            "/setrecurrence <id> <pattern> [N times] [until <date>] - Set/change recurrence\n"
            "/summary - Show daily summary (today + unacknowledged)\n"
            "/dailysummary on|off - Toggle 8 AM summary\n"
            "/cancel <id> [id2...] - Cancel reminder(s)\n"
            "/delay <id> <hours> - Delay by X hours\n"
            "/snooze <id> [mins] - Snooze for 15 mins (or specify)\n"
            "/edit <id> [++] <text> - Edit (or ++ append to) reminder\n"
            "/settime <id> <time> - Set exact time (e.g., 15/02 6pm)\n"
            "/copy <id> <time> - Copy reminder to new time\n"
            "/changelog - Show recent updates\n"
            "/help - Show this message\n\n"
            "Quick reply:\n"
            "Reply to any bot message to act on that reminder:\n"
            "• Reply with /cancel, /snooze, /delay, /edit, /settime, /copy\n"
            "• Or just type new text to edit the reminder\n\n"
            "Time shorthands:\n"
            "• tmr = tomorrow\n"
            "• nxt wk = next week\n"
            "• tomorrow morning/afternoon/evening/night\n"
            "• this morning/afternoon/evening\n\n"
            "Categories & defaults:\n"
            "• Bills → Saturday 9 AM\n"
            "• Food expiry → Day before, 9 AM & 6 PM\n"
            "• General → Next day 9 AM\n\n"
            "Shared reminders:\n"
            "Say 'remind us' instead of 'remind me' to notify all users.\n\n"
            "Privacy:\n"
            "You only see your own reminders + shared ones."
        )

    @staticmethod
    def _collapse_recurring(reminders: list[dict]) -> list[dict]:
        """Collapse recurring series into one entry per series (the earliest pending child).

        Non-recurring reminders pass through unchanged. For recurring ones,
        group by series (using recurrence_parent_id or own id for roots) and
        keep only the entry with the earliest scheduled_time.
        """
        series_map = {}  # series_key -> best reminder
        result = []

        for r in reminders:
            if not r.get("recurrence_pattern"):
                # Non-recurring: pass through
                result.append(r)
            else:
                # Recurring: group by series
                series_key = r.get("recurrence_parent_id") or r["id"]
                if series_key not in series_map:
                    series_map[series_key] = r
                else:
                    # Keep the one with earlier scheduled_time
                    existing_time = datetime.fromisoformat(series_map[series_key]["scheduled_time"])
                    this_time = datetime.fromisoformat(r["scheduled_time"])
                    if this_time < existing_time:
                        series_map[series_key] = r

        result.extend(series_map.values())
        # Re-sort by scheduled_time
        result.sort(key=lambda r: r["scheduled_time"])
        return result

    @staticmethod
    def _format_recurrence_info(reminder: dict) -> str:
        """Format recurrence pattern with end constraints for display."""
        pattern = reminder.get("recurrence_pattern")
        if not pattern:
            return ""
        info = f" 🔄 {pattern.capitalize()}"
        remaining = reminder.get("recurrence_remaining")
        end_date = reminder.get("recurrence_end_date")
        if remaining is not None:
            info += f" ({remaining} remaining)"
        elif end_date:
            try:
                end_display = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d %b %Y")
                info += f" (until {end_display})"
            except ValueError:
                info += f" (until {end_date})"
        return info

    @staticmethod
    def _format_recurrence_confirmation(pattern: str, count: int = None, end_date: str = None) -> str:
        """Format recurrence info for creation confirmation messages."""
        if not pattern:
            return ""
        note = f"\n🔄 Repeats: {pattern}"
        if count is not None:
            note += f" ({count} times)"
        if end_date:
            try:
                end_display = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d %b %Y")
                note += f" (until {end_display})"
            except ValueError:
                note += f" (until {end_date})"
        return note

    async def list_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command - shows user's own reminders + shared reminders."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminders = self.db.get_pending_reminders_for_user(user_id)

        if not reminders:
            await update.message.reply_text("No pending reminders.")
            return

        # Collapse recurring series into single entries
        reminders = self._collapse_recurring(reminders)

        lines = ["Pending reminders:\n"]
        for r in reminders:
            scheduled = datetime.fromisoformat(r["scheduled_time"])
            time_str = scheduled.strftime("%a %d %b, %H:%M")
            shared_marker = " 👥" if r.get("notify_all", 0) else ""
            recurrence_info = self._format_recurrence_info(r)
            lines.append(f"[{r['id']}] {r['task']}{shared_marker}{recurrence_info}\n    📅 {time_str}")

        await update.message.reply_text("\n".join(lines))

    async def cancel_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command - supports multiple IDs."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminder_ids = []

        # Check if replying to a bot message (get ID from reply)
        reply_id = await self._get_reminder_id_from_reply(update, context)
        if reply_id and not context.args:
            reminder_ids = [reply_id]
        elif context.args:
            # Parse all args as IDs
            for arg in context.args:
                try:
                    reminder_ids.append(int(arg))
                except ValueError:
                    await update.message.reply_text(f"Invalid reminder ID: {arg}")
                    return
        else:
            await update.message.reply_text(
                "Usage: /cancel <id> [id2] [id3] ...\n"
                "Example: /cancel 57 58 59\n"
                "Or reply to a reminder message with /cancel"
            )
            return

        # Cancel each reminder and collect results (with ownership check)
        results = []
        for rid in reminder_ids:
            reminder = self.db.get_reminder(rid)
            if not reminder:
                results.append(f"[{rid}] not found")
            elif not self._can_access(user_id, reminder):
                results.append(f"[{rid}] not accessible")
            elif self.db.cancel_reminder(rid):
                results.append(f"[{rid}] cancelled")
            else:
                # cancel_reminder() only works on 'pending' rows. If it returns False,
                # this reminder is already 'sent' or 'cancelled'. For recurring reminders,
                # the user likely wants to stop the whole series — not just this one instance.
                # We detect recurring membership via recurrence_parent_id (child) or
                # recurrence_pattern (root) and stop the entire series automatically.
                parent_id = reminder.get("recurrence_parent_id") or (
                    reminder["id"] if reminder.get("recurrence_pattern") else None
                )
                if parent_id:
                    success, count = self.db.cancel_recurring_series(rid)
                    if success:
                        results.append(
                            f"[{rid}] already sent — stopped recurring series. "
                            f"{count} pending reminder(s) cancelled."
                        )
                    else:
                        results.append(f"[{rid}] already sent or cancelled")
                else:
                    results.append(f"[{rid}] already sent or cancelled")

        await update.message.reply_text("\n".join(results))

    async def delay_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /delay command."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminder_id = None
        hours = None

        reply_id = await self._get_reminder_id_from_reply(update, context)

        if len(context.args) >= 2:
            # Two args: /delay <id> <hours> - explicit, ignore reply context
            try:
                reminder_id = int(context.args[0])
                hours = float(context.args[1])
            except ValueError:
                await update.message.reply_text("Invalid input. Use: /delay <id> <hours>")
                return
        elif len(context.args) == 1:
            if reply_id:
                # One arg + reply: arg is hours, ID from reply
                reminder_id = reply_id
                try:
                    hours = float(context.args[0])
                except ValueError:
                    await update.message.reply_text("Invalid hours. Use: /delay <hours>")
                    return
            else:
                # One arg, no reply: ambiguous - is it ID or hours?
                await update.message.reply_text(
                    "Ambiguous input. Please specify both ID and hours:\n"
                    "/delay <id> <hours>\n\n"
                    "Or reply to a reminder message with /delay <hours>"
                )
                return
        else:
            # No args: always need hours
            await update.message.reply_text(
                "Usage: /delay <reminder_id> <hours>\n"
                "Example: /delay 5 24  (delay by 24 hours)\n\n"
                "Or reply to a reminder message with /delay <hours>"
            )
            return

        # Get reminder and check ownership
        reminder = self.db.get_reminder(reminder_id)

        if not reminder or reminder["status"] != "pending":
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not found or already sent."
            )
            return

        if not self._can_access(user_id, reminder):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not accessible."
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
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminder_id = None
        minutes = 15  # default

        reply_id = await self._get_reminder_id_from_reply(update, context)

        if len(context.args) >= 2:
            # Two args: /snooze <id> <minutes> - explicit, ignore reply context
            try:
                reminder_id = int(context.args[0])
                minutes = int(context.args[1])
            except ValueError:
                await update.message.reply_text("Invalid input. Use: /snooze <id> [minutes]")
                return
        elif len(context.args) == 1:
            if reply_id:
                # One arg + reply: arg is minutes, ID from reply
                reminder_id = reply_id
                try:
                    minutes = int(context.args[0])
                except ValueError:
                    await update.message.reply_text("Invalid minutes. Use: /snooze <minutes>")
                    return
            else:
                # One arg, no reply: ambiguous - is it ID or minutes?
                await update.message.reply_text(
                    "Ambiguous input. Please specify both ID and minutes:\n"
                    "/snooze <id> <minutes>\n\n"
                    "Or reply to a reminder message with /snooze <minutes>"
                )
                return
        elif reply_id:
            # No args + reply: use reply ID with default 15 minutes
            reminder_id = reply_id
        else:
            # No args, no reply: show usage
            await update.message.reply_text(
                "Usage: /snooze <reminder_id> [minutes]\n"
                "Example: /snooze 5 30    (snooze 30 mins)\n\n"
                "Or reply to a reminder message with /snooze [minutes]"
            )
            return

        # Check ownership before snoozing
        reminder = self.db.get_reminder(reminder_id)
        if not reminder:
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not found."
            )
            return

        if not self._can_access(user_id, reminder):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] not accessible."
            )
            return

        # Calculate new time from now
        new_time = self._now() + timedelta(minutes=minutes)

        # Try snoozing sent reminder first, then pending
        if self.db.snooze_reminder(reminder_id, new_time):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] snoozed for {minutes} minutes.\n"
                f"New time: {new_time.strftime('%H:%M')}"
            )
        elif self.db.delay_reminder(reminder_id, new_time):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] snoozed for {minutes} minutes.\n"
                f"New time: {new_time.strftime('%H:%M')}"
            )
        else:
            await update.message.reply_text(
                f"Reminder [{reminder_id}] already cancelled."
            )

    async def edit_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /edit command - edit the task text of a pending reminder.

        Supports append mode with ++ prefix:
        - /edit <id> <text> → replaces entire text
        - /edit <id> ++ <text> → appends to existing text
        """
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminder_id = None
        new_task = None

        # Check if replying to a bot message (get ID from reply)
        reply_id = await self._get_reminder_id_from_reply(update, context)
        if reply_id:
            reminder_id = reply_id
            # All args are the new task text when replying
            if context.args:
                new_task = " ".join(context.args)
            else:
                await update.message.reply_text("Usage: /edit <new text> (when replying)")
                return
        elif len(context.args) >= 2:
            try:
                reminder_id = int(context.args[0])
            except ValueError:
                await update.message.reply_text("Invalid reminder ID.")
                return
            new_task = " ".join(context.args[1:])
        else:
            await update.message.reply_text(
                "Usage: /edit <reminder_id> [++] <new text>\n"
                "Example: /edit 48 buy infant formula by 23 Jan\n"
                "Append: /edit 48 ++ also get diapers\n"
                "Or reply to a reminder message with /edit <new text>"
            )
            return

        # Check ownership before editing
        reminder = self.db.get_reminder(reminder_id)
        if not reminder:
            await update.message.reply_text(f"Reminder [{reminder_id}] not found.")
            return

        if not self._can_access(user_id, reminder):
            await update.message.reply_text(f"Reminder [{reminder_id}] not accessible.")
            return

        if reminder["status"] != "pending":
            await update.message.reply_text(
                f"Reminder [{reminder_id}] already sent.\n"
                f"(Only pending reminders can be edited)"
            )
            return

        # Check for append mode (++ prefix)
        append_mode = False
        if new_task.startswith("++ "):
            append_mode = True
            new_task = new_task[3:]  # Remove "++ "
        elif new_task.startswith("++"):
            append_mode = True
            new_task = new_task[2:]  # Remove "++"

        if append_mode:
            # Append to existing task text
            existing_task = reminder["task"]
            new_task = f"{existing_task}, {new_task}"

        if self.db.modify_reminder(reminder_id, new_task=new_task):
            mode_text = "appended" if append_mode else "updated"
            await update.message.reply_text(
                f"Reminder [{reminder_id}] {mode_text}:\n'{new_task}'"
            )
        else:
            await update.message.reply_text("Failed to edit reminder.")

    async def settime_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /settime command - set exact time for a reminder using explicit format."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminder_id = None
        time_str = None

        reply_id = await self._get_reminder_id_from_reply(update, context)

        if reply_id and context.args:
            # Reply mode: check if first arg is a reminder ID or part of the time
            try:
                candidate_id = int(context.args[0])
                # First arg is an integer — treat as explicit ID override
                reminder_id = candidate_id
                time_str = " ".join(context.args[1:]) if len(context.args) > 1 else None
            except ValueError:
                # First arg is NOT an integer — use reply_id, all args are the time
                reminder_id = reply_id
                time_str = " ".join(context.args)
        elif len(context.args) >= 2:
            # No reply: /settime <id> <time>
            try:
                reminder_id = int(context.args[0])
                time_str = " ".join(context.args[1:])
            except ValueError:
                await update.message.reply_text("Invalid reminder ID.")
                return
        else:
            await update.message.reply_text(
                "Usage: /settime <id> <time>\n\n"
                "Time formats:\n"
                "• DD/MM HH:MM → 15/02 18:00\n"
                "• DD/MM HHam/pm → 15/02 6pm\n"
                "• Day HHam/pm → thursday 9pm\n"
                "• HHam/pm → 6pm (today)\n\n"
                "Examples:\n"
                "• /settime 135 15/02 6pm\n"
                "• /settime 135 thu 9pm\n"
                "• /settime 135 18:00\n"
                "• Reply + /settime 6pm\n"
                "• Reply + /settime 21/03 08:55"
            )
            return

        if not time_str:
            await update.message.reply_text("Please provide a time.")
            return

        # Check reminder exists and user can access
        reminder = self.db.get_reminder(reminder_id)
        if not reminder:
            await update.message.reply_text(f"Reminder [{reminder_id}] not found.")
            return
        if not self._can_access(user_id, reminder):
            await update.message.reply_text(f"Reminder [{reminder_id}] not accessible.")
            return
        if reminder["status"] != "pending":
            await update.message.reply_text(
                f"Reminder [{reminder_id}] already sent.\n"
                "Use /snooze to reschedule sent reminders."
            )
            return

        # Parse the explicit time
        new_time = self._parse_explicit_time(time_str)
        if not new_time:
            await update.message.reply_text(
                f"Couldn't parse time: '{time_str}'\n\n"
                "Expected formats:\n"
                "• DD/MM HH:MM → 15/02 18:00\n"
                "• DD/MM HHam/pm → 15/02 6pm\n"
                "• Day HHam/pm → thu 9pm\n"
                "• HH:MM → 18:00 (today)\n"
                "• HHam/pm → 6pm (today)"
            )
            return

        if self.db.modify_reminder(reminder_id, new_time=new_time):
            await update.message.reply_text(
                f"Reminder [{reminder_id}] rescheduled:\n"
                f"'{reminder['task']}'\n\n"
                f"New time: {new_time.strftime('%A %d %B, %H:%M')}"
            )
        else:
            await update.message.reply_text("Failed to update reminder.")

    async def copy_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /copy command - duplicate a pending reminder with new time."""
        if not self._is_authorized(update.effective_user.id):
            return

        reminder_id = None
        time_text = None

        reply_id = await self._get_reminder_id_from_reply(update, context)

        if len(context.args) >= 2:
            # Two+ args: /copy <id> <time text> - explicit ID
            try:
                reminder_id = int(context.args[0])
                time_text = " ".join(context.args[1:])
            except ValueError:
                await update.message.reply_text("Invalid reminder ID.")
                return
        elif len(context.args) == 1:
            if reply_id:
                # One arg + reply: arg is time text, ID from reply
                reminder_id = reply_id
                time_text = context.args[0]
            else:
                # One arg, no reply: ambiguous
                await update.message.reply_text(
                    "Ambiguous input. Please specify ID and time:\n"
                    "/copy <id> <time>\n\n"
                    "Or reply to a reminder message with /copy <time>"
                )
                return
        elif reply_id:
            # No args + reply: use category default time
            reminder_id = reply_id
            time_text = None
        else:
            await update.message.reply_text(
                "Usage: /copy <id> <time> or reply with /copy [time]\n"
                "Examples:\n"
                "  /copy 48 tomorrow\n"
                "  /copy 48 next week\n"
                "  Reply + /copy 5pm"
            )
            return

        # Get the original reminder (must be pending and accessible)
        user_id = update.effective_user.id
        original = self.db.get_reminder(reminder_id)
        if not original:
            await update.message.reply_text(f"Reminder [{reminder_id}] not found.")
            return
        if not self._can_access(user_id, original):
            await update.message.reply_text(f"Reminder [{reminder_id}] not accessible.")
            return
        if original["status"] != "pending":
            await update.message.reply_text(
                f"Reminder [{reminder_id}] is not pending.\n"
                f"Only pending reminders can be copied."
            )
            return

        current_time = self._now()

        # Parse time using LLM if provided, otherwise use category defaults
        if time_text:
            result = await self.llm.parse_reminder(f"remind me {time_text}", current_time)
            if "error" in result:
                await update.message.reply_text(f"Couldn't parse time: {time_text}")
                return
            scheduled_time_str = result.get("scheduled_time")
            if scheduled_time_str:
                try:
                    scheduled_time = datetime.fromisoformat(scheduled_time_str)
                    if scheduled_time.tzinfo is None:
                        scheduled_time = scheduled_time.replace(tzinfo=self.tz)
                except (ValueError, TypeError):
                    scheduled_time = None
            else:
                scheduled_time = None
        else:
            scheduled_time = None

        # Resolve final time using category defaults if needed
        category = original["category"]
        final_time = self.scheduler.resolve_time(category, scheduled_time, current_time)
        if isinstance(final_time, list):
            final_time = final_time[0]

        # Create the copy
        new_id = self.db.add_reminder(
            original["task"],
            category,
            final_time,
            user_id,
            notify_all=bool(original.get("notify_all", 0))
        )

        await update.message.reply_text(
            f"Copied reminder [{reminder_id}] → [{new_id}]\n"
            f"'{original['task']}'\n\n"
            f"Scheduled: {final_time.strftime('%A %d %B, %H:%M')}"
        )

    async def recurring_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /recurring command - list all active recurring reminders."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        reminders = self.db.get_recurring_reminders_for_user(user_id)

        if not reminders:
            await update.message.reply_text(
                "No recurring reminders set.\n\n"
                "Create one with phrases like:\n"
                "• 'remind me every day at 9am to take vitamins'\n"
                "• 'remind me weekly on Saturday to water plants'"
            )
            return

        lines = ["🔄 <b>Recurring reminders:</b>\n"]
        for r in reminders:
            scheduled = datetime.fromisoformat(r["scheduled_time"])
            time_str = scheduled.strftime("%a %d %b, %H:%M")
            shared_marker = " 👥" if r.get("notify_all", 0) else ""
            recurrence_info = self._format_recurrence_info(r)
            lines.append(
                f"[{r['id']}] {r['task']}{shared_marker}\n"
                f"    📅 Next: {time_str}\n"
                f"   {recurrence_info}"
            )

        lines.append("\nUse /stoprecurring <id> or /setrecurrence <id> off to stop.")
        lines.append("Use /setrecurrence <id> <pattern> to change cadence.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def stop_recurring(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stoprecurring command - stop a recurring reminder."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /stoprecurring <id>\n"
                "Example: /stoprecurring 42\n\n"
                "Use /recurring to see your recurring reminders."
            )
            return

        try:
            reminder_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Invalid reminder ID.")
            return

        # Check reminder exists and user can access
        reminder = self.db.get_reminder(reminder_id)
        if not reminder:
            await update.message.reply_text(f"Reminder [{reminder_id}] not found.")
            return
        if not self._can_access(user_id, reminder):
            await update.message.reply_text(f"Reminder [{reminder_id}] not accessible.")
            return
        # Accept both root IDs (have recurrence_pattern) and child IDs (have
        # recurrence_parent_id) so users can stop a series using any member's ID —
        # previously only worked if the ID still had recurrence_pattern set.
        is_recurring = (
            reminder.get("recurrence_pattern")
            or reminder.get("recurrence_parent_id")
        )
        if not is_recurring:
            await update.message.reply_text(
                f"Reminder [{reminder_id}] is not a recurring reminder."
            )
            return

        success, count = self.db.cancel_recurring_series(reminder_id)
        if success:
            await update.message.reply_text(
                f"✓ Stopped recurring series for [{reminder_id}].\n"
                f"{count} pending reminder(s) cancelled."
            )
        else:
            await update.message.reply_text("Failed to stop recurrence.")

    async def set_recurrence_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setrecurrence command - set, change, or remove recurrence on a reminder."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        VALID_PATTERNS = {"daily", "weekly", "biweekly", "monthly", "yearly", "weekdays", "weekends"}

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /setrecurrence <id> <pattern> [N times] [until <date>]\n\n"
                "Examples:\n"
                "• /setrecurrence 42 weekly\n"
                "• /setrecurrence 42 monthly 3 times\n"
                "• /setrecurrence 42 daily until 2026-12-31\n"
                "• /setrecurrence 42 off\n\n"
                f"Patterns: {', '.join(sorted(VALID_PATTERNS))}, off"
            )
            return

        try:
            reminder_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Invalid reminder ID.")
            return

        pattern_arg = context.args[1].lower()

        # Check reminder exists and user can access
        reminder = self.db.get_reminder(reminder_id)
        if not reminder:
            await update.message.reply_text(f"Reminder [{reminder_id}] not found.")
            return
        if not self._can_access(user_id, reminder):
            await update.message.reply_text(f"Reminder [{reminder_id}] not accessible.")
            return
        if reminder.get("status") != "pending":
            await update.message.reply_text(f"Reminder [{reminder_id}] is not pending.")
            return

        # Handle "off"
        if pattern_arg == "off":
            if self.db.set_recurrence(reminder_id, pattern=None):
                await update.message.reply_text(
                    f"✓ Turned off recurrence for reminder [{reminder_id}].\n"
                    f"It will fire once at its scheduled time but won't repeat."
                )
            else:
                await update.message.reply_text("Failed to update recurrence.")
            return

        # Validate pattern
        if pattern_arg not in VALID_PATTERNS:
            await update.message.reply_text(
                f"Unknown pattern '{pattern_arg}'.\n"
                f"Valid patterns: {', '.join(sorted(VALID_PATTERNS))}, off"
            )
            return

        # Parse optional end constraints from remaining args
        remaining_args = " ".join(context.args[2:]).lower()
        end_date = None
        remaining_count = None

        # Check for "N times"
        times_match = re.search(r'(\d+)\s*times?', remaining_args)
        if times_match:
            remaining_count = int(times_match.group(1))

        # Check for "until <date>"
        until_match = re.search(r'until\s+(.+)', remaining_args)
        if until_match:
            date_str = until_match.group(1).strip()
            # Remove "times" part if it appeared before "until"
            date_str = re.sub(r'\d+\s*times?\s*', '', date_str).strip()
            try:
                # Try ISO format: YYYY-MM-DD
                parsed = datetime.strptime(date_str, "%Y-%m-%d")
                end_date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    # Try DD/MM/YYYY
                    parsed = datetime.strptime(date_str, "%d/%m/%Y")
                    end_date = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    # Try natural dates like "end of 2026", "dec 2026"
                    end_of_match = re.match(r'end\s+of\s+(\d{4})', date_str)
                    if end_of_match:
                        end_date = f"{end_of_match.group(1)}-12-31"
                    else:
                        # Try "month year" format
                        try:
                            parsed = datetime.strptime(date_str, "%b %Y")
                            # Last day of that month
                            import calendar
                            last_day = calendar.monthrange(parsed.year, parsed.month)[1]
                            end_date = f"{parsed.year}-{parsed.month:02d}-{last_day:02d}"
                        except ValueError:
                            try:
                                parsed = datetime.strptime(date_str, "%B %Y")
                                import calendar
                                last_day = calendar.monthrange(parsed.year, parsed.month)[1]
                                end_date = f"{parsed.year}-{parsed.month:02d}-{last_day:02d}"
                            except ValueError:
                                await update.message.reply_text(
                                    f"Could not parse date '{date_str}'.\n"
                                    "Use YYYY-MM-DD, DD/MM/YYYY, 'end of 2026', or 'Dec 2026'."
                                )
                                return

        if self.db.set_recurrence(reminder_id, pattern=pattern_arg, end_date=end_date, remaining=remaining_count):
            # Build confirmation
            constraint_parts = []
            if remaining_count:
                constraint_parts.append(f"{remaining_count} times")
            if end_date:
                end_display = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d %b %Y")
                constraint_parts.append(f"until {end_display}")
            constraint_str = f" ({', '.join(constraint_parts)})" if constraint_parts else ""

            await update.message.reply_text(
                f"✓ Set reminder [{reminder_id}] to recur {pattern_arg}{constraint_str}.\n"
                f"📌 {reminder['task']}"
            )
        else:
            await update.message.reply_text("Failed to update recurrence.")

    async def changelog_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /changelog command - show recent changes."""
        if not self._is_authorized(update.effective_user.id):
            return

        changelog = (
            "📋 Changelog\n"
            "────────────\n\n"
            "v1.6.0 (Mar 2026)\n"
            "• 🔄 Recurring reminders! Use 'every day', 'weekly', etc.\n"
            "• /recurring - view your recurring reminders\n"
            "• /stoprecurring <id> - stop a recurring reminder\n"
            "• Patterns: daily, weekly, biweekly, monthly, yearly, weekdays, weekends\n\n"
            "v1.5.9 (Feb 2026)\n"
            "• /settime accepts day names (thu 9pm) and today/tmr\n"
            "• Validation handles 'this afternoon/evening/morning'\n\n"
            "v1.5.8 (Feb 2026)\n"
            "• Validation now handles today/tomorrow (tdy, tmr, etc.)\n\n"
            "v1.5.7 (Feb 2026)\n"
            "• Shows /settime hint when LLM confidence <20%\n"
            "• Processing message auto-deletes after parsing\n\n"
            "v1.5.6 (Feb 2026)\n"
            "• /settime - set exact time (15/02 6pm, 18:00, etc.)\n\n"
            "v1.5.5 (Feb 2026)\n"
            "• Better parsing for 'X weeks before [month]' patterns\n\n"
            "v1.5.4 (Feb 2026)\n"
            "• Day-of-week auto-correction for LLM parsing errors\n"
            "• Shows ⚡ indicator when correction is applied\n\n"
            "v1.5.3 (Feb 2026)\n"
            "• /dailysummary on|off - toggle 8 AM summary per user\n\n"
            "v1.5.2 (Jan 2026)\n"
            "• Added Snooze 1d button (24 hours)\n\n"
            "v1.5.1 (Jan 2026)\n"
            "• Still pending section now capped at last 7 days\n\n"
            "v1.5.0 (Jan 2026)\n"
            "• Daily summary - auto sent at 8 AM + /summary command\n"
            "• Done button now marks reminders as acknowledged\n"
            "• Privacy - you only see your own + shared reminders\n\n"
            "v1.4.0 (Jan 2026)\n"
            "• /copy command - duplicate reminders with new time\n"
            "• /cancel now supports multiple IDs\n"
            "• Time shorthands: tmr, nxt wk, etc.\n"
            "• Improved direct reply for all commands\n\n"
            "v1.3.0 (Jan 2026)\n"
            "• Pop formatting - bold reminder notifications\n"
            "• Inline buttons - Snooze 15m, Snooze 1h, Done\n\n"
            "v1.2.0 (Jan 2026)\n"
            "• /edit command - edit reminder text\n"
            "• Reply-to-edit - reply to bot message to edit/cancel/snooze\n"
            "• /changelog command\n\n"
            "v1.1.0 (Jan 2026)\n"
            "• Shared reminders ('remind us')\n"
            "• /snooze command\n"
            "• Natural language cancel/delete\n\n"
            "v1.0.0 (Jan 2026)\n"
            "• Initial release"
        )
        await update.message.reply_text(changelog)

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

        # =====================================================================
        # DIRECT REPLY FEATURE: Reply to bot message to edit/cancel/snooze/delay
        # =====================================================================
        if update.message.reply_to_message:
            replied_msg = update.message.reply_to_message
            # Check if replying to a bot message
            if replied_msg.from_user and replied_msg.from_user.id == (await context.bot.get_me()).id:
                # Try to extract reminder ID from the bot's message
                # Patterns: "ID: [48]" (confirmation) or "(ID: 48)" (notification)
                id_match = re.search(r'ID[:\s]*\[?(\d+)\]?', replied_msg.text or "")
                if id_match:
                    reminder_id = int(id_match.group(1))

                    # Check ownership for all reply actions
                    reminder = self.db.get_reminder(reminder_id)
                    if not reminder:
                        await update.message.reply_text(f"Reminder [{reminder_id}] not found.")
                        return
                    if not self._can_access(user_id, reminder):
                        await update.message.reply_text(f"Reminder [{reminder_id}] not accessible.")
                        return

                    # Check what action the user wants
                    if user_input_lower.startswith("/cancel"):
                        if self.db.cancel_reminder(reminder_id):
                            await update.message.reply_text(f"Cancelled reminder [{reminder_id}].")
                        else:
                            await update.message.reply_text(f"Reminder [{reminder_id}] already sent or cancelled.")
                        return

                    elif user_input_lower.startswith("/snooze"):
                        # Parse optional minutes from /snooze or /snooze 30
                        parts = user_input.split()
                        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
                        new_time = self._now() + timedelta(minutes=minutes)
                        if self.db.snooze_reminder(reminder_id, new_time) or self.db.delay_reminder(reminder_id, new_time):
                            await update.message.reply_text(
                                f"Reminder [{reminder_id}] snoozed for {minutes} minutes.\n"
                                f"New time: {new_time.strftime('%H:%M')}"
                            )
                        else:
                            await update.message.reply_text(f"Reminder [{reminder_id}] already cancelled.")
                        return

                    elif user_input_lower.startswith("/delay"):
                        # Parse hours from /delay 2
                        parts = user_input.split()
                        if len(parts) > 1:
                            try:
                                hours = float(parts[1])
                                current_scheduled = datetime.fromisoformat(reminder["scheduled_time"])
                                new_time = current_scheduled + timedelta(hours=hours)
                                if self.db.delay_reminder(reminder_id, new_time):
                                    await update.message.reply_text(
                                        f"Reminder [{reminder_id}] delayed to {new_time.strftime('%a %d %b, %H:%M')}"
                                    )
                                    return
                            except ValueError:
                                pass
                        await update.message.reply_text("Usage: /delay <hours> (e.g., /delay 2)")
                        return

                    elif user_input_lower.startswith("/edit"):
                        # Parse new text from /edit new task text (supports ++ for append)
                        new_task = user_input[5:].strip()  # Remove "/edit"
                        if new_task:
                            # Check for append mode (++ prefix)
                            append_mode = False
                            if new_task.startswith("++ "):
                                append_mode = True
                                new_task = new_task[3:]  # Remove "++ "
                            elif new_task.startswith("++"):
                                append_mode = True
                                new_task = new_task[2:]  # Remove "++"

                            if append_mode:
                                # Append to existing task text
                                new_task = f"{reminder['task']}, {new_task}"

                            if self.db.modify_reminder(reminder_id, new_task=new_task):
                                mode_text = "appended" if append_mode else "updated"
                                await update.message.reply_text(f"Reminder [{reminder_id}] {mode_text}:\n'{new_task}'")
                            else:
                                await update.message.reply_text(
                                    f"Reminder [{reminder_id}] not found or already sent.\n"
                                    f"(Only pending reminders can be edited)"
                                )
                        else:
                            await update.message.reply_text("Usage: /edit [++] <new task text>")
                        return

                    else:
                        # No slash command - treat as new task text (edit)
                        new_task = user_input
                        if self.db.modify_reminder(reminder_id, new_task=new_task):
                            await update.message.reply_text(f"Reminder [{reminder_id}] updated:\n'{new_task}'")
                        else:
                            await update.message.reply_text(
                                f"Reminder [{reminder_id}] not found or already sent.\n"
                                f"(Only pending reminders can be edited)"
                            )
                        return

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
            reminders = self.db.get_pending_reminders_for_user(user_id)
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
        processing_msg = await update.message.reply_text("Processing...")

        result = await self.llm.parse_reminder(user_input, current_time, recent_reminders)

        # Delete the "Processing..." message
        try:
            await processing_msg.delete()
        except Exception:
            pass  # Ignore if delete fails

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
            # Show pending reminders (user's own + shared)
            reminders = self.db.get_pending_reminders_for_user(user_id)
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

        elif action == "cancel":
            # Cancel a reminder
            if not target_id:
                await update.message.reply_text(
                    "I couldn't figure out which reminder to cancel.\n"
                    "Try: 'cancel reminder 5' or use /cancel <id>"
                )
                return

            # Check ownership before cancelling
            reminder = self.db.get_reminder(target_id)
            if not reminder:
                await update.message.reply_text(f"Reminder [{target_id}] not found.")
                return
            if not self._can_access(user_id, reminder):
                await update.message.reply_text(f"Reminder [{target_id}] not accessible.")
                return

            if self.db.cancel_reminder(target_id):
                await update.message.reply_text(f"Cancelled reminder [{target_id}].")
            else:
                await update.message.reply_text(
                    f"Reminder [{target_id}] already sent or cancelled."
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

            # Check ownership before modifying
            reminder = self.db.get_reminder(target_id)
            if not reminder:
                await update.message.reply_text(f"Reminder [{target_id}] not found.")
                return
            if not self._can_access(user_id, reminder):
                await update.message.reply_text(f"Reminder [{target_id}] not accessible.")
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
                    f"Reminder [{target_id}] already sent or cancelled."
                )
            return

        elif action == "search":
            # Search for reminders matching a query
            search_query = result.get("search_query", "").strip()
            if not search_query:
                await update.message.reply_text(
                    "I couldn't figure out what to search for.\n"
                    "Try: 'do I have a reminder for pediatrician?' or 'find reminder about groceries'"
                )
                return

            reminders = self.db.search_reminders_for_user(user_id, search_query)
            if not reminders:
                await update.message.reply_text(f'No reminders found matching "{search_query}".')
            else:
                count = len(reminders)
                plural = "" if count == 1 else "s"
                lines = [f'Found {count} reminder{plural} matching "{search_query}":\n']
                for r in reminders:
                    scheduled = datetime.fromisoformat(r["scheduled_time"])
                    time_str = scheduled.strftime("%a %d %b, %H:%M")
                    shared_icon = " 👥" if r.get("notify_all") else ""
                    lines.append(f"📋 [ID: {r['id']}] {r['task']}{shared_icon}\n   📅 {time_str}")
                await update.message.reply_text("\n".join(lines))
            return

        # Default: CREATE action
        # The LLM returns either "task" (single string) or "tasks" (array) when the user
        # sends a numbered/bulleted list. We normalize both into task_list so the creation
        # loop below handles one-or-many uniformly. Falls back to raw user_input if the
        # LLM returned neither (e.g. older LLM provider without tasks support).
        task = result.get("task")
        tasks = result.get("tasks")
        if tasks and isinstance(tasks, list):
            task_list = [str(t).strip() for t in tasks if t and str(t).strip()]
            if not task_list:
                task_list = [task or user_input]
        else:
            task_list = [task or user_input]
        category = result.get("category", "general")
        scheduled_time_str = result.get("scheduled_time")
        time_confidence = result.get("time_confidence")
        recurrence = result.get("recurrence")
        recurrence_count = result.get("recurrence_count")
        recurrence_end_date = result.get("recurrence_end_date")

        # Keyword fallback for recurrence patterns in case LLM misses it
        recurrence_keywords = {
            "every day": "daily", "daily": "daily", "each day": "daily",
            "every week": "weekly", "weekly": "weekly", "each week": "weekly",
            "every two weeks": "biweekly", "biweekly": "biweekly", "fortnightly": "biweekly",
            "every month": "monthly", "monthly": "monthly",
            "every year": "yearly", "yearly": "yearly", "annually": "yearly",
            "every weekday": "weekdays", "on weekdays": "weekdays", "monday to friday": "weekdays",
            "every weekend": "weekends", "on weekends": "weekends",
        }
        if not recurrence:
            for phrase, pattern in recurrence_keywords.items():
                if phrase in user_input_lower:
                    recurrence = pattern
                    break

        # Parse scheduled time if provided
        scheduled_time = None
        day_corrected = False
        day_found = False
        time_corrected = False
        if scheduled_time_str:
            try:
                scheduled_time = datetime.fromisoformat(scheduled_time_str)
                if scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=self.tz)
                # Apply day-of-week validation (limited scope to avoid false positives)
                scheduled_time, day_corrected, day_found = self._validate_day_of_week(
                    user_input, scheduled_time, current_time
                )
            except (ValueError, TypeError):
                scheduled_time = None

        # Apply time-of-day validation (catches cases where LLM missed explicit time)
        # When no day reference was found in scoped words, also validates the date
        scheduled_time, time_corrected = self._validate_time_of_day(
            user_input, scheduled_time, current_time, day_reference_found=day_found
        )

        # Resolve final time(s) using schedule rules
        final_times = self.scheduler.resolve_time(category, scheduled_time, current_time)

        # Log confidence score for analysis
        if time_confidence is not None:
            self.logger.info(
                f"Time parsing confidence: {time_confidence}% for input '{user_input}' "
                f"→ {scheduled_time_str}"
            )

        # Determine notification text
        notify_text = "you" if not shared else "everyone"

        # Handle single or multiple reminder times
        # Build notes for corrections and low confidence
        notes = []
        if day_corrected:
            notes.append("⚡ (day-of-week auto-corrected)")
        if time_corrected:
            notes.append("⚡ (time auto-corrected)")
        # Show hint if confidence is low (<20%) OR post-validation was triggered
        low_confidence = time_confidence is not None and time_confidence < 20
        if low_confidence or day_corrected or time_corrected:
            notes.append("💡 Wrong? Reply /settime DD/MM[/YY] HH:MM or HHam/pm")
        correction_note = "\n" + "\n".join(notes) if notes else ""

        multi_task = len(task_list) > 1
        if multi_task:
            tasks_display = "\n".join(f"  {i+1}. '{t}'" for i, t in enumerate(task_list))
        else:
            tasks_display = f"'{task_list[0]}'"

        if isinstance(final_times, list):
            # Multiple reminders (e.g., food expiry) — cross-product with task_list
            ids = []
            for t in final_times:
                for tk in task_list:
                    rid = self.db.add_reminder(tk, category, t, user_id, notify_all=shared, time_confidence=time_confidence, recurrence_pattern=recurrence, recurrence_end_date=recurrence_end_date, recurrence_remaining=recurrence_count)
                    ids.append(rid)

            times_str = "\n".join(
                f"  • {t.strftime('%a %d %b, %H:%M')}" for t in final_times
            )
            shared_note = " 👥 (shared)" if shared else ""
            recurrence_note = self._format_recurrence_confirmation(recurrence, recurrence_count, recurrence_end_date)
            await update.message.reply_text(
                f"Got it! I'll remind {notify_text}:\n"
                f"{tasks_display}{shared_note}\n\n"
                f"Scheduled times:\n{times_str}\n\n"
                f"Category: {category}{recurrence_note}\n"
                f"IDs: {ids}{correction_note}"
            )
        else:
            # Single time — one reminder per task in task_list
            ids = []
            for tk in task_list:
                rid = self.db.add_reminder(tk, category, final_times, user_id, notify_all=shared, time_confidence=time_confidence, recurrence_pattern=recurrence, recurrence_end_date=recurrence_end_date, recurrence_remaining=recurrence_count)
                ids.append(rid)
            time_str = final_times.strftime("%A %d %B, %H:%M")
            shared_note = " 👥 (shared)" if shared else ""
            recurrence_note = self._format_recurrence_confirmation(recurrence, recurrence_count, recurrence_end_date)

            id_line = f"IDs: {ids}" if multi_task else f"ID: [{ids[0]}]"
            await update.message.reply_text(
                f"Got it! I'll remind {notify_text}:\n"
                f"{tasks_display}{shared_note}\n\n"
                f"Scheduled: {time_str}\n"
                f"Category: {category}{recurrence_note}\n"
                f"{id_line}{correction_note}"
            )

    async def handle_button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses."""
        query = update.callback_query
        await query.answer()  # Acknowledge the callback

        user_id = query.from_user.id
        if not self._is_authorized(user_id):
            return

        data = query.data  # e.g., "snooze_15_123" or "done_123"
        parts = data.split("_")

        if len(parts) < 2:
            return

        action = parts[0]

        if action == "snooze" and len(parts) >= 3:
            # snooze_15_123 or snooze_60_123
            minutes = int(parts[1])
            reminder_id = int(parts[2])
            new_time = self._now() + timedelta(minutes=minutes)

            if self.db.snooze_reminder(reminder_id, new_time) or self.db.delay_reminder(reminder_id, new_time):
                # Update the message to show it was snoozed
                await query.edit_message_text(
                    f"⏰ <b>Snoozed</b> for {minutes} minutes\n\n"
                    f"New time: {new_time.strftime('%H:%M')}\n\n"
                    f"<i>(ID: {reminder_id})</i>",
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text(
                    f"Could not snooze reminder [{reminder_id}] - it may have been cancelled.",
                    parse_mode="HTML",
                )

        elif action == "snoozerel" and len(parts) >= 3:
            # Snooze relative to the reminder's original scheduled_time, not the
            # current time. E.g. a 9am reminder tapped at 3pm with "+1d" reschedules
            # to 9am tomorrow, preserving the original time slot. If original+N is
            # already in the past (reminder was overdue by more than N days), falls
            # back to now+N as a safety net.
            days = int(parts[1])
            reminder_id = int(parts[2])
            reminder = self.db.get_reminder(reminder_id)
            if not reminder:
                await query.edit_message_text("Reminder not found.", parse_mode="HTML")
                return
            orig_time = datetime.fromisoformat(reminder["scheduled_time"])
            if orig_time.tzinfo is None:
                orig_time = orig_time.replace(tzinfo=self.tz)
            new_time = orig_time + timedelta(days=days)
            if new_time <= self._now():
                new_time = self._now() + timedelta(days=days)
            if self.db.snooze_reminder(reminder_id, new_time) or self.db.delay_reminder(reminder_id, new_time):
                label = f"+{days}d" if days < 7 else f"+{days // 7}w"
                await query.edit_message_text(
                    f"⏰ <b>Snoozed {label}</b> from original time\n\n"
                    f"New time: {new_time.strftime('%a %d %b, %H:%M')}\n\n"
                    f"<i>(ID: {reminder_id})</i>",
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text(
                    f"Could not snooze reminder [{reminder_id}] - it may have been cancelled.",
                    parse_mode="HTML",
                )

        elif action == "stopseries" and len(parts) >= 2:
            # Handles the "🛑 Stop" inline button shown on recurring reminder
            # notifications. Stops the entire recurring series (clears recurrence_pattern
            # on all members and cancels all pending instances) so the user doesn't have
            # to hunt for the next pending child's ID to cancel individually.
            reminder_id = int(parts[1])
            reminder = self.db.get_reminder(reminder_id)
            if not reminder:
                await query.edit_message_text("Reminder not found.", parse_mode="HTML")
                return
            task_text = reminder.get("task", "reminder")
            success, count = self.db.cancel_recurring_series(reminder_id)
            if success:
                await query.edit_message_text(
                    f"🛑 <b>Recurring series stopped</b>\n\n"
                    f"📌 {task_text}\n\n"
                    f"{count} pending reminder(s) cancelled.\n"
                    f"<i>(ID: {reminder_id})</i>",
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text("Failed to stop recurring series.", parse_mode="HTML")

        elif action == "done" and len(parts) >= 2:
            # done_123
            reminder_id = int(parts[1])
            # Get reminder to preserve the task text
            reminder = self.db.get_reminder(reminder_id)
            task_text = reminder["task"] if reminder else "reminder"
            # Mark as acknowledged in DB and update message
            self.db.acknowledge_reminder(reminder_id)
            await query.edit_message_text(
                f"[✓ Done] {task_text}\n\n"
                f"<i>(ID: {reminder_id})</i>",
                parse_mode="HTML",
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
                recurrence_note = " 🔄" if reminder.get("recurrence_pattern") else ""
                reminder_id = reminder['id']

                # Pop formatting with horizontal lines
                message = (
                    f"━━━━━━━━━━━━━━━\n"
                    f"🔔 <b>Reminder</b>{shared_note}{recurrence_note}\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"📌 {reminder['task']}\n\n"
                    f"(ID: {reminder_id})"
                )

                # Inline keyboard buttons for quick actions.
                # "1h"/"1d" snooze from current time (good for "not now" deferral).
                # "+1d"/"+1w" snooze from original scheduled_time (good for "same time
                # tomorrow/next week" — e.g. a 9am reminder tapped at 3pm reschedules
                # to 9am next day, not 3pm). Falls back to now+N if original+N is past.
                # "Stop" only shown for recurring reminders to stop the whole series.
                keyboard = [
                    [
                        InlineKeyboardButton("⏰ 1h", callback_data=f"snooze_60_{reminder_id}"),
                        InlineKeyboardButton("⏰ 1d", callback_data=f"snooze_1440_{reminder_id}"),
                        InlineKeyboardButton("⏰ +1d", callback_data=f"snoozerel_1_{reminder_id}"),
                        InlineKeyboardButton("⏰ +1w", callback_data=f"snoozerel_7_{reminder_id}"),
                    ],
                    [
                        InlineKeyboardButton("✓ Done", callback_data=f"done_{reminder_id}"),
                    ],
                ]
                if reminder.get("recurrence_pattern"):
                    keyboard[1].append(
                        InlineKeyboardButton("🛑 Stop", callback_data=f"stopseries_{reminder_id}")
                    )
                reply_markup = InlineKeyboardMarkup(keyboard)

                for user_id in recipients:
                    try:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=message,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                        self.logger.info(f"Sent reminder {reminder['id']} to user {user_id}")
                    except Exception as e:
                        self.logger.error(f"Failed to send reminder {reminder['id']} to {user_id}: {e}")

                self.db.mark_sent(reminder["id"])

                # Handle recurring reminders: create next occurrence
                recurrence_pattern = reminder.get("recurrence_pattern")
                if recurrence_pattern:
                    # Check if recurrence should continue
                    recurrence_remaining = reminder.get("recurrence_remaining")
                    should_create_next = True

                    # Check count-based limit
                    if recurrence_remaining is not None and recurrence_remaining <= 1:
                        should_create_next = False

                    scheduled_time = datetime.fromisoformat(reminder["scheduled_time"])
                    if scheduled_time.tzinfo is None:
                        scheduled_time = scheduled_time.replace(tzinfo=self.tz)
                    next_time = self._calculate_next_recurrence(scheduled_time, recurrence_pattern)

                    # Check end-date limit
                    recurrence_end_date = reminder.get("recurrence_end_date")
                    if recurrence_end_date and next_time:
                        end_dt = datetime.strptime(recurrence_end_date, "%Y-%m-%d").replace(
                            hour=23, minute=59, second=59, tzinfo=self.tz
                        )
                        if next_time > end_dt:
                            should_create_next = False

                    if next_time and should_create_next:
                        # Get parent ID (original recurring reminder)
                        parent_id = reminder.get("recurrence_parent_id") or reminder["id"]
                        # Decrement remaining count if set
                        next_remaining = None
                        if recurrence_remaining is not None:
                            next_remaining = recurrence_remaining - 1
                        new_id = self.db.add_reminder(
                            task=reminder["task"],
                            category=reminder["category"],
                            scheduled_time=next_time,
                            created_by=reminder["created_by"],
                            notify_all=bool(notify_all),
                            recurrence_pattern=recurrence_pattern,
                            recurrence_parent_id=parent_id,
                            recurrence_end_date=recurrence_end_date,
                            recurrence_remaining=next_remaining,
                        )
                        self.logger.info(
                            f"Created next recurring reminder [{new_id}] for "
                            f"{next_time.strftime('%Y-%m-%d %H:%M')} (parent: {parent_id})"
                        )

            except Exception as e:
                self.logger.error(f"Failed to process reminder {reminder['id']}: {e}")

    async def summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /summary command - show daily summary on demand."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        summary = self._build_summary_for_user(user_id)
        await update.message.reply_text(summary, parse_mode="HTML")

    async def dailysummary_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /dailysummary command - toggle daily summary on/off for the user."""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            return

        if context.args:
            arg = context.args[0].lower()
            if arg in ("on", "yes", "true", "1"):
                self.db.set_user_preference(user_id, "daily_summary_enabled", 1)
                await update.message.reply_text("✓ Daily summary enabled. You'll receive it at 8 AM.")
            elif arg in ("off", "no", "false", "0"):
                self.db.set_user_preference(user_id, "daily_summary_enabled", 0)
                await update.message.reply_text("✓ Daily summary disabled. You can still use /summary anytime.")
            else:
                await update.message.reply_text("Usage: /dailysummary on|off")
        else:
            # Show current status
            enabled = self.db.is_daily_summary_enabled(user_id)
            status = "enabled" if enabled else "disabled"
            await update.message.reply_text(
                f"Daily summary is currently {status} for you.\n\n"
                f"Use /dailysummary on or /dailysummary off to change."
            )

    def _build_summary_for_user(self, user_id: int) -> str:
        """Build daily summary text for a specific user."""
        now = self._now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        # Get today's reminders
        todays_reminders = self.db.get_todays_reminders_for_user(user_id, today_start, today_end)

        # Get unacknowledged sent reminders
        unacknowledged = self.db.get_unacknowledged_reminders(user_id)

        # Build summary
        lines = [f"☀️ <b>Daily Summary</b> - {now.strftime('%a %d %b')}\n"]

        if todays_reminders:
            lines.append("📅 <b>Today's reminders:</b>")
            for r in todays_reminders:
                scheduled = datetime.fromisoformat(r["scheduled_time"])
                time_str = scheduled.strftime("%H:%M")
                shared_marker = " 👥" if r.get("notify_all", 0) else ""
                lines.append(f"• {time_str} - {r['task']}{shared_marker} [ID: {r['id']}]")
            lines.append("")
        else:
            lines.append("📅 No reminders scheduled for today.\n")

        if unacknowledged:
            lines.append("⚠️ <b>Still pending (sent but not marked done):</b>")
            for r in unacknowledged:
                sent_at = datetime.fromisoformat(r["sent_at"]) if r.get("sent_at") else None
                sent_str = sent_at.strftime("%a %H:%M") if sent_at else "unknown"
                shared_marker = " 👥" if r.get("notify_all", 0) else ""
                lines.append(f"• {r['task']}{shared_marker} [ID: {r['id']}] - sent {sent_str}")

        return "\n".join(lines)

    async def send_daily_summary(self, app: Application):
        """Send daily summary to users who have it enabled. Called by scheduler."""
        self.logger.info("Sending daily summary...")
        for user_id in self.authorized_users:
            # Check if user has daily summary enabled
            if not self.db.is_daily_summary_enabled(user_id):
                self.logger.info(f"Skipping daily summary for user {user_id} (disabled)")
                continue
            try:
                summary = self._build_summary_for_user(user_id)
                await app.bot.send_message(
                    chat_id=user_id,
                    text=summary,
                    parse_mode="HTML",
                )
                self.logger.info(f"Sent daily summary to user {user_id}")
            except Exception as e:
                self.logger.error(f"Failed to send daily summary to {user_id}: {e}")

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
        app.add_handler(CommandHandler("edit", self.edit_reminder))
        app.add_handler(CommandHandler("settime", self.settime_reminder))
        app.add_handler(CommandHandler("copy", self.copy_reminder))
        app.add_handler(CommandHandler("summary", self.summary_command))
        app.add_handler(CommandHandler("dailysummary", self.dailysummary_toggle))
        app.add_handler(CommandHandler("recurring", self.recurring_reminders))
        app.add_handler(CommandHandler("stoprecurring", self.stop_recurring))
        app.add_handler(CommandHandler("setrecurrence", self.set_recurrence_command))
        app.add_handler(CommandHandler("changelog", self.changelog_command))
        app.add_handler(CallbackQueryHandler(self.handle_button_callback))
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

        # Set up daily summary job at configured time (default 8:00 AM)
        daily_summary_config = self.config.get("daily_summary", {})
        if daily_summary_config.get("enabled", True):
            summary_time_str = daily_summary_config.get("time", "08:00")
            try:
                hour, minute = map(int, summary_time_str.split(":"))
                summary_time = time(hour=hour, minute=minute, tzinfo=self.tz)
                job_queue.run_daily(
                    lambda ctx: asyncio.create_task(self.send_daily_summary(app)),
                    time=summary_time,
                )
                self.logger.info(f"Daily summary scheduled at {summary_time_str}")
            except ValueError:
                self.logger.error(f"Invalid daily_summary time format: {summary_time_str}")

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
