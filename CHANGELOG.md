# Changelog

All notable changes to this project will be documented in this file.

## [1.8.0] - 2026-07-19

### Fixed
- **Multi-line content / URLs dropped from reminders (#7)**: The LLM prompt had no instruction about multi-line messages, so lines after a line break (e.g. links pasted below a task) were silently dropped. Added a MULTI-LINE CONTENT PRESERVATION prompt section plus a deterministic `_restore_dropped_urls` safety net that re-appends any URL present in the raw input but missing from the parsed task.
- **Reminder push notifications showed no useful preview (#8)**: `send_due_reminders` used to lead with decorative `━━━` lines and a generic "🔔 Reminder" header, so the phone's notification preview showed zero signal about what the reminder was for. The task text now goes on line 1. Task text is now `html.escape`d everywhere it meets a `parse_mode="HTML"` send/edit (reminder notifications, done/stop-series button confirmations, `/recurring` list, `/summary`) so a task like "buy &lt;milk&gt;" no longer breaks the send.

## [1.7.1] - 2026-05-24

### Changed
- Default Gemini model updated from `gemini-3.1-flash-lite-preview` to `gemini-3.1-flash-lite` ahead of the preview model's discontinuation on May 25, 2026. The GA model is architecturally identical; no prompt or logic changes were required.

## [1.7.0] - 2026-04-12

### Added
- **Multi-task lists**: Send a numbered list (e.g., "10pm:\n1. Buy milk\n2. Call mom") and each item becomes a separate reminder at the same time
- **Stop Series button**: Recurring reminder notifications now show a 🛑 Stop button to stop the entire series in one tap
- **Relative snooze buttons**: New "+1d" and "+1w" buttons snooze from the original scheduled time (not current time) — a 9am reminder tapped at 3pm reschedules to 9am next day

### Changed
- **Snooze buttons reworked**: Replaced `15m | 1h | 1d` with `1h | 1d | +1d | +1w`
- `/cancel` on a sent recurring reminder now auto-stops the whole series (previously said "already sent or cancelled")
- `/stoprecurring` now accepts any reminder ID in a series (parent or child) and cancels all pending instances

### Fixed
- **Task text time-stripping**: LLM no longer strips content-time references from task text (e.g., "tmr 1030am: car wash at 1130am" now preserves "car wash at 1130am" instead of just "car wash")

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
