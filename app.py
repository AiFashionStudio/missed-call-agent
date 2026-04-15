"""
Missed-Call Text-Back Agent
----------------------------
When someone calls your Twilio number and the business doesn't answer,
this agent automatically texts the caller, has an AI conversation to
collect their name + reason for calling, and notifies the business owner.
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

twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

BUSINESS_PHONE  = os.getenv("BUSINESS_PHONE")
TWILIO_PHONE    = os.getenv("TWILIO_PHONE")
BUSINESS_NAME   = os.getenv("BUSINESS_NAME", "our team")
OWNER_PHONE     = os.getenv("OWNER_PHONE")
RING_TIMEOUT    = int(os.getenv("RING_TIMEOUT", "20"))

conversations = {}
lead_collected = set()


@app.route("/voice", methods=["POST"])
def handle_incoming_call():
    response = VoiceResponse()
    dial = Dial(action="/call-status", timeout=RING_TIMEOUT, method="POST")
    dial.number(BUSINESS_PHONE)
    response.append(dial)
    return Response(str(response), mimetype="text/xml")


@app.route("/call-status", methods=["POST"])
def call_status():
    dial_status = request.form.get("DialCallStatus", "")
    caller       = request.form.get("From", "")
    if dial_status in ("no-answer", "busy", "failed", "canceled"):
        _start_conversation(caller)
    return Response("<Response/>", mimetype="text/xml")


@app.route("/sms", methods=["POST"])
def handle_sms():
    from_number  = request.form.get("From", "")
    message_body = request.form.get("Body", "").strip()
    if from_number not in conversations:
        _start_conversation(from_number)
    conversations[from_number].append({"role": "user", "content": message_body})
    reply = _get_ai_reply(from_number)
    twilio_client.messages.create(body=reply, from_=TWILIO_PHONE, to=from_number)
    _maybe_notify_owner(from_number)
    return Response("<Response/>", mimetype="text/xml")


def _start_conversation(caller):
    opening = (
        f"Hi! Sorry we missed your call at {BUSINESS_NAME}. "
        "What can we help you with? Reply here and we'll call you back!"
    )
    system_prompt = (
        f"You are a friendly receptionist for {BUSINESS_NAME}. "
        "Someone just called and you missed them. Your job: "
        "1. Find out their name naturally. "
        "2. Understand what they need. "
        "3. Tell them someone will call back soon. "
        "4. Keep SMS replies SHORT - under 100 chars. "
        "5. Once you have name AND reason, add at the END: "
        "---LEAD--- "
        '{{"name":"<name>","reason":"<reason>","phone":"' + caller + '"}} '
        "Only include that once."
    )
    conversations[caller] = [
        {"role": "system", "content": system_prompt},
        {"role": "assistant", "content": opening},
    ]
    twilio_client.messages.create(body=opening, from_=TWILIO_PHONE, to=caller)


def _get_ai_reply(from_number):
    msgs = conversations[from_number]
    system_prompt = next((m["content"] for m in msgs if m["role"] == "system"), "")
    chat_msgs = [m for m in msgs if m["role"] != "system"]
    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=system_prompt,
        messages=chat_msgs,
    )
    full_reply = response.content[0].text.strip()
    conversations[from_number].append({"role": "assistant", "content": full_reply})
    return full_reply.split("---LEAD---")[0].strip()


def _maybe_notify_owner(from_number):
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
                pass
            break


def _notify_owner(lead):
    if not OWNER_PHONE:
        print(f"[LEAD] {lead}")
        return
    timestamp = datetime.now().strftime("%d %b %H:%M")
    notification = (
        f"New lead ({timestamp}): "
        f"{lead.get('name','?')} / {lead.get('phone','?')} - "
        f"{lead.get('reason','?')}"
    )
    twilio_client.messages.create(body=notification, from_=TWILIO_PHONE, to=OWNER_PHONE)
    print(f"[LEAD NOTIFIED] {lead}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
