# Reminder Bot

A simple, privacy-conscious reminder system that works via Telegram.

**Features:**
- Natural language input ("remind me to pay bills tomorrow")
- Smart defaults by category (bills → Saturday, food expiry → day before)
- Multi-user support (you + partner)
- Management commands (/list, /cancel, /delay)

**Architecture:**
- Designed for easy migration from cloud (Oracle) to home (Raspberry Pi)
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

### Step 4: Set Up Oracle Cloud (30-45 minutes)

This gives you a free server that runs 24/7.

#### 4a. Create an Oracle Cloud Account

1. Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/)
2. Click **"Start for free"**
3. Fill in your details
4. You'll need a credit card for verification (you won't be charged)
5. **Important:** Choose a home region close to you (this cannot be changed later)
6. Wait for email confirmation

> **Troubleshooting:** If signup is rejected, try a different card or contact Oracle support. As a backup, you can use Hetzner (~€4/month) instead.

#### 4b. Create a Virtual Machine

1. Log into [Oracle Cloud Console](https://cloud.oracle.com)
2. Click the hamburger menu (☰) → **Compute** → **Instances**
3. Click **"Create Instance"**
4. Configure:
   - **Name:** `reminder-bot` (or anything you like)
   - **Image:** Oracle Linux 8 (default is fine)
   - **Shape:** VM.Standard.E2.1.Micro (this is the "Always Free" option)
5. Under **"Add SSH keys"**:
   - If you don't have SSH keys: Select "Generate a key pair for me" and **download both keys**
   - If you have SSH keys: Upload your public key
6. Click **"Create"**
7. Wait for the instance to be "Running" (2-3 minutes)
8. Note the **Public IP address** shown on the instance details page

#### 4c. Connect to Your Server

**On Mac/Linux** (Terminal):
```bash
# If you downloaded keys from Oracle, first fix permissions:
chmod 400 ~/Downloads/ssh-key-*.key

# Connect (replace with your actual IP and key path):
ssh -i ~/Downloads/ssh-key-2024-01-15.key opc@YOUR_PUBLIC_IP
```

**On Windows** (PowerShell or use PuTTY):
```powershell
ssh -i C:\Users\YourName\Downloads\ssh-key.key opc@YOUR_PUBLIC_IP
```

> **First time connecting?** Type `yes` when asked about the fingerprint.

### Step 5: Install the Bot on Your Server (15 minutes)

Run these commands one at a time after connecting via SSH:

```bash
# Update system packages
sudo dnf update -y

# Install Python 3.11 and git
sudo dnf install -y python3.11 python3.11-pip git

# Clone the repository
git clone https://github.com/YOUR_USERNAME/reminder-system-claude.git
cd reminder-system-claude

# Install Python dependencies
pip3.11 install -r requirements.txt

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
```

Save and exit: Press `Ctrl+X`, then `Y`, then `Enter`

### Step 7: Test the Bot (2 minutes)

```bash
python3.11 bot.py
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

Paste this content (replace `YOUR_USERNAME` with your actual Oracle username, usually `opc`):
```ini
[Unit]
Description=Reminder Bot
After=network.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/reminder-system-claude
ExecStart=/usr/bin/python3.11 /home/opc/reminder-system-claude/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

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

### Step 9: Open Firewall (if needed)

Oracle Cloud has a firewall. The bot uses outbound connections only, so it should work. But if you have issues:

```bash
# This is usually not needed, but just in case:
sudo firewall-cmd --permanent --add-port=443/tcp
sudo firewall-cmd --reload
```

---

## Using the Bot

### Creating Reminders

Just message naturally:
- "remind me to pay the electricity bill"
- "check if the milk expires tomorrow"
- "call mom at 3pm"
- "dentist appointment next tuesday at 2pm"

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show help |
| `/list` | Show all pending reminders |
| `/cancel <id>` | Cancel a reminder |
| `/delay <id> <hours>` | Delay a reminder by X hours |

### Smart Defaults

If you don't specify a time:

| Category | Default Time |
|----------|--------------|
| Bills/payments | Saturday 9 AM |
| Food expiry | Day before at 9 AM and 6 PM |
| Everything else | Next day 9 AM |

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

---

## Cost Summary

| Item | Cost |
|------|------|
| Oracle Cloud | $0 (Always Free tier) |
| Gemini API | $0 (Free tier, generous limits) |
| Telegram | $0 |
| **Total** | **$0/month** |
