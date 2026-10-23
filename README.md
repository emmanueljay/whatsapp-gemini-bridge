# All-in-One Personal WhatsApp + Gemini AI Bridge (`e2-micro` Always Free Tier)

Turn your personal WhatsApp into a 24/7 AI assistant powered by **Google Gemini (`gemini-3.8-flash` via Vertex AI)**—running entirely on a single **Google Cloud Compute Engine `e2-micro` VM** within the **Google Cloud Always Free tier**.

---

## Part 1: Human-Readable Overview

### What Does This Project Do?
Once deployed, your WhatsApp account gains a private, cloud-hosted AI companion that works even when your computer is turned off:
1. **Chat with Gemini Inside WhatsApp ("Message Yourself")**:
   Open your own WhatsApp **"Message Yourself"** chat and send any message starting with `!ai` (for example: *`!ai Can you send a WhatsApp message with lots of emojis to Nadine explaining that I now use Gemini?`*).
2. **Built-in Contact Lookup & Outbound Messaging Tools**:
   Gemini can search your WhatsApp contacts/chats, save new contact numbers, read recent messages, and send messages to friends or colleagues on your behalf using a **human-like anti-ban typing cadence** (`sendSeen` $\rightarrow$ `startTyping` $\rightarrow$ proportional human delay $\rightarrow$ `stopTyping` $\rightarrow$ `sendText`).
3. **Automatic Clarification & Error Alerts**:
   If you ask Gemini to message someone whose phone number isn't cached yet (or if anything goes wrong), Gemini **never guesses**—it immediately replies to your WhatsApp self-chat with a drafted message preview and asks you for clarification (e.g., their `+41...` or `+33...` phone number). Once you reply with the number, it saves the contact and delivers the message automatically.
4. **Two-Way Google Sheets Sync (Works with the Gemini Web & Mobile App)**:
   Every message is logged to a Google Sheet (`Messages` and `Contacts` tabs) so you can ask the **Gemini Web/Mobile App (`gemini.google.com`)** to summarize your WhatsApp chats via `@Google Drive`, or queue outbound replies and `!ai` commands in the `Pending Replies` tab.

### What You Need Before Starting (Human Checklist)
You don't need to run the terminal commands yourself—**your AI coding agent (Antigravity, Claude Code, Cursor, etc.) can set up the entire project for you!** Before handing this repo to your AI agent, make sure you have:
1. **A Google Cloud Account** with a billing account linked (the `e2-micro` VM in `us-central1`, `us-west1`, or `us-east1` with a 20 GB Standard Persistent Disk falls within the Google Cloud Always Free tier).
2. **`gcloud` CLI installed and logged in** (`gcloud auth login`), or tell your AI agent to install and authenticate it for you.
3. **Your phone with WhatsApp** ready to scan a QR code once at the end (`WhatsApp` $\rightarrow$ `Settings` $\rightarrow$ `Linked Devices` $\rightarrow$ `Link a Device`).

### Copy-Paste Prompt for Your AI Agent
Copy and paste this into your AI coding assistant:
> *"Please clone `https://github.com/emmanueljay/whatsapp-gemini-bridge`, read the **AI Agent Autonomous Setup Guide** in `README.md`, and set up and deploy my personal WhatsApp + Gemini Bridge on Google Cloud from start to finish."*

---

## Part 2: AI Agent Autonomous Setup Guide

> **Instructions for the AI Agent**: Follow these steps autonomously on behalf of the user. Execute all `gcloud`, `docker`, and configuration commands yourself, and only pause to ask the user when they need to scan the WhatsApp QR code or optionally share a Google Sheet.

### Step 1: Verify `gcloud` Authentication & Create a Dedicated GCP Project
1. Check `gcloud auth list` and `gcloud billing accounts list` to find the user's active Google account and open billing account ID (`BILLING_ACCOUNT_ID`).
2. Create a new Google Cloud project (e.g., `whatsapp-gemini-<random-suffix>`) and link it to the active billing account:
   ```bash
   gcloud projects create <PROJECT_ID> --name="WhatsApp Gemini Bridge"
   gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
   ```
3. Enable the required Google Cloud APIs on `<PROJECT_ID>`:
   ```bash
   gcloud services enable \
     compute.googleapis.com \
     aiplatform.googleapis.com \
     generativelanguage.googleapis.com \
     sheets.googleapis.com \
     secretmanager.googleapis.com \
     logging.googleapis.com \
     monitoring.googleapis.com \
     --project=<PROJECT_ID>
   ```

### Step 2: Grant Vertex AI Permissions to the Compute Engine Service Account
Instead of requiring a paid AI Studio API key, authenticate `google-genai` via **Vertex AI (`USE_VERTEX_AI=true`)** using the VM's default Compute Engine Service Account (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`):
```bash
PROJECT_NUM=$(gcloud projects describe <PROJECT_ID> --format="value(projectNumber)")
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:${PROJECT_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### Step 3: Provision the Always-Free `e2-micro` VM
Create an `e2-micro` VM in `us-central1-a` (Always Free eligible region) with a **20 GB `pd-standard`** disk (Always Free includes up to 30 GB `pd-standard`; avoid `pd-balanced`) and both `cloud-platform` and `spreadsheets` OAuth scopes:
```bash
gcloud compute instances create whatsapp-gemini-vm \
  --project=<PROJECT_ID> \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --boot-disk-type=pd-standard \
  --boot-disk-size=20GB \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
```

### Step 4: Bootstrap 2 GB Swap & Docker on the VM
Because an `e2-micro` instance has 1 GB of physical RAM, you **must** configure a 2 GB `/swapfile` before pulling or running Docker containers:
1. Copy the repository files (`docker-compose.yml`, `.env.example`, `setup_vm.sh`, `bridge/`) to `/opt/whatsapp-gemini-bridge` on `whatsapp-gemini-vm` via `gcloud compute scp`.
2. Run `setup_vm.sh` on the VM to create the 2 GB `/swapfile` (`vm.swappiness=10`) and install Docker Engine + Docker Compose plugin:
   ```bash
   gcloud compute ssh whatsapp-gemini-vm --project=<PROJECT_ID> --zone=us-central1-a \
     --command="chmod +x /opt/whatsapp-gemini-bridge/setup_vm.sh && /opt/whatsapp-gemini-bridge/setup_vm.sh"
   ```

### Step 5: Generate `.env` and Launch the Containers
1. Generate a cryptographically random `WAHA_API_KEY` (32-byte hex) and `WAHA_DASHBOARD_PASSWORD`, and write `/opt/whatsapp-gemini-bridge/.env` (`chmod 600`):
   ```ini
   WAHA_API_KEY=<GENERATED_HEX_SECRET>
   WAHA_DASHBOARD_USER=admin
   WAHA_DASHBOARD_PASSWORD=<GENERATED_PASSWORD>
   USE_VERTEX_AI=true
   GCP_PROJECT_ID=<PROJECT_ID>
   GCP_LOCATION=global
   GEMINI_MODEL=gemini-3.8-flash
   ALLOW_SELF_CHAT=true
   TRIGGER_PREFIX=!ai
   GOOGLE_SHEET_ID=<OPTIONAL_SHEET_ID>
   ```
2. Important implementation notes already handled in `docker-compose.yml` and `bridge/main.py`:
   - Uses `devlikeapro/waha:noweb` (lightweight WebSocket image without Chromium) rather than `devlikeapro/waha:latest`.
   - Port `3000` is bound strictly to `127.0.0.1:3000:3000` so the WAHA dashboard is **never** exposed to the public internet.
   - Subscribes to both `message` and `message.any` webhook events (required because outgoing "Message Yourself" messages have `fromMe: true` and only trigger `message.any`).
   - Automatically resolves WhatsApp `@lid` Linked Identity JIDs (used by iPhones in self-chat and multi-device chats) to standard `@c.us` phone numbers via WAHA's `/api/default/lids` endpoint and auto-detects `SELF_CHAT_ID` / `SELF_LID_PREFIX` from the active session.
3. Start the containers:
   ```bash
   gcloud compute ssh whatsapp-gemini-vm --project=<PROJECT_ID> --zone=us-central1-a \
     --command="cd /opt/whatsapp-gemini-bridge && sudo docker compose up -d --build"
   ```

### Step 6: Open an SSH Tunnel & Ask the User to Scan the WhatsApp QR Code
1. Open a background SSH local port-forwarding tunnel from the user's machine (`localhost:3000` $\rightarrow$ VM `localhost:3000`):
   ```bash
   gcloud compute ssh whatsapp-gemini-vm --project=<PROJECT_ID> --zone=us-central1-a -- -N -L 3000:localhost:3000
   ```
2. Provide the user with:
   - Dashboard URL: **`http://localhost:3000/dashboard`**
   - Username (`admin`) and the generated `WAHA_DASHBOARD_PASSWORD`
   - Instructions: Open **WhatsApp** on their phone $\rightarrow$ **Settings** $\rightarrow$ **Linked Devices** $\rightarrow$ **Link a Device**, and scan the QR code shown for the `default` session until its status becomes **`WORKING`**.

### Step 7: Optional Two-Way Google Sheets Sync & Gemini App Integration
If the user wants two-way Google Sheets sync (so they can also read/queue WhatsApp messages from `gemini.google.com` or the Gemini Mobile App):
1. Ask the user to create a blank Google Sheet, share it (Editor access) with `${PROJECT_NUM}-compute@developer.gserviceaccount.com`, and give you the spreadsheet URL/ID.
2. Set `GOOGLE_SHEET_ID=<SHEET_ID>` in `/opt/whatsapp-gemini-bridge/.env` and restart `gemini-bridge`.
3. `gemini-bridge` will automatically create and populate four tabs in the sheet:
   - **`Messages`**: Real-time log of all inbound and outbound WhatsApp messages.
   - **`Contacts`**: Saved contact names and `@c.us` phone numbers.
   - **`Pending Replies`**: Queue outbound messages or `!ai` commands from Google Sheets.
   - **`AI Skill & Instructions`**: Cloud-hosted instructions that the Gemini Web App (`gemini.google.com`) can read via `@Google Drive`.
