# Blog Reference: Building a Reminder Bot with Claude Code

## Overview

This document captures the key moments from building a Telegram reminder bot using Claude Code. The project demonstrates AI-assisted development: collaborative problem-solving, iterative feature development, and debugging complex issues together.

**Tech Stack:** Python, Telegram Bot API, SQLite, Gemini LLM (for natural language parsing)

---

## 1. Chronological Summary

### Phase 1: Core Bot (Previous Session)
- Built initial reminder bot with natural language parsing via Gemini LLM
- SQLite database for persistence
- Category-based smart scheduling (bills → Saturday, food expiry → day before)
- Multi-user support with "remind us" for shared reminders

### Phase 2: Bug Fixes & Reliability
- **Shared reminders bug**: Partner couldn't receive shared notifications
- **Natural language commands failing**: "list", "cancel that" were creating reminders instead of acting
  - Fix: Added keyword fallback detection before LLM call
- **False positive concern**: "remind me to delete my old emails" would trigger cancel
  - Fix: Check for "remind me/us to" pattern first, skip keyword fallback if matched
- **Snooze not working on sent reminders**: `delay_reminder()` only worked on pending
  - Fix: Added `snooze_reminder()` that resets sent reminders back to pending

### Phase 3: UX Improvements
- **Reply-to-edit feature**: Reply to any bot message to act on that reminder without specifying ID
- **/edit command**: Edit reminder text directly
- **Pop formatting**: Made reminder notifications visually distinct with borders and bold text
- **Inline action buttons**: Snooze 15m, Snooze 1h, Done buttons on notifications

### Phase 4: Command Refinements
- **Direct reply for slash commands**: /snooze, /cancel, /delay, /edit now work when replying
- **Argument parsing logic**: Explicit ID always takes precedence over reply context
  - `/snooze 48 10` = snooze reminder 48 for 10 mins (even if replying to different reminder)
  - `/snooze 10` (as reply) = snooze replied reminder for 10 mins
- **Multi-ID cancel**: `/cancel 57 58 59` cancels all three
- **Shorthand support**: "tmr", "nxt wk" recognized in LLM prompt

### Phase 5: /copy Command & Duplicate Handling
- Removed `duplicate` action from LLM (was causing false positives)
- Added `/copy` slash command instead (more controlled)
- Copy only works on pending reminders
- Requires new time or uses category defaults

### Phase 6: Singaporean English Support
- **Bug**: "Remind me Friday if go back Florence to bring back the breastfeeding milk" → scheduled for Wednesday instead of Friday
- **Root cause**: LLM confused by Singaporean English grammar patterns (preposition omission, subject dropping)
- **Fix**: Added language note to LLM prompt explaining Singaporean English patterns

---

## 2. Illustrative Exchanges

### Exchange 1: Debugging the Snooze Bug Together

**User shared screenshot showing /snooze didn't work after reminder was sent**

> **Me:** "Looking at the screenshot, you typed "test this bot" and the bot interpreted it as a **duplicate** action, copying reminder [54] → [56].
>
> **Current behavior:** The LLM parsed "test this bot" as a duplicate request (probably because it loosely matched "that" patterns like "duplicate that").
>
> **Expected behavior:** This should have been a **create** action with task = "test this bot"."

**User:**
> "i think 56 was flagged as a duplicate because that text "test this bot" was identical to an earlier set reminder - but aside from pointing out that it's a duplicate, im not sure if anything else happened. importantly, although 56 and 54 may be identical, 56 was set up after 54 was already sent, so im not sure why the duplicate-nature matters"

*This exchange shows collaborative debugging - I proposed a theory, user corrected with domain knowledge.*

---

### Exchange 2: User Pushing Back on My Design

**Me (proposing /snooze behavior):**
> "With the current implementation, if you reply to a bot message (say, ID 53) with `/snooze 48 10`:
> - It extracts ID **53** from the replied message
> - It interprets `48` as the minutes argument
> - It ignores `10`
> - Result: **Snoozes reminder 53 for 48 minutes**"

**User:**
> "(2) sounds correct to me. the logic should be that:
> - if there's two numerical args provided, then it is assumed to be {id, minutes} regardless of whether it was a direct reply or not
> - if there's only one arg, and it's a direct reply, then the directly-replied-message would be the ID and the single arg would be the snooze duration
> - if there's only one arg without a direct reply, we don't have sufficient info and it should error out - agree?"

*User defined cleaner logic than my initial implementation. Shows human judgment improving AI output.*

---

### Exchange 3: Reframing the Problem (Singlish)

**Me (initially proposing day-of-week detection fix):**
> "**Proposed Fix:** Add keyword fallback that only allows `action=duplicate` if the input explicitly contains words like "duplicate", "copy", or "another one like". Otherwise, force `action=create`."

**User:**
> "feels like the wrong approach. should we add singlish robustness instead?
>
> the intended sentence is meant to break as such: "remind me Friday/if go back florence/to bring back the breastfeeding milk", but the parsing split the sentence wrongly.
>
> given that the users are singaporean, some degree of singlish should be expected, which typically includes slightly unusual phrasing/grammar from standard american or british english, in this case missing preposition"

*User reframed the problem entirely - it wasn't about day detection, it was about linguistic context.*

---

### Exchange 4: Refining the Singlish Approach

**Me (after researching Singlish grammar):**
> "Based on my research, here's what I found about Singlish grammar:
>
> | Pattern | Standard English | Singlish |
> |---------|------------------|----------|
> | Subject dropping | "How do I go?" | "How to go?" |
> | Preposition omission | "go to Florence" | "go Florence" |
> | Topic prominence | "On Friday, remind me" | "Friday remind me" |"

**User:**
> "on second thought, the users will speak closer to standard singaporean english than singlish per se. would you amend the prompt differently? for example, we wouldn't use "lah", "leh", etc. particles with this bot. but the grammatical quirks we might still use, for example the preposition omission"

*Shows iterative refinement - user knows their context better than generic research.*

---

### Exchange 5: Mobile-First Thinking

**User (with screenshot showing broken formatting):**
> "the rendering here for the header looks a bit odd, perhaps we could have it just be the horizontal lines above and below the "Reminder" text, without the vertical "sides" of the box?
>
> i also think the ID emoji is a bit unintuitive, switch that back to plain text (anyway when the reminder is sent, the attention should be on the reminder itself)"

**Later:**
> "can you shorten the length of the reminder border line? it renders ok on desktop but it's too long on mobile. i think we should just shorten the line universally"

*User caught real-world issues from actual usage that I couldn't anticipate.*

---

## 3. Interesting Challenges & Pivots

### Challenge 1: False Positives vs False Negatives Tradeoff
When adding keyword fallback for commands like "cancel" and "list", we had to balance:
- **False negatives**: LLM missing obvious commands ("list" creating a reminder)
- **False positives**: "remind me to delete my old emails" triggering cancel

**Solution:** Check if input starts with "remind me/us to" first - if so, skip keyword fallback entirely.

### Challenge 2: Duplicate Action Causing Confusion
The LLM's `duplicate` action was matching inputs that weren't duplicate requests at all. "test this bot" triggered duplicate because it matched a previous reminder's text.

**Pivot:** Removed `duplicate` from LLM entirely. Created explicit `/copy` command instead. More predictable, less magic.

### Challenge 3: Direct Reply Context vs Explicit Arguments
When user types `/snooze 10` as a reply to reminder 53, what does "10" mean?
- The reminder ID? (snooze reminder 10 for default 15 mins)
- The duration? (snooze reminder 53 for 10 mins)

**Solution:** User-defined rule: "Explicit always wins"
- Two args = always {id, duration}
- One arg + reply = use reply ID, arg is duration
- One arg alone = ambiguous, error

### Challenge 4: Singaporean English Grammar
The LLM (trained primarily on US/UK English) struggled with Singaporean English patterns:
- "remind me Friday" (missing "on")
- "if go back Florence" (missing "I" and "to")

**Pivot:** Instead of adding deterministic day-of-week detection, we added linguistic context to the LLM prompt. More generalizable, addresses root cause.

---

## 4. Key Takeaways for Blog

1. **AI as collaborator, not replacement**: The best solutions came from combining AI's technical capabilities with human domain knowledge

2. **Iterative refinement works**: First solution is rarely the best. User feedback on actual usage revealed issues (mobile formatting, grammar patterns)

3. **Know when to use LLM vs deterministic logic**:
   - LLM for fuzzy parsing (natural language → structured data)
   - Deterministic fallback for reliability (keyword detection for common commands)
   - Slash commands for precise control (/copy instead of LLM duplicate detection)

4. **Context matters**: Singaporean English fix was more valuable than generic "day detection" because it addressed the actual user base

5. **User knows their domain**: User caught that "singlish" was too broad - they actually use "Singaporean English" (grammatical quirks but no particles like "lah")

---

## 5. Features Built (Final State)

### Commands
- `/list` - Show pending reminders
- `/cancel <id> [id2...]` - Cancel one or more reminders
- `/delay <id> <hours>` - Delay reminder
- `/snooze <id> [mins]` - Snooze (default 15 mins)
- `/edit <id> <text>` - Edit reminder text
- `/copy <id> <time>` - Copy reminder to new time
- `/changelog` - Show version history
- `/help` - Show help

### Natural Language
- "remind me to buy groceries tomorrow"
- "remind us to call the plumber" (shared)
- "cancel that last reminder"
- Time shorthands: tmr, nxt wk

### UX Features
- Pop formatting with horizontal borders
- Inline buttons: Snooze 15m, Snooze 1h, Done
- Direct reply support for all commands
- Singaporean English grammar support

### Technical
- SQLite persistence
- Gemini LLM for parsing
- Category-based default scheduling
- Multi-user authorization
