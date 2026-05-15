import requests
import time
import os
import threading
from dotenv import load_dotenv

load_dotenv()

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

HEADERS_BLAND = {
    "authorization": BLAND_API_KEY,
    "Content-Type": "application/json"
}

HEADERS_CLAUDE = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json"
}

def keep_render_awake():
    while True:
        try:
            requests.get("https://kue-backend-7b61.onrender.com/health", timeout=5)
            print("  [ping] Render kept awake")
        except:
            pass
        time.sleep(600)

def get_bland_calls(limit=100):
    """Fetch recent calls from Bland API"""
    res = requests.get(
        f"https://api.bland.ai/v1/calls?limit={limit}",
        headers=HEADERS_BLAND
    )
    if res.status_code == 200:
        return res.json().get("calls", [])
    else:
        print(f"Failed to fetch Bland calls: {res.status_code} {res.text}")
        return []

def get_call_transcript(call_id):
    """Fetch full transcript for a specific call"""
    res = requests.get(
        f"https://api.bland.ai/v1/calls/{call_id}",
        headers=HEADERS_BLAND
    )
    if res.status_code == 200:
        data = res.json()
        # Use concatenated_transcript first, fall back to transcripts list
        transcript = data.get("concatenated_transcript", "")
        if not transcript:
            transcripts = data.get("transcripts", [])
            if isinstance(transcripts, list):
                lines = []
                for entry in transcripts:
                    role = entry.get("role", "").upper()
                    text = entry.get("text", "")
                    if text:
                        lines.append(f"{role}: {text}")
                transcript = "\n".join(lines)
        return transcript
    return ""
def get_companies():
    """Get all companies from Supabase"""
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/companies?select=id,name,phone",
        headers=HEADERS_DB
    )
    return {c["id"]: c for c in res.json()}

def map_transcript_to_tree(company_id, company_name, transcript):
    """Use Claude to extract IVR steps from transcript and save to Supabase"""
    if not transcript or not transcript.strip():
        print(f"  ⚠ Empty transcript for {company_name}")
        return False

    print(f"  Running Tree Mapper for {company_name}...")

    # Ask Claude to extract IVR steps
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=HEADERS_CLAUDE,
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 800,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Below is a transcript of an AI navigating an IVR phone system for {company_name}.\n"
                        f"Extract only the real IVR navigation steps.\n\n"
                        f"Ignore: hold music, advertisements, company announcements, AI waiting messages.\n\n"
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
        timeout=30
    )

    result = res.json()
    try:
        import json
        text = result["content"][0]["text"]
        # Strip markdown code blocks if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        steps = parsed.get("steps", [])
    except Exception as e:
        print(f"  ✗ Claude parse error: {e}")
        return False

    if not steps:
        print(f"  ⚠ No steps extracted for {company_name}")
        return False

    print(f"  ✓ Extracted {len(steps)} steps")

    # Check if discovery tree already exists
    tree_res = requests.get(
        f"{SUPABASE_URL}/rest/v1/ivr_trees?company_id=eq.{company_id}&department=eq.discovery&limit=1",
        headers=HEADERS_DB
    )
    existing = tree_res.json()

    if existing:
        tree_id = existing[0]["id"]
        # Delete old discovery steps
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/ivr_steps?tree_id=eq.{tree_id}&source_tag=eq.discovery",
            headers=HEADERS_DB
        )
    else:
        # Create new discovery tree
        tree_result = requests.post(
            f"{SUPABASE_URL}/rest/v1/ivr_trees",
            headers=HEADERS_DB,
            json={"company_id": company_id, "department": "discovery"}
        )
        tree_id = tree_result.json()[0]["id"]

    # Insert steps
    for step in steps:
        requests.post(
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

    print(f"  ✓ Saved {len(steps)} steps for {company_name}")
    return True


def run_transcript_recovery():
    """Pull transcripts from Bland for discovery calls and run Tree Mapper"""
    print("=== kue. Transcript Recovery ===\n")
    print("Fetching recent calls from Bland...")

    calls = get_bland_calls(limit=100)
    print(f"Found {len(calls)} total calls in Bland\n")

    # Filter to discovery mode calls
    discovery_calls = [
        c for c in calls
        if c.get("metadata", {}).get("mode") == "discovery"
        and c.get("status") == "completed"
    ]
    print(f"Found {len(discovery_calls)} completed Discovery calls\n")

    if not discovery_calls:
        print("No completed Discovery calls found.")
        return

    companies = get_companies()
    success = 0
    failed = 0
    skipped = 0

    for i, call in enumerate(discovery_calls):
        call_id = call.get("call_id") or call.get("id")
        company_id = call.get("metadata", {}).get("company_id")
        company = companies.get(company_id)
        company_name = company["name"] if company else company_id

        print(f"[{i+1}/{len(discovery_calls)}] {company_name}")

        if not company_id:
            print(f"  ⚠ No company_id in metadata, skipping")
            skipped += 1
            continue

        # Get full transcript
        transcript = get_call_transcript(call_id)
        if not transcript:
            print(f"  ⚠ No transcript available")
            skipped += 1
            continue

        if map_transcript_to_tree(company_id, company_name, transcript):
            success += 1
        else:
            failed += 1

        time.sleep(2)  # Small delay to avoid API rate limits

    print(f"\n=== Done ===")
    print(f"✓ Mapped: {success}")
    print(f"✗ Failed: {failed}")
    print(f"⚠ Skipped: {skipped}")


def run_batch(skip_confirm=False):
    """Place new Discovery calls for unmapped companies"""
    print("=== kue. Batch Discovery Mapper ===\n")

    # Start keep-alive ping
    ping_thread = threading.Thread(target=keep_render_awake, daemon=True)
    ping_thread.start()
    print("Background ping started to keep Render awake\n")

    print("Fetching unmapped companies...")

    # Get all companies
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/companies?select=id,name,phone&order=name.asc",
        headers=HEADERS_DB
    )
    all_companies = res.json()

    # Get company IDs that already have non-discovery steps
    trees_res = requests.get(
        f"{SUPABASE_URL}/rest/v1/ivr_trees?select=id,company_id",
        headers=HEADERS_DB
    )
    trees = trees_res.json()

    mapped_ids = set()
    for tree in trees:
        steps_res = requests.get(
            f"{SUPABASE_URL}/rest/v1/ivr_steps?tree_id=eq.{tree['id']}&confidence_score=gt.30&limit=1",
            headers=HEADERS_DB
        )
        if steps_res.json():
            mapped_ids.add(tree["company_id"])

    unmapped = [
        c for c in all_companies
        if c["id"] not in mapped_ids
        and c.get("phone")
        and len("".join(filter(str.isdigit, c["phone"]))) >= 10
    ]

    print(f"Found {len(unmapped)} companies needing Discovery calls\n")

    if not unmapped:
        print("All companies already mapped.")
        return

    estimated_cost = len(unmapped) * 0.35
    print(f"Estimated cost: ${estimated_cost:.2f}")
    print("Companies to map:")
    for c in unmapped:
        print(f"  - {c['name']}")

    if not skip_confirm:
        confirm = input(f"\nProceed with {len(unmapped)} Discovery calls? (yes/no): ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return

    success = 0
    failed = 0

    for i, company in enumerate(unmapped):
        print(f"\n[{i+1}/{len(unmapped)}] {company['name']}")

        phone = company["phone"]
        digits = "".join(filter(str.isdigit, phone))
        if len(digits) == 10:
            phone = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            phone = f"+{digits}"

        payload = {
            "phone_number": phone,
            "task": (
                f"You are calling {company['name']} to navigate their IVR phone system. "
                f"Your goal is to reach a live customer service representative. "
                f"Navigate through all menu options choosing the most general customer service option. "
                f"If asked which product: say 'mobile' for telecom, otherwise say the most general option. "
                f"If asked personal or business: say 'personal'. "
                f"If offered callback vs hold: press 3 to hold. "
                f"If asked for account number: say 'I do not have that available'. "
                f"Press 0 if stuck. "
                f"When a human answers: say 'One moment please, transferring now' then go silent."
            ),
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
                "company_id": company["id"],
                "goal": "reach customer service",
                "mode": "discovery"
            }
        }

        res = requests.post(
            "https://api.bland.ai/v1/calls",
            headers=HEADERS_BLAND,
            json=payload
        )
        result = res.json()

        if result.get("status") == "success":
            print(f"  ✓ Queued: {result.get('call_id')}")
            success += 1
        else:
            print(f"  ✗ Failed: {result}")
            failed += 1

        if i < len(unmapped) - 1:
            print(f"  Waiting 90 seconds...")
            time.sleep(90)

    print(f"\n=== Done ===")
    print(f"✓ Queued: {success}")
    print(f"✗ Failed: {failed}")


if __name__ == "__main__":
    print("kue. Batch Mapper\n")
    print("1. Recover transcripts from existing Bland calls (FREE - uses calls already made)")
    print("2. Place new Discovery calls for unmapped companies (~$15)")
    choice = input("\nChoose (1 or 2): ")

    if choice == "1":
        run_transcript_recovery()
    elif choice == "2":
        run_batch()
    else:
        print("Invalid choice.")