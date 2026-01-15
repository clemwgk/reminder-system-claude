# Changelog

All notable changes to this project will be documented in this file.

## [1.2.0] - 2025-01-15

### Added
- `/edit <id> <text>` command to edit reminder text
- **Reply-to-edit feature**: Reply to any bot message to act on that reminder
  - Reply with `/cancel` to cancel
  - Reply with `/snooze` or `/snooze 30` to snooze
  - Reply with `/delay 2` to delay by hours
  - Reply with `/edit new text` to edit
  - Reply with just text to edit (no command needed)
- `/changelog` command to view recent changes

### Changed
- Updated `/help` to include new features

## [1.1.0] - 2025-01-10

### Added
- Shared reminders: Say "remind us" to notify all authorized users
- `/snooze <id> [mins]` command (default 15 minutes)
- Natural language cancel/delete (e.g., "cancel that last reminder")
- Smart context parsing - bot remembers recent reminders for reference
- Keyword fallback for common commands (list, cancel, delete)

### Fixed
- Snooze now works on already-sent reminders
- "remind me to delete my emails" no longer triggers cancel

## [1.0.0] - 2025-01-06

### Added
- Initial release
- Natural language reminder creation via Telegram
- Multiple LLM providers: Gemini, OpenAI, Groq, Ollama
- Category-based smart scheduling:
  - Bills → Saturday 9 AM
  - Food expiry → Day before at 9 AM & 6 PM
  - General → Next day 9 AM
- Multi-user support with authorization
- Commands: `/list`, `/cancel`, `/delay`, `/help`
- SQLite database for persistence
- Systemd service for 24/7 operation
