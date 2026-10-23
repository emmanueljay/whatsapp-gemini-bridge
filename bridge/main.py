import asyncio
import logging
import os
import random
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from google import genai
from google.genai import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gemini-bridge")

# --- Environment Configuration ---
WAHA_BASE_URL = os.getenv("WAHA_BASE_URL", "http://waha:3000").rstrip("/")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful, concise personal AI assistant replying via WhatsApp.",
)

ALLOWED_CHAT_IDS = {
    c.strip()
    for c in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
    if c.strip()
}
ALLOW_SELF_CHAT = os.getenv("ALLOW_SELF_CHAT", "true").lower() in ("1", "true", "yes")
TRIGGER_PREFIX = os.getenv("TRIGGER_PREFIX", "!ai").strip()

MAX_REPLIES_PER_MINUTE = int(os.getenv("MAX_REPLIES_PER_MINUTE", "6"))
MAX_REPLIES_PER_HOUR = int(os.getenv("MAX_REPLIES_PER_HOUR", "40"))
CONTEXT_WINDOW_MESSAGES = int(os.getenv("CONTEXT_WINDOW_MESSAGES", "12"))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
SHEETS_POLL_INTERVAL_SECONDS = int(os.getenv("SHEETS_POLL_INTERVAL_SECONDS", "20"))
DB_PATH = os.getenv("DB_PATH", "/app/data/bridge.sqlite3")

# --- Global State ---
reply_timestamps_minute: deque = deque()
reply_timestamps_hour: deque = deque()
bot_sent_message_ids: set = set()
gemini_client: Optional[genai.Client] = None
sheets_service: Any = None
sheets_lock = asyncio.Lock()


SELF_CHAT_ID = os.getenv("SELF_CHAT_ID", "").strip()
SELF_LID_PREFIX = os.getenv("SELF_LID_PREFIX", "").strip()


def normalize_chat_id(chat_id: str) -> str:
    """Convert internal @s.whatsapp.net and self @lid JIDs to standard WAHA @c.us format."""
    if not chat_id:
        return ""
    if SELF_LID_PREFIX and chat_id.startswith(SELF_LID_PREFIX) and chat_id.endswith("@lid") and SELF_CHAT_ID:
        return SELF_CHAT_ID
    if chat_id.endswith("@s.whatsapp.net"):
        return chat_id.replace("@s.whatsapp.net", "@c.us")
    return chat_id


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                msg_id TEXT PRIMARY KEY,
                chat_id TEXT,
                processed_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT,
                chat_id TEXT,
                sender TEXT,
                role TEXT,
                body TEXT,
                timestamp INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, timestamp)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                name TEXT PRIMARY KEY,
                chat_id TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.commit()


def is_duplicate_or_mark(msg_id: str, chat_id: str) -> bool:
    """Return True if msg_id was already processed; otherwise record it and return False."""
    if not msg_id:
        return False
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT 1 FROM processed_messages WHERE msg_id = ?", (msg_id,)
        )
        if cur.fetchone():
            return True
        conn.execute(
            "INSERT INTO processed_messages (msg_id, chat_id, processed_at) VALUES (?, ?, ?)",
            (msg_id, chat_id, now),
        )
        # Keep table compact
        if random.random() < 0.05:
            conn.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?",
                (now - 7 * 86400,),
            )
        conn.commit()
    return False


def log_message(
    msg_id: str, chat_id: str, sender: str, role: str, body: str, timestamp: int
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO messages (msg_id, chat_id, sender, role, body, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (msg_id, chat_id, sender, role, body, timestamp),
        )
        conn.commit()


def get_chat_history(chat_id: str, limit: int = CONTEXT_WINDOW_MESSAGES) -> List[types.Content]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT role, body FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        rows = list(reversed(cur.fetchall()))

    contents: List[types.Content] = []
    for role, body in rows:
        sdk_role = "model" if role == "model" else "user"
        contents.append(
            types.Content(
                role=sdk_role,
                parts=[types.Part.from_text(text=body)],
            )
        )
    return contents


def check_rate_limit() -> bool:
    """Enforce per-minute and per-hour outbound auto-reply caps."""
    now = time.time()
    while reply_timestamps_minute and now - reply_timestamps_minute[0] > 60:
        reply_timestamps_minute.popleft()
    while reply_timestamps_hour and now - reply_timestamps_hour[0] > 3600:
        reply_timestamps_hour.popleft()

    if len(reply_timestamps_minute) >= MAX_REPLIES_PER_MINUTE:
        logger.warning("Rate limit reached: %d replies/min", MAX_REPLIES_PER_MINUTE)
        return False
    if len(reply_timestamps_hour) >= MAX_REPLIES_PER_HOUR:
        logger.warning("Rate limit reached: %d replies/hour", MAX_REPLIES_PER_HOUR)
        return False

    reply_timestamps_minute.append(now)
    reply_timestamps_hour.append(now)
    return True


def init_sheets_service():
    """Initialize Google Sheets API client using VM Application Default Credentials."""
    global sheets_service
    if not GOOGLE_SHEET_ID:
        return None
    if sheets_service is not None:
        return sheets_service
    try:
        import google.auth
        from googleapiclient.discovery import build

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        ensure_sheet_tabs()
        logger.info("Google Sheets two-way sync initialized for sheet %s", GOOGLE_SHEET_ID)
        return sheets_service
    except Exception as exc:
        logger.warning("Could not initialize Google Sheets client: %s", exc)
        return None


def ensure_sheet_tabs() -> None:
    """Ensure 'Messages', 'Pending Replies', 'Contacts', and 'AI Skill & Instructions' tabs exist in the Google Sheet."""
    if not sheets_service or not GOOGLE_SHEET_ID:
        return
    try:
        meta = (
            sheets_service.spreadsheets()
            .get(spreadsheetId=GOOGLE_SHEET_ID)
            .execute()
        )
        existing_titles = {
            s.get("properties", {}).get("title") for s in meta.get("sheets", [])
        }
        requests_body = []
        for tab in ("Messages", "Pending Replies", "Contacts", "AI Skill & Instructions"):
            if tab not in existing_titles:
                requests_body.append({"addSheet": {"properties": {"title": tab}}})
        if requests_body:
            sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=GOOGLE_SHEET_ID, body={"requests": requests_body}
            ).execute()
            if "Messages" not in existing_titles:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range="'Messages'!A1:F1",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            ["Timestamp (UTC)", "Chat ID", "Sender", "Direction", "Message", "Message ID"]
                        ]
                    },
                ).execute()
            if "Pending Replies" not in existing_titles:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range="'Pending Replies'!A1:D1",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            ["Chat ID, Contact Name, or '!ai'", "Reply Text or !ai Instruction", "Status (Leave blank or PENDING)", "Sent Timestamp / AI Response"]
                        ]
                    },
                ).execute()
            if "Contacts" not in existing_titles:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range="'Contacts'!A1:C2",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            ["Contact Name", "Phone / Chat ID (e.g. 41791234567@c.us)", "Notes"],
                            ["Self (Message Yourself)", SELF_CHAT_ID or "self", "Message Yourself"],
                        ]
                    },
                ).execute()
            if "AI Skill & Instructions" not in existing_titles:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=GOOGLE_SHEET_ID,
                    range="'AI Skill & Instructions'!A1:B10",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            ["Topic", "Instructions for Gemini Web / Mobile App & Cloud Bridge"],
                            ["Overview", f"This Google Sheet (ID: {GOOGLE_SHEET_ID}) is synced 24/7 with your Google Cloud WhatsApp + Gemini Bridge VM."],
                            ["Owner Self-Chat ID", f"{SELF_CHAT_ID or 'Auto-detected from WAHA session'} (Message Yourself)"],
                            ["Reading Messages", "Read the 'Messages' tab to view or summarize all incoming and outgoing WhatsApp messages."],
                            ["Looking Up Contacts", "Read the 'Contacts' tab to find phone numbers / @c.us Chat IDs for contacts."],
                            ["Sending via WhatsApp (!ai)", "On WhatsApp ('Message Yourself'), type '!ai <instruction>' (e.g., '!ai Send a message to...'). The cloud VM executes the Python tool-calling script directly."],
                            ["Sending via Google Sheet", "In the 'Pending Replies' tab, add a row: Column A = Chat ID, Contact Name, or '!ai'; Column B = Message text (or natural-language AI instruction if Column A is '!ai'); Column C = PENDING. The VM polls every 20s and executes it."],
                            ["Error & Clarification Rule", "MANDATORY: Whenever a contact is unknown, matches multiple people, or an error occurs, the bridge automatically sends a WhatsApp message to your self-chat explaining the error or asking for clarification (with the drafted message attached)."],
                        ]
                    },
                ).execute()
    except Exception as exc:
        logger.warning("Failed verifying Google Sheet tabs: %s", exc)


async def run_sheets_call(fn):
    """Execute a synchronous Google Sheets API call under a lock with a 10s timeout (httplib2 is not thread-safe)."""
    global sheets_service
    async with sheets_lock:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Google Sheets API call timed out after 10s; resetting client.")
            sheets_service = None
            raise


async def append_message_to_sheet(
    chat_id: str, sender: str, direction: str, body: str, msg_id: str
) -> None:
    if not GOOGLE_SHEET_ID:
        return
    svc = init_sheets_service()
    if not svc:
        return
    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    row = [[ts_str, chat_id, sender, direction, body, msg_id]]
    try:
        await run_sheets_call(
            lambda: svc.spreadsheets()
            .values()
            .append(
                spreadsheetId=GOOGLE_SHEET_ID,
                range="'Messages'!A:F",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": row},
            )
            .execute()
        )
    except Exception as exc:
        logger.warning("Failed appending message to Google Sheet: %s", exc)


def format_phone_or_jid(raw: str) -> str:
    """Normalize a phone number or JID into a valid WAHA @c.us or @g.us chat ID."""
    cleaned = normalize_chat_id(raw.strip())
    if cleaned.lower() in ("self", "me"):
        return SELF_CHAT_ID
    if "@" in cleaned:
        return cleaned
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if digits:
        return f"{digits}@c.us"
    return cleaned


async def fetch_waha_lid_map(client: httpx.AsyncClient) -> Dict[str, str]:
    """Fetch WAHA's internal @lid -> @c.us phone number mapping table."""
    lid_map: Dict[str, str] = {f"{SELF_LID_PREFIX}@lid": SELF_CHAT_ID}
    headers = {"X-Api-Key": WAHA_API_KEY}
    try:
        r_lids = await client.get(
            f"{WAHA_BASE_URL}/api/{WAHA_SESSION}/lids?limit=500", headers=headers
        )
        if r_lids.status_code == 200:
            for item in r_lids.json():
                lid = str(item.get("lid") or "")
                pn = normalize_chat_id(str(item.get("pn") or ""))
                if lid and pn:
                    lid_map[lid] = pn
    except Exception as exc:
        logger.debug("WAHA lids lookup warning: %s", exc)
    return lid_map


async def lookup_contacts_and_chats(query: str) -> List[Dict[str, str]]:
    """Search SQLite contacts, Google Sheet Contacts tab, WAHA chats, and WAHA contacts for query."""
    q = (query or "").strip().lower()
    q_words = [w for w in q.split() if len(w) >= 2] if q else []
    results: Dict[str, str] = {}

    def _matches(text_a: str, text_b: str) -> bool:
        if not q:
            return True
        combined = f"{text_a} {text_b}".lower()
        if q in combined:
            return True
        return any(w in combined for w in q_words)

    # 1. SQLite saved contacts
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute("SELECT name, chat_id FROM contacts").fetchall()
            for name, cid in rows:
                norm_cid = format_phone_or_jid(cid)
                if _matches(name, norm_cid):
                    results[norm_cid] = name
    except Exception as exc:
        logger.debug("SQLite contacts lookup warning: %s", exc)

    # 2. Google Sheet 'Contacts' tab
    if GOOGLE_SHEET_ID:
        svc = init_sheets_service()
        if svc:
            try:
                sheet_res = await run_sheets_call(
                    lambda: svc.spreadsheets()
                    .values()
                    .get(spreadsheetId=GOOGLE_SHEET_ID, range="'Contacts'!A2:C200")
                    .execute()
                )
                for row in sheet_res.get("values", []):
                    if len(row) >= 2 and str(row[0]).strip() and str(row[1]).strip():
                        name = str(row[0]).strip()
                        cid = format_phone_or_jid(str(row[1]).strip())
                        if _matches(name, cid):
                            results[cid] = name
            except Exception as exc:
                logger.debug("Sheet Contacts lookup warning: %s", exc)

    # 3. WAHA active chats and contacts store (with @lid -> @c.us mapping)
    headers = {"X-Api-Key": WAHA_API_KEY}
    async with httpx.AsyncClient(timeout=10.0) as client:
        lid_map = await fetch_waha_lid_map(client)
        try:
            r_chats = await client.get(
                f"{WAHA_BASE_URL}/api/{WAHA_SESSION}/chats?limit=200", headers=headers
            )
            if r_chats.status_code == 200:
                for c in r_chats.json():
                    raw_id = str(c.get("id") or "")
                    cid = lid_map.get(raw_id, normalize_chat_id(raw_id))
                    if cid.endswith("@lid"):
                        continue
                    name = str(c.get("name") or c.get("pushname") or "").strip()
                    if _matches(name, cid):
                        if cid and (cid not in results or not results[cid] or results[cid] == cid):
                            results[cid] = name or cid
        except Exception as exc:
            logger.debug("WAHA chats lookup warning: %s", exc)

        try:
            r_contacts = await client.get(
                f"{WAHA_BASE_URL}/api/contacts/all?session={WAHA_SESSION}",
                headers=headers,
            )
            if r_contacts.status_code == 200:
                for c in r_contacts.json():
                    raw_id = str(c.get("id") or "")
                    cid = lid_map.get(raw_id, normalize_chat_id(raw_id))
                    if cid.endswith("@lid"):
                        continue
                    name = (
                        str(c.get("name") or c.get("pushname") or c.get("shortName") or "").strip()
                    )
                    if not name and q and not _matches("", cid):
                        continue
                    if _matches(name, cid):
                        if cid and (cid not in results or results[cid] == cid):
                            results[cid] = name or cid
        except Exception as exc:
            logger.debug("WAHA contacts lookup warning: %s", exc)

    return [{"chat_id": cid, "name": name} for cid, name in results.items()]


async def save_contact_entry(name: str, phone_or_chat_id: str, notes: str = "") -> Dict[str, str]:
    """Persist a contact name and phone/JID to SQLite and the Google Sheet Contacts tab."""
    clean_name = name.strip()
    clean_cid = format_phone_or_jid(phone_or_chat_id)
    now_ts = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO contacts (name, chat_id, updated_at) VALUES (?, ?, ?)",
            (clean_name, clean_cid, now_ts),
        )
        conn.commit()

    if GOOGLE_SHEET_ID:
        svc = init_sheets_service()
        if svc:
            try:
                await run_sheets_call(
                    lambda: svc.spreadsheets()
                    .values()
                    .append(
                        spreadsheetId=GOOGLE_SHEET_ID,
                        range="'Contacts'!A:C",
                        valueInputOption="USER_ENTERED",
                        insertDataOption="INSERT_ROWS",
                        body={"values": [[clean_name, clean_cid, notes or "Saved by Gemini Bridge"]]},
                    )
                    .execute()
                )
            except Exception as exc:
                logger.warning("Failed appending contact to Google Sheet: %s", exc)

    return {"name": clean_name, "chat_id": clean_cid}


async def configure_waha_session() -> None:
    """Ensure the default WAHA session has NOWEB store enabled and auto-detect SELF_CHAT_ID / SELF_LID_PREFIX."""
    global SELF_CHAT_ID, SELF_LID_PREFIX
    headers = {"X-Api-Key": WAHA_API_KEY, "Content-Type": "application/json"}
    session_config = {
        "name": WAHA_SESSION,
        "config": {
            "noweb": {
                "store": {
                    "enabled": True,
                    "fullSync": False,
                }
            },
            "webhooks": [
                {
                    "url": "http://gemini-bridge:8000/webhook/whatsapp",
                    "events": ["message", "message.any", "session.status"],
                }
            ],
        },
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(1, 15):
            try:
                resp = await client.get(
                    f"{WAHA_BASE_URL}/api/sessions/{WAHA_SESSION}", headers=headers
                )
                if resp.status_code == 200:
                    data = resp.json()
                    me_info = data.get("me") or {}
                    if not SELF_CHAT_ID and me_info.get("id"):
                        SELF_CHAT_ID = str(me_info["id"]).replace("@s.whatsapp.net", "@c.us")
                        logger.info("Auto-detected SELF_CHAT_ID: %s", SELF_CHAT_ID)
                    if not SELF_LID_PREFIX and me_info.get("lid"):
                        SELF_LID_PREFIX = str(me_info["lid"]).split(":")[0].split("@")[0]
                        logger.info("Auto-detected SELF_LID_PREFIX: %s", SELF_LID_PREFIX)
                    cfg = data.get("config") or {}
                    store_cfg = (cfg.get("noweb") or {}).get("store") or {}
                    if not store_cfg.get("enabled"):
                        logger.info("Updating WAHA session '%s' to enable NOWEB store...", WAHA_SESSION)
                        await client.put(
                            f"{WAHA_BASE_URL}/api/sessions/{WAHA_SESSION}",
                            headers=headers,
                            json=session_config,
                        )
                    else:
                        logger.info("WAHA session '%s' already has NOWEB store enabled.", WAHA_SESSION)
                    return
                elif resp.status_code == 404:
                    logger.info("Creating WAHA session '%s' with NOWEB store enabled...", WAHA_SESSION)
                    await client.post(
                        f"{WAHA_BASE_URL}/api/sessions",
                        headers=headers,
                        json={**session_config, "start": True},
                    )
                    return
            except Exception as exc:
                logger.debug("Waiting for WAHA container (attempt %d): %s", attempt, exc)
            await asyncio.sleep(3)


async def send_human_like_whatsapp_reply(
    chat_id: str, reply_text: str, reply_to_msg_id: Optional[str] = None
) -> Optional[str]:
    """Send seen, startTyping, wait proportional human delay, stopTyping, and sendText."""
    headers = {"X-Api-Key": WAHA_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        # 1. Mark message as seen
        seen_payload: Dict[str, Any] = {"session": WAHA_SESSION, "chatId": chat_id}
        if reply_to_msg_id:
            seen_payload["messageIds"] = [reply_to_msg_id]
        try:
            await client.post(
                f"{WAHA_BASE_URL}/api/sendSeen", headers=headers, json=seen_payload
            )
        except Exception as exc:
            logger.debug("sendSeen non-fatal warning: %s", exc)

        # 2. Start typing indicator
        try:
            await client.post(
                f"{WAHA_BASE_URL}/api/startTyping",
                headers=headers,
                json={"session": WAHA_SESSION, "chatId": chat_id},
            )
        except Exception as exc:
            logger.debug("startTyping non-fatal warning: %s", exc)

        # 3. Human-like delay (1.5s to 3.5s proportional to length + jitter)
        base_delay = min(3.5, max(1.5, len(reply_text) * 0.015))
        jitter = random.uniform(-0.3, 0.4)
        delay = max(1.5, min(3.5, base_delay + jitter))
        await asyncio.sleep(delay)

        # 4. Stop typing indicator
        try:
            await client.post(
                f"{WAHA_BASE_URL}/api/stopTyping",
                headers=headers,
                json={"session": WAHA_SESSION, "chatId": chat_id},
            )
        except Exception as exc:
            logger.debug("stopTyping non-fatal warning: %s", exc)

        # 5. Send text reply
        send_payload: Dict[str, Any] = {
            "session": WAHA_SESSION,
            "chatId": chat_id,
            "text": reply_text,
        }
        resp = await client.post(
            f"{WAHA_BASE_URL}/api/sendText", headers=headers, json=send_payload
        )
        resp.raise_for_status()
        resp_data = resp.json()
        sent_id = resp_data.get("id")
        if sent_id:
            bot_sent_message_ids.add(sent_id)
            if len(bot_sent_message_ids) > 1000:
                bot_sent_message_ids.pop()
        return sent_id


async def notify_owner_error_or_clarification(notice_text: str) -> None:
    """Send an error or clarification request message directly to Emmanuel's self-chat."""
    try:
        sent_id = await send_human_like_whatsapp_reply(SELF_CHAT_ID, notice_text)
        now_ts = int(time.time())
        log_message(sent_id or f"err_{now_ts}", SELF_CHAT_ID, "me", "model", notice_text, now_ts)
        await append_message_to_sheet(
            SELF_CHAT_ID, "Gemini Bridge (Alert)", "OUTBOUND", notice_text, sent_id or ""
        )
    except Exception as exc:
        logger.error("Failed sending error/clarification alert to owner: %s", exc)


async def poll_pending_sheets_replies() -> None:
    """Background loop that polls the 'Pending Replies' tab in Google Sheets and dispatches messages."""
    while True:
        await asyncio.sleep(SHEETS_POLL_INTERVAL_SECONDS)
        if not GOOGLE_SHEET_ID:
            continue
        svc = init_sheets_service()
        if not svc:
            continue
        try:
            result = await run_sheets_call(
                lambda: svc.spreadsheets()
                .values()
                .get(spreadsheetId=GOOGLE_SHEET_ID, range="'Pending Replies'!A2:D100")
                .execute()
            )
            rows = result.get("values", [])
            for idx, row in enumerate(rows, start=2):
                if len(row) < 2:
                    continue
                raw_chat_id = str(row[0]).strip()
                reply_text = str(row[1]).strip()
                status = str(row[2]).strip().upper() if len(row) >= 3 else ""
                if not raw_chat_id or not reply_text:
                    continue
                if status not in ("", "PENDING", "SEND", "QUEUED"):
                    continue

                if raw_chat_id.lower() in ("!ai", "ai", "gemini"):
                    try:
                        logger.info("Executing queued !ai instruction from Google Sheet (row %d): %s", idx, reply_text)
                        now_ts = int(time.time())
                        log_message(f"sheet_ai_in_{now_ts}", SELF_CHAT_ID, "Sheet (!ai)", "user", reply_text, now_ts)
                        ai_reply = await generate_gemini_reply(SELF_CHAT_ID, reply_text, is_owner=True)
                        if ai_reply:
                            sent_id = await send_human_like_whatsapp_reply(SELF_CHAT_ID, ai_reply)
                            log_message(sent_id or f"sheet_ai_out_{now_ts}", SELF_CHAT_ID, "me", "model", ai_reply, now_ts)
                            asyncio.create_task(append_message_to_sheet(SELF_CHAT_ID, "Gemini Bridge (Sheet !ai)", "OUTBOUND", ai_reply, sent_id or ""))
                        sent_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        await run_sheets_call(
                            lambda r=idx, s=sent_str, resp=(ai_reply or "")[:180]: svc.spreadsheets()
                            .values()
                            .update(
                                spreadsheetId=GOOGLE_SHEET_ID,
                                range=f"'Pending Replies'!C{r}:D{r}",
                                valueInputOption="USER_ENTERED",
                                body={"values": [["COMPLETED", f"[{s}] {resp}"]]},
                            )
                            .execute()
                        )
                    except Exception as ai_exc:
                        await notify_owner_error_or_clarification(
                            f"⚠️ Error running your queued `!ai` sheet instruction (row {idx}):\n`{ai_exc}`"
                        )
                    continue

                # Check if raw_chat_id is a contact name rather than a phone number / JID
                has_letters = any(ch.isalpha() for ch in raw_chat_id.split("@")[0])
                if has_letters and raw_chat_id.lower() not in ("self", "me"):
                    matches = await lookup_contacts_and_chats(raw_chat_id)
                    if len(matches) == 1:
                        chat_id = matches[0]["chat_id"]
                    elif len(matches) == 0:
                        err_msg = (
                            f"⚠️ Clarification needed for queued Google Sheet reply (row {idx}): "
                            f"I couldn't find a phone number or Chat ID for '{raw_chat_id}'. "
                            f"Please reply here with their phone number (e.g. +41... or +33...) or update row {idx} in the Pending Replies sheet."
                        )
                        await notify_owner_error_or_clarification(err_msg)
                        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        await run_sheets_call(
                            lambda r=idx, s=ts_now: svc.spreadsheets()
                            .values()
                            .update(
                                spreadsheetId=GOOGLE_SHEET_ID,
                                range=f"'Pending Replies'!C{r}:D{r}",
                                valueInputOption="USER_ENTERED",
                                body={"values": [["NEEDS_CLARIFICATION", f"Unknown contact '{raw_chat_id}' ({s})"]]},
                            )
                            .execute()
                        )
                        continue
                    else:
                        options_str = ", ".join(f"{m['name']} ({m['chat_id']})" for m in matches[:5])
                        err_msg = (
                            f"⚠️ Clarification needed for queued Google Sheet reply (row {idx}): "
                            f"Multiple contacts matched '{raw_chat_id}': {options_str}. "
                            f"Please specify the exact Chat ID in row {idx} or tell me which one to message."
                        )
                        await notify_owner_error_or_clarification(err_msg)
                        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        await run_sheets_call(
                            lambda r=idx, s=ts_now: svc.spreadsheets()
                            .values()
                            .update(
                                spreadsheetId=GOOGLE_SHEET_ID,
                                range=f"'Pending Replies'!C{r}:D{r}",
                                valueInputOption="USER_ENTERED",
                                body={"values": [["NEEDS_CLARIFICATION", f"Multiple matches ({s})"]]},
                            )
                            .execute()
                        )
                        continue
                else:
                    chat_id = format_phone_or_jid(raw_chat_id)

                try:
                    logger.info("Sending queued Google Sheet reply to %s (row %d)", chat_id, idx)
                    sent_id = await send_human_like_whatsapp_reply(chat_id, reply_text)
                    now_ts = int(time.time())
                    log_message(sent_id or f"sheet_{now_ts}", chat_id, "me", "model", reply_text, now_ts)
                    asyncio.create_task(append_message_to_sheet(chat_id, "me (Sheet Queue)", "OUTBOUND", reply_text, sent_id or ""))

                    sent_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    await run_sheets_call(
                        lambda r=idx, s=sent_str: svc.spreadsheets()
                        .values()
                        .update(
                            spreadsheetId=GOOGLE_SHEET_ID,
                            range=f"'Pending Replies'!C{r}:D{r}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [["SENT", s]]},
                        )
                        .execute()
                    )
                except Exception as send_exc:
                    err_detail = str(send_exc)
                    logger.warning("Failed sending queued sheet row %d to %s: %s", idx, chat_id, err_detail)
                    await notify_owner_error_or_clarification(
                        f"⚠️ Error sending your queued WhatsApp message (row {idx}) to `{chat_id}`:\n"
                        f"`{err_detail}`\n\nPlease check the phone number/Chat ID or let me know how you'd like to proceed."
                    )
                    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    await run_sheets_call(
                        lambda r=idx, s=ts_now, e=err_detail[:80]: svc.spreadsheets()
                        .values()
                        .update(
                            spreadsheetId=GOOGLE_SHEET_ID,
                            range=f"'Pending Replies'!C{r}:D{r}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [["ERROR", f"{e} ({s})"]]},
                        )
                        .execute()
                    )
        except Exception as exc:
            logger.debug("Google Sheets poll error: %s", exc)


OWNER_INSTRUCTIONS = """
IMPORTANT OWNER ASSISTANT INSTRUCTIONS:
You are speaking directly with the owner of this WhatsApp bridge (in their "Message Yourself" self-chat).
You have access to tools to search WhatsApp contacts/chats (`search_whatsapp_contacts`), send WhatsApp messages to other people (`send_whatsapp_message`), save contact numbers (`save_whatsapp_contact`), and read recent messages (`get_recent_whatsapp_messages`).

Rules when the owner asks you to send a WhatsApp message to someone:
1. Call `search_whatsapp_contacts` ONCE with the person's full name if they give a name instead of a phone number. Never call `search_whatsapp_contacts` multiple times for the same person (one search already checks all words in the name).
2. If `search_whatsapp_contacts` returns NO matches (because WAHA's lightweight memory mode only caches contacts after a message is exchanged or after they are saved to Contacts), or returns MULTIPLE ambiguous matches:
   - DO NOT call `search_whatsapp_contacts` again, DO NOT guess or invent a phone number, and DO NOT claim the message was sent.
   - Immediately reply to the owner asking for clarification (e.g., ask for the person's phone number with country code like +41... or +33..., or which contact they meant).
   - Include your drafted message in the reply so the owner can review it and simply reply with the phone number!
3. When the owner provides a contact's phone number, call `save_whatsapp_contact` to remember it for the future, and call `send_whatsapp_message` to deliver the message.
4. If any tool returns an error (`status == "ERROR"`), report the exact error clearly to the owner and ask for clarification or next steps.
"""


async def execute_owner_tool_call(fn_name: str, fn_args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a Gemini tool call requested during an owner/self-chat conversation."""
    try:
        if fn_name == "search_whatsapp_contacts":
            query = str(fn_args.get("query") or "")
            matches = await lookup_contacts_and_chats(query)
            return {
                "matches": matches,
                "count": len(matches),
                "note": (
                    "0 matches found (all saved contacts, Google Sheet contacts, and active WhatsApp chats were already searched for every word in the query). "
                    "DO NOT call search_whatsapp_contacts again! Immediately reply to the owner with your drafted message and ask for the recipient's phone number (with country code, e.g. +41... or +33...)."
                    if len(matches) == 0
                    else "Matches found."
                ),
            }
        elif fn_name == "save_whatsapp_contact":
            name = str(fn_args.get("name") or "").strip()
            phone_or_jid = str(fn_args.get("phone_or_chat_id") or "").strip()
            if not name or not phone_or_jid:
                return {"status": "ERROR", "error": "Both 'name' and 'phone_or_chat_id' are required."}
            saved = await save_contact_entry(name, phone_or_jid)
            return {"status": "SAVED", "contact": saved}
        elif fn_name == "send_whatsapp_message":
            raw_target = str(fn_args.get("recipient_phone_or_chat_id") or "").strip()
            text = str(fn_args.get("message_text") or "").strip()
            if not raw_target or not text:
                return {"status": "ERROR", "error": "Both 'recipient_phone_or_chat_id' and 'message_text' are required."}
            target_cid = format_phone_or_jid(raw_target)
            if not any(ch.isdigit() for ch in target_cid.split("@")[0]):
                return {
                    "status": "ERROR",
                    "error": f"'{raw_target}' is not a valid phone number or @c.us Chat ID. Search contacts first or ask the owner for the phone number.",
                }
            sent_id = await send_human_like_whatsapp_reply(target_cid, text)
            now_ts = int(time.time())
            log_message(sent_id or f"tool_{now_ts}", target_cid, "me", "model", text, now_ts)
            await append_message_to_sheet(target_cid, "Gemini Bridge (Tool)", "OUTBOUND", text, sent_id or "")
            return {"status": "SENT", "recipient_chat_id": target_cid, "message_id": sent_id}
        elif fn_name == "get_recent_whatsapp_messages":
            limit = min(30, max(1, int(fn_args.get("limit") or 10)))
            filter_cid = str(fn_args.get("chat_id") or "").strip()
            with sqlite3.connect(DB_PATH) as conn:
                if filter_cid:
                    norm_filter = format_phone_or_jid(filter_cid)
                    rows = conn.execute(
                        "SELECT timestamp, chat_id, sender, role, body FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                        (norm_filter, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT timestamp, chat_id, sender, role, body FROM messages ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            messages_list = [
                {
                    "timestamp_utc": datetime.fromtimestamp(r[0], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "chat_id": r[1],
                    "sender": r[2],
                    "role": r[3],
                    "body": r[4],
                }
                for r in reversed(rows)
            ]
            return {"messages": messages_list}
        else:
            return {"status": "ERROR", "error": f"Unknown tool: {fn_name}"}
    except Exception as exc:
        logger.exception("Tool %s execution error: %s", fn_name, exc)
        return {"status": "ERROR", "error": str(exc)}


OWNER_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_whatsapp_contacts",
                description="Search saved contacts, Google Sheet Contacts tab, and active WhatsApp chats by name or phone number.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="Name or phone substring to search for (e.g. 'Nadine' or '4179').",
                        )
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="save_whatsapp_contact",
                description="Save a contact's name and phone number / WhatsApp Chat ID so they can be messaged by name in the future.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name": types.Schema(
                            type=types.Type.STRING,
                            description="Full name of the contact (e.g. 'Nadine Volanthen').",
                        ),
                        "phone_or_chat_id": types.Schema(
                            type=types.Type.STRING,
                            description="International phone number or @c.us JID (e.g. '+41791234567' or '41791234567@c.us').",
                        ),
                    },
                    required=["name", "phone_or_chat_id"],
                ),
            ),
            types.FunctionDeclaration(
                name="send_whatsapp_message",
                description="Send a WhatsApp message to a specific phone number or @c.us Chat ID.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "recipient_phone_or_chat_id": types.Schema(
                            type=types.Type.STRING,
                            description="Target phone number with country code or @c.us Chat ID (e.g. '41791234567@c.us'). Never pass a person's name here.",
                        ),
                        "message_text": types.Schema(
                            type=types.Type.STRING,
                            description="The exact WhatsApp message text to send.",
                        ),
                    },
                    required=["recipient_phone_or_chat_id", "message_text"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_recent_whatsapp_messages",
                description="Get recent WhatsApp messages logged by the bridge across all chats or for a specific chat_id.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "chat_id": types.Schema(
                            type=types.Type.STRING,
                            description="Optional chat_id filter (leave empty for all recent messages).",
                        ),
                        "limit": types.Schema(
                            type=types.Type.INTEGER,
                            description="Max number of recent messages to return (default 10).",
                        ),
                    },
                ),
            ),
        ]
    )
]


async def generate_gemini_reply(chat_id: str, user_prompt: str, is_owner: bool = False) -> str:
    global gemini_client
    use_vertex = os.getenv("USE_VERTEX_AI", "true").lower() in ("1", "true", "yes")
    gcp_project = os.getenv("GCP_PROJECT_ID", "").strip()
    gcp_location = os.getenv("GCP_LOCATION", "global").strip()

    if gemini_client is None:
        if use_vertex:
            if not gcp_project:
                try:
                    import google.auth
                    _, detected_project = google.auth.default()
                    gcp_project = detected_project or ""
                except Exception:
                    pass
            gemini_client = genai.Client(
                vertexai=True, project=gcp_project or None, location=gcp_location
            )
        elif GEMINI_API_KEY:
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            return "⚠️ Neither Vertex AI nor GEMINI_API_KEY is configured on the bridge."

    history = get_chat_history(chat_id, limit=CONTEXT_WINDOW_MESSAGES)
    if not history or history[-1].parts[0].text != user_prompt:
        history.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_prompt)],
            )
        )

    model_name = "gemini-3.8-flash" if GEMINI_MODEL in ("gemini-2.5-flash", "gemini-flash-latest") else GEMINI_MODEL
    sys_instruction = f"{SYSTEM_PROMPT}\n\n{OWNER_INSTRUCTIONS}" if is_owner else SYSTEM_PROMPT
    config = types.GenerateContentConfig(
        system_instruction=sys_instruction,
        temperature=0.7,
        tools=OWNER_TOOLS if is_owner else None,
    )

    for _step in range(5):
        if _step > 0:
            # Brief pacing between multi-turn tool calls to avoid Vertex AI per-second DSQ burst limits
            await asyncio.sleep(1.5)

        response = None
        for attempt in range(4):
            try:
                response = await gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=history,
                    config=config,
                )
                break
            except Exception as exc:
                err_str = str(exc)
                if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str) and attempt < 3:
                    wait_sec = 4 * (attempt + 1)
                    logger.warning(
                        "Vertex AI 429 burst limit hit (attempt %d/4); waiting %ds before retry...",
                        attempt + 1,
                        wait_sec,
                    )
                    await asyncio.sleep(wait_sec)
                else:
                    raise

        function_calls = getattr(response, "function_calls", None)
        if not function_calls:
            return (response.text or "").strip()

        # Append the model's tool-call turn
        if response.candidates and response.candidates[0].content:
            history.append(response.candidates[0].content)

        tool_parts: List[types.Part] = []
        for fc in function_calls:
            fn_name = fc.name
            fn_args = dict(fc.args) if fc.args else {}
            logger.info("Executing owner tool call: %s(%s)", fn_name, fn_args)
            result_dict = await execute_owner_tool_call(fn_name, fn_args)
            tool_parts.append(
                types.Part.from_function_response(
                    name=fn_name,
                    response=result_dict,
                )
            )
        history.append(types.Content(role="user", parts=tool_parts))

    return (response.text or "").strip()


async def handle_incoming_message(payload: Dict[str, Any]) -> None:
    msg_id = str(payload.get("id") or "")
    from_me = bool(payload.get("fromMe", False))
    raw_from = str(payload.get("from") or "")
    raw_to = str(payload.get("to") or "")
    body = str(payload.get("body") or "").strip()
    timestamp = int(payload.get("timestamp") or time.time())

    if not body:
        return

    chat_id = normalize_chat_id(raw_from)
    to_id = normalize_chat_id(raw_to)
    if chat_id.endswith("@lid") or to_id.endswith("@lid"):
        async with httpx.AsyncClient(timeout=8.0) as client:
            lid_map = await fetch_waha_lid_map(client)
            chat_id = lid_map.get(chat_id, chat_id)
            to_id = lid_map.get(to_id, to_id)

    # 1. Ignore status broadcasts & channels
    if (
        chat_id == "status@broadcast"
        or chat_id.endswith("@broadcast")
        or chat_id.endswith("@newsletter")
    ):
        return

    # 2. Strict Loop Prevention:
    # Ignore any message sent by this bridge or any outgoing message that isn't a self-chat with trigger prefix
    if msg_id in bot_sent_message_ids:
        return

    is_self_chat = from_me and (
        chat_id == to_id
        or to_id == "me"
        or chat_id == SELF_CHAT_ID
        or raw_from.endswith("@lid")
        or raw_to.endswith("@lid")
    )
    has_trigger = bool(
        TRIGGER_PREFIX and body.lower().startswith(TRIGGER_PREFIX.lower())
    )

    if from_me:
        # Never reply to our own messages unless the user explicitly typed the trigger prefix (e.g. !ai)
        if not has_trigger:
            return

    # 3. Deduplication check
    if is_duplicate_or_mark(msg_id, chat_id):
        logger.debug("Skipping duplicate message ID %s", msg_id)
        return

    # Strip trigger prefix for cleaner prompt if present
    cleaned_body = body
    if has_trigger:
        cleaned_body = body[len(TRIGGER_PREFIX) :].strip()
        if not cleaned_body:
            cleaned_body = "Hello!"

    # 4. Log incoming message to SQLite and optional Google Sheet (non-blocking)
    raw_data = payload.get("_data") or {}
    push_name = str(raw_data.get("pushName") or raw_data.get("notifyName") or "").strip()
    sender_label = f"{push_name} ({chat_id})" if push_name and push_name not in chat_id else str(payload.get("participant") or chat_id)
    log_message(msg_id, chat_id, sender_label, "user", cleaned_body, timestamp)
    asyncio.create_task(append_message_to_sheet(chat_id, sender_label, "INBOUND", body, msg_id))

    # 5. Allowlist / Trigger Gating
    is_allowed_contact = chat_id in ALLOWED_CHAT_IDS
    should_reply = has_trigger or is_allowed_contact or (ALLOW_SELF_CHAT and is_self_chat)

    if not should_reply:
        logger.info("Logged message from %s passively (not in allowlist and no prefix).", chat_id)
        return

    # 6. Rate Limiting Guardrail
    if not check_rate_limit():
        return

    # 7. Call Gemini API & send human-like reply
    is_owner_request = from_me or (chat_id == SELF_CHAT_ID)
    try:
        reply_text = await generate_gemini_reply(
            chat_id, cleaned_body, is_owner=is_owner_request
        )
        if not reply_text:
            if is_owner_request:
                await notify_owner_error_or_clarification(
                    "⚠️ I received your request, but Gemini returned an empty response. Could you clarify or rephrase what you'd like me to do?"
                )
            return
        sent_id = await send_human_like_whatsapp_reply(
            chat_id, reply_text, reply_to_msg_id=msg_id
        )
        now_ts = int(time.time())
        log_message(sent_id or f"bot_{now_ts}", chat_id, "me", "model", reply_text, now_ts)
        asyncio.create_task(append_message_to_sheet(
            chat_id, "Gemini Bridge", "OUTBOUND", reply_text, sent_id or ""
        ))
        logger.info("Replied to %s (%d chars)", chat_id, len(reply_text))
    except Exception as exc:
        logger.exception("Error generating or sending Gemini reply to %s: %s", chat_id, exc)
        await notify_owner_error_or_clarification(
            f"⚠️ Sorry Emmanuel, I ran into an error while processing your request (`{cleaned_body[:80]}`):\n"
            f"`{exc}`\n\nPlease let me know if you'd like to clarify or try again!"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_sheets_service()
    asyncio.create_task(configure_waha_session())
    poll_task = asyncio.create_task(poll_pending_sheets_replies())
    yield
    poll_task.cancel()


app = FastAPI(title="WhatsApp Gemini Bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "model": GEMINI_MODEL,
        "waha_session": WAHA_SESSION,
        "sheets_sync_enabled": bool(GOOGLE_SHEET_ID),
    }


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> Dict[str, str]:
    data = await request.json()
    event = data.get("event")
    if event in ("message", "message.any"):
        payload = data.get("payload") or {}
        background_tasks.add_task(handle_incoming_message, payload)
    elif event == "session.status":
        status = (data.get("payload") or {}).get("status")
        logger.info("WAHA session status changed: %s", status)
    return {"status": "received"}
