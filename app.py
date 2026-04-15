"""
Missed-Call Text-Back Agent
----------------------------
When someone calls your Twilio number and the business doesn't answer,
this agent automatically texts the caller, has an AI conversation to
collect their name + reason for calling, and notifies the business owner.

Setup instructions: see SETUP.md
"""

import os
import json
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.rest import Client
import anthropic
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)

# --- Clients ---
twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# --- Config ---
BUSINESS_PHONE  = os.getenv("BUSINESS_PHONE")   # The real phone number to ring first
TWILIO_PHONE    = os.getenv("TWILIO_PHONE")      # Your Twilio number
BUSINESS_NAME   = os.getenv("BUSINESS_NAME", "our team")
OWNER_PHONE     = os.getenv("OWNER_PHONE")       # Where to send lead notifications
RING_TIMEOUT    = int(os.getenv("RING_TIMEOUT", "20"))  # Seconds before giving up

# In-memory conversation store.
# For production (multiple clients), swap this for a simple database like SQLite or Redis.
conversations: dict[str, list[dict]] = {}
lead_collected: set[str] = set()  # Track which callers we've already notified about


# ---------------------------------------------------------------------------
# VOICE ROUTES
# ---------------------------------------------------------------------------

@app.route("/voice", methods=["POST"])
def handle_incoming_call():
    """
    Twilio calls this when someone rings your Twilio number.
    We forward the call to the real business number.
    If no answer, /call-status handles it.
    """
    response = VoiceResponse()
    dial = Dial(action="/call-status", timeout=RING_TIMEOUT, method="POST")
    dial.number(BUSINESS_PHONE)
    response.append(dial)
    return Response(str(response), mimetype="text/xml")


@app.route("/call-status", methods=["POST"])
def call_status():
    """
    Twilio calls this after the forwarded call ends.
    If the business didn't pick up, we fire off the first text.
    """
    dial_status = request.form.get("DialCallStatus", "")
    caller       = request.form.get("From", "")

    if dial_status in ("no-answer", "busy", "failed", "canceled"):
        _start_conversation(caller)

    return Response("<Response/>", mimetype="text/xml")


# ---------------------------------------------------------------------------
# SMS ROUTE
# ---------------------------------------------------------------------------

@app.route("/sms", methods=["POST"])
def handle_sms():
    """
    Twilio calls this when the caller texts back.
    The AI continues the conversation and, once it has the lead info,
    notifies the business owner.
    """
    from_number  = request.form.get("From", "")
    message_body = request.form.get("Body", "").strip()

    # If we somehow get a text without a prior call, start fresh
    if from_number not in conversations:
        _start_conversation(from_number)

    # Append user message
    conversations[from_number].append({"role": "user", "content": message_body})

    # Get AI reply
    reply = _get_ai_reply(from_number)

    # Send the reply back
    twilio_client.messages.create(
        body=reply,
        from_=TWILIO_PHONE,
        to=from_number,
    )

    # Check if AI has collected enough info to notify the business
    _maybe_notify_owner(from_number)

    return Response("<Response/>", mimetype="text/xml")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _start_conversation(caller: str):
    """Initialise the conversation and send the first text."""
    opening = (
        f"Hi! Sorry we missed your call at {BUSINESS_NAME}. "
        "We don't want to leave you hanging — what can we help you with today? "
        "Just reply here and we'll get back to you shortly."
    )

    system_prompt = f"""You are a warm, friendly receptionist for {BUSINESS_NAME}.
Someone just called and you missed them. Your job:
1. Find out their name (ask naturally, not like a form).
2. Understand what they need / why they called.
3. Let them know someone from the team will call them back soon.
4. Keep messages short — this is SMS, not email.
5. Once you have their name AND the reason for their call, include the following
   JSON block on its own line at the END of your message (hidden from the user
   by adding it after "---LEAD---"):

---LEAD---
{{"name": "<name>", "reason": "<reason>", "phone": "{caller}"}}

Only include that block once, when you have both pieces of info."""

    conversations[caller] = [
        {"role": "system",    "content": system_prompt},
        {"role": "assistant", "content": opening},
    ]

    twilio_client.messages.create(
        body=opening,
        from_=TWILIO_PHONE,
        to=caller,
    )


def _get_ai_reply(from_number: str) -> str:
    """Call Claude and return the assistant's message (stripping the LEAD block)."""
    msgs = conversations[from_number]

    # Claude takes system prompt separately from the messages list
    system_prompt = next((m["content"] for m in msgs if m["role"] == "system"), "")
    chat_msgs = [m for m in msgs if m["role"] != "system"]

    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system_prompt,
        messages=chat_msgs,
    )
    full_reply = response.content[0].text.strip()

    # Store the full reply (with lead block) in history
    conversations[from_number].append({"role": "assistant", "content": full_reply})

    # Strip the LEAD block before sending to the customer
    visible_reply = full_reply.split("---LEAD---")[0].strip()
    return visible_reply


def _maybe_notify_owner(from_number: str):
    """
    Scan conversation for the LEAD block and, if found, SMS the business owner.
    Only fires once per caller.
    """
    if from_number in lead_collected:
        return

    for msg in conversations[from_number]:
        if "---LEAD---" in msg.get("content", ""):
            try:
                json_part = msg["content"].split("---LEAD---")[1].strip()
                lead = json.loads(json_part)
                _notify_owner(lead)
                lead_collected.add(from_number)
            except (json.JSONDecodeError, IndexError):
                pass  # AI hasn't formatted it perfectly yet — wait for next turn
            break


def _notify_owner(lead: dict):
    """Send a lead notification SMS to the business owner."""
    if not OWNER_PHONE:
        print(f"[LEAD] {lead}")  # Fallback: just log it
        return

    timestamp = datetime.now().strftime("%d %b %H:%M")
    notification = (
        f"📞 New lead ({timestamp})\n"
        f"Name: {lead.get('name', 'Unknown')}\n"
        f"Phone: {lead.get('phone', 'Unknown')}\n"
        f"Reason: {lead.get('reason', 'Not specified')}\n"
        f"Call them back!"
    )
    twilio_client.messages.create(
        body=notification,
        from_=TWILIO_PHONE,
        to=OWNER_PHONE,
    )
    print(f"[LEAD NOTIFIED] {lead}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
