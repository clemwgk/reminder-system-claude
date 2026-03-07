# Reminder Bot

> **Built with [Claude Code](https://docs.anthropic.com/en/docs/claude-code)** - This entire project was developed through pair programming with Claude, Anthropic's AI assistant. It demonstrates how AI-assisted development can be used to build practical, production-ready applications from scratch.

A simple, privacy-conscious reminder system that works via Telegram.

**Features:**
- Natural language input ("remind me to pay bills tomorrow")
- Smart defaults by category (bills → Saturday, food expiry → day before)
- Multi-user support (you + partner)
- Management commands (/list, /cancel, /delay)

**Architecture:**
- Designed for easy migration from cloud (Google Cloud) to home (Raspberry Pi)
- LLM provider is swappable: Gemini (free cloud) → Ollama (local/private)

---

## Quick Start (Estimated time: 1-2 hours)

### Step 1: Create a Telegram Bot (10 minutes)

1. Open Telegram on your phone
2. Search for `@BotFather` and start a chat
3. Send: `/newbot`
4. Follow the prompts:
   - Choose a name (e.g., "Family Reminders")
   - Choose a username (must end in `bot`, e.g., `my_family_reminder_bot`)
5. BotFather will give you a **token** that looks like:
   ```
   123456789:ABCdefGHIjklmnoPQRstuvWXYz
   ```
6. **Save this token** - you'll need it later

### Step 2: Get Your Telegram User ID (2 minutes)

You'll need your Telegram user ID to authorize yourself. Here's how to find it:

1. Search for `@userinfobot` on Telegram
2. Start a chat and send any message
3. It will reply with your user ID (a number like `123456789`)
4. **Save this number** - you'll need it later
5. Repeat for your partner if they'll use the bot too

### Step 3: Get a Gemini API Key (5 minutes)

1. Go to [Google AI Studio](https://aistudio.google.com)
2. Sign in with your Google account
3. Click **"Get API Key"** (left sidebar or main page)
4. Click **"Create API Key"**
5. Copy the key (looks like `AIzaSy...`)
6. **Save this key** - you'll need it later

### Step 4: Set Up Google Cloud (30-45 minutes)

This gives you a free server that runs 24/7.

#### 4a. Create a Google Cloud Account

1. Go to [cloud.google.com/free](https://cloud.google.com/free)
2. Click **"Get started for free"**
3. Sign in with your Google account
4. You'll need a credit card for verification (you won't be charged for Always Free resources)
5. Complete the signup process

> **Note:** Google gives you $300 free credit for 90 days, but we'll use the "Always Free" tier which is free forever.

#### 4b. Create a Virtual Machine

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. If prompted, create a new project (name it anything, e.g., "reminder-bot")
3. Click the hamburger menu (☰) → **Compute Engine** → **VM instances**
4. If this is your first time, click **"Enable"** and wait ~1 minute
5. Click **"Create Instance"**

**Configure each section carefully (the defaults are NOT free):**

**Name and Region:**
| Setting | What to select |
|---------|----------------|
| Name | `reminder-bot` (or anything you like) |
| Region | **Must be one of:** `us-west1` (Oregon), `us-central1` (Iowa), or `us-east1` (South Carolina) |
| Zone | Any zone within your chosen region (e.g., `us-west1-b`) |

**Machine configuration (IMPORTANT - defaults are not free):**

6. Under "Machine configuration":
   - **Series**: Select **E2** (should be default)
   - **Machine type**: Click the dropdown and select **e2-micro (2 vCPU, 1 GB memory)**

   > ⚠️ **Warning:** The display says "2 vCPU" but e2-micro uses shared CPU time. This IS the free tier option. Do NOT select e2-small or anything else.

**Boot disk (IMPORTANT - must change disk type):**

7. Click **"Change"** next to Boot disk
8. In the popup:
   - **Operating system**: Debian
   - **Version**: Debian GNU/Linux 12 (bookworm)
   - **Boot disk type**: **⚠️ CHANGE THIS** → Select **Standard persistent disk** (NOT "Balanced persistent disk" which is the default and costs money)
   - **Size**: 30 GB (maximum free)
9. Click **"Select"**

**Verify your cost estimate shows $0:**

Before proceeding, check the cost panel on the right side. It should show:
```
Monthly estimate: $0.00
```

If it shows any cost, you've selected something wrong. Common mistakes:
- Wrong region (must be us-west1, us-central1, or us-east1)
- Wrong disk type (must be "Standard persistent disk", not "Balanced")
- Wrong machine type (must be e2-micro)

**Firewall:**

10. Scroll down to "Firewall"
11. Check both:
    - ☑️ Allow HTTP traffic
    - ☑️ Allow HTTPS traffic

**Create:**

12. Click **"Create"** and wait 1-2 minutes
13. You'll see your VM in the list with a green checkmark when ready

> **Free Tier Summary:** 1x e2-micro VM + 30GB standard disk in us-west1/us-central1/us-east1 = $0/month forever. See [Google Cloud Free Tier docs](https://cloud.google.com/free/docs/free-cloud-features) for details.

#### 4c. Connect to Your Server

The easiest way is using Google's built-in SSH:

1. In the VM instances list, find your `reminder-bot` VM
2. Click the **"SSH"** button (under "Connect" column)
3. A new browser window opens with a terminal - you're now connected!

**Alternative: Connect from your own terminal** (optional, for advanced users)

```bash
# Install gcloud CLI first: https://cloud.google.com/sdk/docs/install
gcloud compute ssh reminder-bot --zone=us-west1-b
```

### Step 5: Install the Bot on Your Server (15 minutes)

Run these commands one at a time in the SSH terminal:

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install Python 3.11 and git
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Clone the repository (replace YOUR_USERNAME with your GitHub username)
git clone https://github.com/YOUR_USERNAME/reminder-system-claude.git
cd reminder-system-claude

# Create a virtual environment and install dependencies
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create your config file
cp config.example.yaml config.yaml
```

### Step 6: Configure the Bot (5 minutes)

Edit the config file:
```bash
nano config.yaml
```

Update these values:
```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN_HERE"  # From Step 1
  authorized_users:
    - 123456789  # Your Telegram user ID from Step 2
    - 987654321  # Partner's ID (optional)

llm:
  provider: "gemini"
  gemini:
    api_key: "YOUR_GEMINI_API_KEY"  # From Step 3
    model: "gemini-2.5-flash-lite"  # Free tier: 1000 req/day
```

> **Alternative LLM Providers:** If Gemini doesn't work, you can use OpenAI (`gpt-4o-mini`, ~$0.15/1M tokens) or Groq (`llama-3.3-70b-versatile`, free 14,400 req/day). See `config.example.yaml` for all options.

Save and exit: Press `Ctrl+X`, then `Y`, then `Enter`

### Step 7: Test the Bot (2 minutes)

```bash
# Make sure you're in the right directory with venv activated
cd ~/reminder-system-claude
source venv/bin/activate

# Run the bot
python bot.py
```

Now open Telegram and message your bot:
- Send: `/start`
- Send: `remind me to test this bot in 2 minutes`

You should get a confirmation, then a reminder 2 minutes later!

Press `Ctrl+C` to stop the bot (we'll make it run forever next).

### Step 8: Make the Bot Run Forever (5 minutes)

Create a systemd service so the bot starts automatically and restarts if it crashes:

```bash
# Create service file
sudo nano /etc/systemd/system/reminder-bot.service
```

Paste this content exactly:
```ini
[Unit]
Description=Reminder Bot
After=network.target

[Service]
Type=simple
User=YOUR_GOOGLE_USERNAME
WorkingDirectory=/home/YOUR_GOOGLE_USERNAME/reminder-system-claude
ExecStart=/home/YOUR_GOOGLE_USERNAME/reminder-system-claude/venv/bin/python /home/YOUR_GOOGLE_USERNAME/reminder-system-claude/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Important:** Replace `YOUR_GOOGLE_USERNAME` with your actual username. To find it, run:
```bash
whoami
```
It's usually your Google email without the @gmail.com part, or a name like `your_name`.

Save and exit (`Ctrl+X`, `Y`, `Enter`), then:

```bash
# Reload systemd, enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable reminder-bot
sudo systemctl start reminder-bot

# Check it's running
sudo systemctl status reminder-bot
```

You should see "active (running)" in green.

---

## Using the Bot

### Creating Reminders

Just message naturally:
- "remind me to pay the electricity bill"
- "check if the milk expires tomorrow"
- "call mom at 3pm"
- "dentist appointment next tuesday at 2pm"

### Shared Reminders (Notify Both Partners)

Say "remind us" instead of "remind me" to notify all authorized users:
- "remind us to call the plumber"
- "we need to pick up the package tomorrow"

Shared reminders show a 👥 icon and will be sent to everyone.

### Recurring Reminders

Create reminders that automatically repeat:
- "remind me every day at 9am to take vitamins"
- "remind me weekly on Saturday to water plants"
- "remind me every weekday at 8am to check emails"

Supported patterns:
- **daily** - every day
- **weekly** - same day every week
- **biweekly** - every two weeks
- **monthly** - same day every month
- **yearly** - same day every year
- **weekdays** - Monday through Friday
- **weekends** - Saturday and Sunday

Recurring reminders show a 🔄 icon and automatically schedule the next occurrence when sent.

### Managing Reminders (Natural Language)

You can modify reminders by talking naturally:
- "cancel that last reminder"
- "remove reminder 5"
- "duplicate that for 7pm" (creates a copy at a new time)
- "change reminder 3 to tomorrow"
- "show my reminders"

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show help |
| `/list` | Show all pending reminders |
| `/recurring` | Show recurring reminders |
| `/stoprecurring <id>` | Stop a recurring reminder |
| `/cancel <id>` | Cancel a reminder |
| `/delay <id> <hours>` | Delay a reminder by X hours |
| `/snooze <id> [mins]` | Snooze for 15 mins (or specify duration) |

### Smart Defaults

If you don't specify a time:

| Category | Default Time |
|----------|--------------|
| Bills/payments | Saturday 9 AM |
| Food expiry | Day before at 9 AM and 6 PM |
| Everything else | Next day 9 AM |

---

## Adding Your Partner

To let your partner use the bot (receive shared reminders and create their own):

### Step 1: Get Partner's Telegram User ID

1. Have your partner search for `@userinfobot` on Telegram
2. They send any message to it
3. It replies with their user ID (e.g., `987654321`)

### Step 2: Add Their ID to Config

SSH into your server and edit the config:

```bash
nano ~/reminder-system-claude/config.yaml
```

Add their ID to the `authorized_users` list:

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN"
  authorized_users:
    - 123456789  # Your ID
    - 987654321  # Partner's ID (add this line)
```

Save and exit (`Ctrl+X`, `Y`, `Enter`).

### Step 3: Restart the Bot

```bash
sudo systemctl restart reminder-bot
```

### Step 4: Partner Starts the Bot

Your partner should:
1. Search for your bot on Telegram (by the username you created)
2. Start a chat and send `/start`
3. They can now create reminders and receive shared reminders

**Note:** Both users can create reminders independently. Use "remind us" for shared reminders that notify both partners.

---

## Maintenance

### View Logs
```bash
sudo journalctl -u reminder-bot -f
```

### Restart the Bot
```bash
sudo systemctl restart reminder-bot
```

### Update the Bot
```bash
cd ~/reminder-system-claude
git pull
sudo systemctl restart reminder-bot
```

### Backup Your Reminders
```bash
cp ~/reminder-system-claude/reminders.db ~/reminders-backup-$(date +%Y%m%d).db
```

---

## Future: Migration to Raspberry Pi

When you're ready to move to a Raspberry Pi for more privacy:

1. Set up a Raspberry Pi with Raspberry Pi OS
2. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
3. Pull a model: `ollama pull llama3.2`
4. Clone this repo to the Pi
5. Update `config.yaml`:
   ```yaml
   llm:
     provider: "ollama"  # Changed from "gemini"
   ```
6. Run the bot

Your reminder text will now be processed locally instead of being sent to Google.

---

## Troubleshooting

### "Config file not found"
Make sure you copied `config.example.yaml` to `config.yaml`:
```bash
cp config.example.yaml config.yaml
```

### "You're not authorized"
Add your Telegram user ID to `config.yaml` under `authorized_users`.

### Bot not responding
Check if it's running:
```bash
sudo systemctl status reminder-bot
```

Check logs for errors:
```bash
sudo journalctl -u reminder-bot -n 50
```

### Gemini API errors
- Verify your API key is correct
- Check you haven't exceeded free tier limits (unlikely for personal use)
- Make sure the key is enabled at [Google AI Studio](https://aistudio.google.com)

### SSH Connection Issues
If the browser-based SSH disconnects:
1. Go back to [VM instances](https://console.cloud.google.com/compute/instances)
2. Click the SSH button again
3. Your bot is still running (the systemd service keeps it alive)

### VM Stopped Unexpectedly
Google's free tier VMs can occasionally be preempted. Check:
1. Go to [VM instances](https://console.cloud.google.com/compute/instances)
2. If stopped, click the three dots (⋮) → **Start**
3. The systemd service will auto-start the bot

---

## Cost Summary

| Item | Cost |
|------|------|
| Google Cloud | $0 (Always Free e2-micro in us-west1/us-central1/us-east1) |
| Gemini API (gemini-2.5-flash-lite) | $0 (Free tier: 1,000 req/day) |
| Telegram | $0 |
| **Total** | **$0/month** |

> **Note:** The free tier requires your VM to be in specific US regions. 30GB disk and e2-micro are within free limits. Egress (outbound data) has a free allowance of 1GB/month to most regions, which is plenty for this bot.
>
> **Alternative LLM options:** OpenAI gpt-4o-mini (~$0.01/month for typical usage), Groq (free 14,400 req/day).
