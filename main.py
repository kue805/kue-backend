from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx
import os
import json

load_dotenv()

app = FastAPI(title="kue. API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
BLAND_API_KEY = os.getenv("BLAND_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

HEADERS_DB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# ── HEALTH CHECK ──────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "kue. backend running"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── HELPER: fetch IVR tree from Supabase ──────────────────
async def get_ivr_tree(company_id: str):
    async with httpx.AsyncClient() as client:
        tree_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/ivr_trees?company_id=eq.{company_id}&limit=1",
            headers=HEADERS_DB
        )
        trees = tree_res.json()
        if not trees:
            return None, []

        tree_id = trees[0]["id"]

        steps_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/ivr_steps?tree_id=eq.{tree_id}&order=step_number.asc",
            headers=HEADERS_DB
        )
        steps = steps_res.json()
        return trees[0], steps

# ── HELPER: fetch company from Supabase ───────────────────
async def get_company(company_id: str):
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/companies?id=eq.{company_id}&limit=1",
            headers=HEADERS_DB
        )
        data = res.json()
        return data[0] if data else None

# ── HELPER: normalize goal ────────────────────────────────
def normalize_goal(goal: str) -> str:
    goal_lower = goal.lower().strip()
    if any(x in goal_lower for x in ["talk to a human", "speak to someone", "talk to someone", "human", "agent", "representative"]):
        return "customer service"
    if any(x in goal_lower for x in ["technical", "tech support", "not working", "broken", "outage", "internet", "signal"]):
        return "technical support"
    if any(x in goal_lower for x in ["bill", "billing", "payment", "charge", "pay my bill", "dispute"]):
        return "billing"
    if any(x in goal_lower for x in ["cancel", "cancellation", "disconnect"]):
        return "cancel service"
    if any(x in goal_lower for x in ["upgrade", "new phone", "new device", "new service"]):
        return "upgrade"
    return goal

# ── HELPER: normalize phone number ───────────────────────
def normalize_phone(phone: str) -> str:
    digits = "".join(filter(str.isdigit, phone))
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone

# ── HELPER: build task prompt ─────────────────────────────
def build_task_prompt(company_name: str, goal: str, user_phone: str, context: dict = None):
    product_type = context.get("product_type", "mobile") if context else "mobile"
    account_type = context.get("account_type", "personal") if context else "personal"
    known_questions = context.get("known_questions", []) if context else []

    normalized = normalize_goal(goal)

    if known_questions:
        known_qa = "\n".join([
            f"- If asked '{q['question']}': answer '{q['answer']}'"
            for q in known_questions
        ])
    else:
        known_qa = (
            f"- If asked which product: answer '{product_type}'\n"
            f"- If asked personal or business: answer '{account_type}'"
        )

    return (
        f"You are calling {company_name} on behalf of a customer. Goal: {normalized}\n\n"
        f"BUTTON PRESSING RULES:\n"
        f"- When you decide to press a button, execute it immediately and silently\n"
        f"- Do NOT announce what you are pressing, just press it\n\n"
        f"ANSWERING SCREENING QUESTIONS:\n"
        f"{known_qa}\n"
        f"- If asked for account number: say 'I do not have that information available'\n"
        f"- If offered callback vs hold: press 3 or say you want to hold for a representative\n"
        f"- If asked about text or link offers: say 'No, I need to speak with a human agent'\n"
        f"- If the IVR asks what you need and none of the options match: say 'customer service'\n"
        f"- If stuck or in a loop: press 0\n"
        f"- If asked why calling: say '{normalized}'\n\n"
        f"WHEN HUMAN ANSWERS:\n"
        f"Immediately say 'One moment please, transferring now.' then go completely silent.\n"
        f"Do not say anything else after that.\n\n"
        f"You have maximum 8 minutes. Navigate efficiently."
    )

# ── HELPER: update call status ────────────────────────────
async def update_call_status(call_id: str, company_id: str, status: str):
    if not call_id:
        return
    async with httpx.AsyncClient() as client:
        check = await client.get(
            f"{SUPABASE_URL}/rest/v1/call_status?call_id=eq.{call_id}&limit=1",
            headers=HEADERS_DB
        )
        existing = check.json()

        if existing:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/call_status?call_id=eq.{call_id}",
                headers={**HEADERS_DB, "Prefer": "return=minimal"},
                json={"status": status}
            )
        else:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/call_status",
                headers=HEADERS_DB,
                json={
                    "call_id": call_id,
                    "company_id": company_id,
                    "status": status
                }
            )

# ── TREE MAPPER: parse transcript into IVR steps ─────────
async def map_transcript_to_tree(company_id: str, transcript: str, goal: str):
    if not transcript or not company_id:
        return

    print(f"Tree Mapper running for company {company_id}")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Below is a transcript of an AI navigating an IVR phone system.\n"
                            f"Extract only the real IVR navigation steps.\n\n"
                            f"Ignore: hold music, advertisements, company announcements, "
                            f"AI waiting messages.\n\n"
                            f"Include only: menu prompts heard and button presses or spoken responses.\n\n"
                            f"Respond with JSON only:\n"
                            f"{{\n"
                            f"  \"steps\": [\n"
                            f"    {{\n"
                            f"      \"step_number\": 1,\n"
                            f"      \"prompt_text\": \"what the IVR said\",\n"
                            f"      \"action_type\": \"press or say or wait\",\n"
                            f"      \"action_value\": \"the button pressed or words spoken\"\n"
                            f"    }}\n"
                            f"  ]\n"
                            f"}}\n\n"
                            f"TRANSCRIPT:\n{transcript[:3000]}"
                        )
                    }
                ]
            },
            timeout=20.0
        )
        result = response.json()

    try:
        text = result["content"][0]["text"]
        parsed = json.loads(text)
        steps = parsed.get("steps", [])
    except Exception as e:
        print(f"Tree Mapper parse error: {e}")
        return

    if not steps:
        print("Tree Mapper: no steps extracted")
        return

    print(f"Tree Mapper: extracted {len(steps)} steps")

    async with httpx.AsyncClient() as client:
        tree_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/ivr_trees?company_id=eq.{company_id}"
            f"&department=eq.discovery&limit=1",
            headers=HEADERS_DB
        )
        existing_trees = tree_res.json()

        if existing_trees:
            tree_id = existing_trees[0]["id"]
            await client.delete(
                f"{SUPABASE_URL}/rest/v1/ivr_steps?tree_id=eq.{tree_id}"
                f"&source_tag=eq.discovery",
                headers=HEADERS_DB
            )
        else:
            tree_result = await client.post(
                f"{SUPABASE_URL}/rest/v1/ivr_trees",
                headers=HEADERS_DB,
                json={"company_id": company_id, "department": "discovery"}
            )
            tree_data = tree_result.json()
            tree_id = tree_data[0]["id"]

        for step in steps:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/ivr_steps",
                headers=HEADERS_DB,
                json={
                    "tree_id": tree_id,
                    "step_number": step.get("step_number"),
                    "prompt_text": step.get("prompt_text"),
                    "action_type": step.get("action_type", "say"),
                    "action_value": step.get("action_value", ""),
                    "confidence_score": 30,
                    "source_tag": "discovery",
                    "discovery_mode": True
                }
            )

    print(f"Tree Mapper: complete. {len(steps)} steps saved for company {company_id}")


# ── POST /navigate — Autopilot call with known tree ───────
@app.post("/navigate")
async def navigate(request: Request):
    body = await request.json()
    company_id = body.get("company_id")
    user_phone = body.get("user_phone")
    goal = body.get("goal", "reach a human agent")
    context = body.get("context", {})

    company = await get_company(company_id)
    if not company:
        return {"error": "Company not found"}

    tree, steps = await get_ivr_tree(company_id)

    if not steps:
        return await discovery_start_logic(company, user_phone, goal, context)

    task = build_task_prompt(company["name"], goal, user_phone, context)

    async with httpx.AsyncClient() as client:
        bland_res = await client.post(
            "https://api.bland.ai/v1/calls",
            headers={
                "authorization": BLAND_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "phone_number": company["phone"],
                "task": task,
                "transfer_phone_number": user_phone,
                "transfer_list": {
                    "human_detected": user_phone
                },
                "answered_by_enabled": True,
                "wait_for_greeting": True,
                "max_duration": 8,
                "record": True,
                "noise_cancellation": False,
                "interruption_threshold": 500,
                "block_interruptions": False,
                "model": "base",
                "language": "babel-en",
                "voicemail_action": "hangup",
                "webhook": "https://kue-backend-7b61.onrender.com/bland-webhook",
                "metadata": {
                    "company_id": company_id,
                    "goal": goal,
                    "mode": "autopilot"
                }
            }
        )
        result = bland_res.json()
        print(f"Bland response: {bland_res.status_code} - {result}")

    new_call_id = result.get("call_id")
    if new_call_id:
        await update_call_status(new_call_id, company_id, "navigating")

    return {
        "status": "call_initiated",
        "call_id": new_call_id,
        "company": company["name"],
        "mode": "autopilot"
    }


# ── POST /discovery-start — AI navigates blind ────────────
@app.post("/discovery-start")
async def discovery_start(request: Request):
    body = await request.json()
    company_id = body.get("company_id")
    user_phone = body.get("user_phone")
    goal = body.get("goal", "reach a human agent")
    context = body.get("context", {})

    company = await get_company(company_id)
    if not company:
        return {"error": "Company not found"}

    return await discovery_start_logic(company, user_phone, goal, context)


async def discovery_start_logic(company, user_phone, goal, context=None):
    task = build_task_prompt(company["name"], goal, user_phone, context)

    async with httpx.AsyncClient() as client:
        bland_res = await client.post(
            "https://api.bland.ai/v1/calls",
            headers={
                "authorization": BLAND_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "phone_number": company["phone"],
                "task": task,
                "transfer_phone_number": user_phone,
                "transfer_list": {
                    "human_detected": user_phone
                },
                "answered_by_enabled": True,
                "wait_for_greeting": True,
                "max_duration": 8,
                "record": True,
                "noise_cancellation": False,
                "interruption_threshold": 500,
                "block_interruptions": False,
                "model": "base",
                "language": "babel-en",
                "voicemail_action": "hangup",
                "webhook": "https://kue-backend-7b61.onrender.com/bland-webhook",
                "metadata": {
                    "company_id": company.get("id"),
                    "goal": goal,
                    "mode": "discovery"
                }
            }
        )
        result = bland_res.json()
        print(f"Bland discovery response: {bland_res.status_code} - {result}")

    new_call_id = result.get("call_id")
    if new_call_id:
        await update_call_status(new_call_id, company.get("id"), "navigating")

    return {
        "status": "discovery_initiated",
        "call_id": new_call_id,
        "company": company["name"],
        "mode": "discovery"
    }


# ── POST /navigate-custom — call any phone number ─────────
@app.post("/navigate-custom")
async def navigate_custom(request: Request):
    body = await request.json()
    user_phone = body.get("user_phone")
    target_phone = body.get("target_phone")
    company_name = body.get("company_name", "this company")
    goal = body.get("goal", "reach a human agent")
    context = body.get("context", {})

    if not target_phone or not user_phone:
        return {"error": "user_phone and target_phone are required"}

    target_phone = normalize_phone(target_phone)
    task = build_task_prompt(company_name, goal, user_phone, context)

    async with httpx.AsyncClient() as client:
        bland_res = await client.post(
            "https://api.bland.ai/v1/calls",
            headers={
                "authorization": BLAND_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "phone_number": target_phone,
                "task": task,
                "transfer_phone_number": user_phone,
                "transfer_list": {
                    "human_detected": user_phone
                },
                "answered_by_enabled": True,
                "wait_for_greeting": True,
                "max_duration": 8,
                "record": True,
                "noise_cancellation": False,
                "interruption_threshold": 500,
                "block_interruptions": False,
                "model": "base",
                "language": "babel-en",
                "voicemail_action": "hangup",
                "webhook": "https://kue-backend-7b61.onrender.com/bland-webhook",
                "metadata": {
                    "company_id": None,
                    "company_name": company_name,
                    "goal": goal,
                    "mode": "custom"
                }
            }
        )
        result = bland_res.json()
        print(f"Custom call response: {bland_res.status_code} - {result}")

    new_call_id = result.get("call_id")
    if new_call_id:
        await update_call_status(new_call_id, None, "navigating")

    return {
        "status": "call_initiated",
        "call_id": new_call_id,
        "company": company_name,
        "mode": "custom"
    }


# ── POST /hold-takeover — take over an existing hold ──────
@app.post("/hold-takeover")
async def hold_takeover(request: Request):
    body = await request.json()
    user_phone = body.get("user_phone")
    target_phone = body.get("target_phone")
    company_name = body.get("company_name", "this company")
    company_id = body.get("company_id")
    department = body.get("department", "customer service")
    context = body.get("context", {})

    if not target_phone or not user_phone:
        return {"error": "user_phone and target_phone are required"}

    target_phone = normalize_phone(target_phone)

    task = (
        f"You are taking over a hold for a customer waiting with {company_name} "
        f"for the {department} department.\n\n"
        f"IMPORTANT: You are NOT navigating an IVR from scratch. "
        f"The customer was already connected and placed on hold.\n\n"
        f"Your job:\n"
        f"- If you hear hold music or silence: wait patiently and silently\n"
        f"- If the IVR plays menu options: press 0 or say 'representative' to get back to hold\n"
        f"- If a human answers: immediately say 'One moment please, transferring now' then go silent\n"
        f"- The call transfers to the customer automatically when a human is detected\n\n"
        f"Do not navigate menus unless forced to. Just wait silently until a human picks up.\n"
        f"You have up to 12 minutes."
    )

    async with httpx.AsyncClient() as client:
        bland_res = await client.post(
            "https://api.bland.ai/v1/calls",
            headers={
                "authorization": BLAND_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "phone_number": target_phone,
                "task": task,
                "transfer_phone_number": user_phone,
                "transfer_list": {
                    "human_detected": user_phone
                },
                "answered_by_enabled": True,
                "wait_for_greeting": True,
                "max_duration": 12,
                "record": True,
                "noise_cancellation": False,
                "interruption_threshold": 500,
                "block_interruptions": False,
                "model": "base",
                "language": "babel-en",
                "voicemail_action": "hangup",
                "webhook": "https://kue-backend-7b61.onrender.com/bland-webhook",
                "metadata": {
                    "company_id": company_id,
                    "company_name": company_name,
                    "goal": f"hold takeover for {department}",
                    "mode": "hold_takeover"
                }
            }
        )
        result = bland_res.json()
        print(f"Hold takeover response: {bland_res.status_code} - {result}")

    new_call_id = result.get("call_id")
    if new_call_id:
        await update_call_status(new_call_id, company_id, "navigating")

    return {
        "status": "hold_takeover_initiated",
        "call_id": new_call_id,
        "company": company_name,
        "mode": "hold_takeover"
    }


# ── POST /reason-goal — Claude infers department ──────────
@app.post("/reason-goal")
async def reason_goal(request: Request):
    body = await request.json()
    company_name = body.get("company_name", "this company")
    problem = body.get("problem", "")

    if not problem:
        return {"error": "No problem description provided"}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"A customer is calling {company_name} "
                            f"with this problem: \"{problem}\"\n\n"
                            f"Respond with JSON only, no other text:\n"
                            f"{{\n"
                            f"  \"department\": \"most appropriate department\",\n"
                            f"  \"goal\": \"clear one-sentence goal for the call\",\n"
                            f"  \"product_type\": \"mobile or landline or internet or insurance or other\",\n"
                            f"  \"account_type\": \"personal or business\",\n"
                            f"  \"urgency\": \"normal or urgent\",\n"
                            f"  \"suggested_questions\": [\n"
                            f"    {{\"question\": \"likely screening question\", "
                            f"\"answer\": \"best answer based on context\"}}\n"
                            f"  ]\n"
                            f"}}"
                        )
                    }
                ]
            },
            timeout=15.0
        )
        result = response.json()

    try:
        text = result["content"][0]["text"]
        parsed = json.loads(text)
        return parsed
    except Exception as e:
        print(f"Claude parse error: {e} - {result}")
        return {
            "department": "customer service",
            "goal": f"speak with a representative about: {problem}",
            "product_type": "mobile",
            "account_type": "personal",
            "urgency": "normal",
            "suggested_questions": []
        }


# ── POST /bland-webhook — receives call events ────────────
@app.post("/bland-webhook")
async def bland_webhook(request: Request):
    body = await request.json()
    call_id = body.get("call_id")
    status = body.get("status")
    metadata = body.get("metadata", {})
    transcript = body.get("concatenated_transcript") or body.get("transcript", "")
    company_id = metadata.get("company_id")
    goal = metadata.get("goal", "")
    mode = metadata.get("mode", "")
    transferred = body.get("transferred", False)

    print(f"Webhook received: call_id={call_id} status={status} transferred={transferred} mode={mode}")

    # Determine call outcome
    if transferred:
        call_outcome = "transferred"
    elif status == "completed":
        call_outcome = "failed"
    else:
        call_outcome = "partial"

    # Log to call_logs
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{SUPABASE_URL}/rest/v1/call_logs",
            headers=HEADERS_DB,
            json={
                "company_id": company_id,
                "goal": goal,
                "outcome": call_outcome,
                "discovery_mode": mode in ["discovery", "hold_takeover"],
                "transcript": transcript
            }
        )

    # Update real-time call status
    if call_id:
        await update_call_status(call_id, company_id, call_outcome)

    # Run Tree Mapper on Discovery AND Hold Takeover calls
    if mode in ["discovery", "hold_takeover"] and status == "completed" and company_id and transcript:
        await map_transcript_to_tree(company_id, transcript, goal)

    return {"received": True}