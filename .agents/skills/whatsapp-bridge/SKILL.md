---
name: whatsapp-bridge
description: >-
  Send and read personal WhatsApp messages, look up WhatsApp contacts/chats,
  or queue replies via the user's self-hosted WhatsApp + Gemini Bridge on
  Google Cloud (VM whatsapp-gemini-vm) and the two-way synced Google Sheet.
  Activate this skill whenever the user asks to send a WhatsApp message, check
  or summarize recent WhatsApp messages, or interact with their WhatsApp bridge.
---

# Personal WhatsApp + Gemini Bridge Skill (Template)

Copy this skill to your global AI agent skills directory (e.g., `~/.gemini/config/skills/whatsapp-bridge/`) and configure `scripts/wa_cli.py` with your `PROJECT_ID`, `VM_NAME`, and `SELF_CHAT_ID`.

## Quick Commands

### 1. Send a WhatsApp Message
```bash
python scripts/wa_cli.py send --to "self" --text "Hello!"
python scripts/wa_cli.py send --to "41791234567@c.us" --text "Hello from Gemini!"
```

### 2. Read Recent WhatsApp Messages
```bash
python scripts/wa_cli.py recent --limit 20
```

### 3. Search WhatsApp Chats / Contacts
```bash
python scripts/wa_cli.py chats --search "Name"
```

### 4. Mandatory Error & Clarification Rule
- Whenever a requested recipient contact name is not found or matches multiple contacts, or if you need any clarification before sending a message, **always send a WhatsApp message directly to the owner's self-chat (`--to self`)** explaining what needs clarification and attaching the drafted message so the owner can simply reply with the phone number or confirmation.

---

## Using WhatsApp from the Gemini Web & Mobile App (Any Device / Surface)

Because the Gemini Web App (`gemini.google.com`) and the Gemini Mobile App (iPhone/Android) run in Google's cloud rather than on your local computer, they connect to your WhatsApp Bridge through your **two-way Google Sheet**:

1. **Add to Saved Info in the Gemini App (`gemini.google.com/saved-info`)**:
   ```text
   My personal WhatsApp is connected 24/7 to my Google Cloud bridge and synced with my Google Sheet named "WhatsApp Gemini Sync" (Spreadsheet ID: <YOUR_GOOGLE_SHEET_ID>).
   - Whenever I ask about my WhatsApp messages, contacts, or how the bridge works, use @Google Drive to read the "AI Skill & Instructions", "Messages", and "Contacts" tabs of "WhatsApp Gemini Sync".
   - My own WhatsApp Chat ID ("Message Yourself") is <YOUR_PHONE_NUMBER>@c.us.
   - If there is ever an error, an unknown contact phone number, or a need for clarification, always notify me on my WhatsApp self-chat (<YOUR_PHONE_NUMBER>@c.us) with the drafted message and ask for clarification.
   ```
2. **Queueing Replies or `!ai` Commands from Any Device via Google Sheets**:
   - In the **`Pending Replies`** tab of your Google Sheet, add a row:
     - **Column A**: `!ai` (for a natural-language AI command), a saved Contact Name, or a Phone Number / `@c.us` Chat ID
     - **Column B**: Your instruction or message text
     - **Column C**: `PENDING`
   - Within 20 seconds, your GCP `e2-micro` bridge automatically executes the request, sends the WhatsApp message (or messages your self-chat if clarification is needed), and updates Column C/D!
