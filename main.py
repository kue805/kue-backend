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

# ── HELPER: build task prompt ─────────────────────────────
def build_task_prompt(company_name: str, goal: str, user_phone: str, context: dict = None):
    product_type = context.get("product_type", "mobile") if context else "mobile"
    account_type = context.get("account_type", "personal") if context else "personal"
    known_questions = context.get("known_questions", []) if context else []

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
        f"You are calling {company_name} on behalf of a customer. Goal: {goal}\n\n"
        f"BUTTON PRESSING RULES:\n"
        f"- When you decide to press a button, execute it immediately and silently\n"
        f"- Do NOT announce what you are pressing, just press it\n\n"
        f"ANSWERING SCREENING QUESTIONS:\n"
        f"{known_qa}\n"
        f"- If asked for account number: say 'I do not have that information available'\n"
        f"- If offered callback vs hold: press 3 or say you want to hold for a representative\n"
        f"- If asked about text or link offers: say 'No, I need to speak with a human agent'\n"
        f"- If stuck or in a loop: press 0\n"
        f"- If asked why calling: say '{goal}'\n\n"
        f"WHEN HUMAN ANSWERS:\n"
        f"Say nothing and wait silently. The call will be transferred automatically.\n\n"
        f"You have maximum 8 minutes. Navigate efficiently."
    )


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

    return {
        "status": "call_initiated",
        "call_id": result.get("call_id"),
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

    return {
        "status": "discovery_initiated",
        "call_id": result.get("call_id"),
        "company": company["name"],
        "mode": "discovery"
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
    transcript = body.get("transcript", "")

    print(f"Webhook received: call_id={call_id} status={status}")

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{SUPABASE_URL}/rest/v1/call_logs",
            headers=HEADERS_DB,
            json={
                "company_id": metadata.get("company_id"),
                "goal": metadata.get("goal"),
                "outcome": "success" if status == "completed" else "partial",
                "discovery_mode": metadata.get("mode") == "discovery",
                "transcript": transcript
            }
        )

    return {"received": True}