# Changelog

All notable changes to this project will be documented in this file.

## [1.6.1] - 2026-03-07

### Added
- **Time-of-day validation guardrail**: Automatically corrects when LLM misses explicit time in messages like "11am check skincare"
  - Scans first few words for time patterns (11am, 3pm, 6:30pm, etc.)
  - Shows "⚡ (time auto-corrected)" indicator when correction is applied
- Enhanced LLM prompt with time-fronting examples for better accuracy

### Changed
- **Upgraded default Gemini model** from `gemini-2.5-flash-lite` to `gemini-2.5-flash` for better parsing quality (250 req/day free tier)

## [1.6.0] - 2026-03-07

### Added
- **Recurring reminders**: Create reminders that automatically repeat
  - Natural language: "remind me every day at 9am to take vitamins"
  - Patterns: daily, weekly, biweekly, monthly, yearly, weekdays, weekends
- `/recurring` command: View all active recurring reminders
- `/stoprecurring <id>` command: Stop a recurring reminder series
- Automatic creation of next occurrence when a recurring reminder is sent
- LLM parsing for recurrence patterns with keyword fallback
- **Time-of-day defaults**: Explicit rules for "tomorrow afternoon" (14:00), "today/this morning/afternoon/evening", and "tomorrow night" (20:00)
- **Append to reminders**: Use `/edit <id> ++ text` to append instead of replace
- **Natural language search**: Ask "do I have a reminder for X?" to find matching reminders

### Changed
- Past time-of-day references (e.g., "this morning" when it's afternoon) now roll over to next day

## [1.5.9] - 2026-02-14

### Added
- `/settime` now accepts day names (e.g., `/settime 171 thu 9pm`)
- `/settime` now accepts today/tomorrow variants (e.g., `/settime 171 tmr 9am`)
- Post-validation handles "tonight", "tonite", "2nite"
- Post-validation handles two-word phrases: "this morning", "this afternoon", "this evening"
- Smart scope extension: if "this" detected in first words, extends check by one word

## [1.5.8] - 2026-02-10

### Added
- Post-validation now handles "today" and "tomorrow" (and variants: tdy, tmr, tml, tmrw)
- Auto-corrects when LLM schedules wrong date despite explicit today/tomorrow reference

## [1.5.7] - 2026-02-04

### Added
- LLM now reports time parsing confidence (0-100)
- Shows `/settime` hint when confidence <20%

### Changed
- "Processing..." message now auto-deletes after parsing completes

## [1.5.6] - 2026-02-04

### Added
- `/settime <id> <time>` command - set exact time using explicit format (no LLM)
- Supports both 24hr (18:00) and AM/PM (6pm) formats
- Supports date formats: DD/MM, DD/MM/YY, or time-only for today

## [1.5.5] - 2026-02-04

### Improved
- LLM prompt now handles "X weeks/days before [date/month]" patterns (e.g., "2 weeks before March")

## [1.5.4] - 2026-02-02

### Added
- Day-of-week post-processing validation: auto-corrects LLM parsing errors when user specifies a day name (e.g., "fri 3pm" scheduled on wrong day)
- Limited scope validation: only triggers when day name is in first 2 words (or words 2-4 after "remind me/us")
- User notification: shows "⚡ (day-of-week auto-corrected)" in confirmation when correction was applied

## [1.5.3] - 2026-02-02

### Added
- `/dailysummary on|off` command - each user can toggle their own 8 AM summary
- User preferences stored in database (no restart needed)

## [1.5.2] - 2026-01-27

### Added
- "Snooze 1d" button on reminder notifications (snooze for 24 hours)

### Changed
- Shortened snooze button labels (15m, 1h, 1d) to fit on mobile

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

## [1.3.0] - 2026-01-16

### Added
- **Pop formatting**: Reminder notifications now use bold formatting for better visibility
- **Inline action buttons**: Quick-action buttons on reminder notifications:
  - "Snooze 15m" - snooze for 15 minutes
  - "Snooze 1h" - snooze for 1 hour
  - "Done" - dismiss the reminder

### Changed
- Reminder notifications now use HTML formatting for cleaner display
- Buttons replace the snooze hint text for a cleaner, more interactive experience

## [1.2.0] - 2026-01-15

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

## [1.1.0] - 2026-01-10

### Added
- Shared reminders: Say "remind us" to notify all authorized users
- `/snooze <id> [mins]` command (default 15 minutes)
- Natural language cancel/delete (e.g., "cancel that last reminder")
- Smart context parsing - bot remembers recent reminders for reference
- Keyword fallback for common commands (list, cancel, delete)

### Fixed
- Snooze now works on already-sent reminders
- "remind me to delete my emails" no longer triggers cancel

## [1.0.0] - 2026-01-06

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
