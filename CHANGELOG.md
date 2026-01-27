# Changelog

All notable changes to this project will be documented in this file.

## [1.5.1] - 2026-01-27

### Changed
- "Still pending" section now only shows reminders sent within the last 7 days
- Prevents old pre-feature reminders from cluttering the daily summary

## [1.5.0] - 2026-01-26

### Added
- **Daily summary**: Automatic morning digest at 8 AM (configurable)
  - Shows today's scheduled reminders
  - Shows sent reminders not yet marked as Done
- `/summary` command for on-demand daily summary
- **Done button now functional**: Pressing Done marks reminder as acknowledged in database
- **Privacy/ownership**: Each user only sees their own reminders + shared ones

### Changed
- `/list` now shows only your reminders + shared reminders (not partner's private reminders)
- All commands (`/cancel`, `/snooze`, `/delay`, `/edit`, `/copy`) now respect ownership
- Snoozing a reminder resets its acknowledged status

## [1.3.0] - 2025-01-16

### Added
- **Pop formatting**: Reminder notifications now use bold formatting for better visibility
- **Inline action buttons**: Quick-action buttons on reminder notifications:
  - "Snooze 15m" - snooze for 15 minutes
  - "Snooze 1h" - snooze for 1 hour
  - "Done" - dismiss the reminder

### Changed
- Reminder notifications now use HTML formatting for cleaner display
- Buttons replace the snooze hint text for a cleaner, more interactive experience

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
