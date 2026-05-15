import requests
import time
import os
from dotenv import load_dotenv
load_dotenv()

BLAND_API_KEY = os.getenv("BLAND_API_KEY")
HEADERS_BLAND = {
    "authorization": BLAND_API_KEY,
    "Content-Type": "application/json"
}

companies = [
    ("87eb2608-98cc-47f8-a79f-36b056fd20e5", "Aetna", "+18008722362"),
    ("a577e283-646d-4342-b869-d24faf3df473", "Bank of America", "+18004321000"),
    ("0d4f8950-cce3-4bbd-825a-66466405ee7e", "CIGNA", "+18009971654"),
    ("f416da5b-f09e-4267-9787-aca83127183e", "DISH", "+18886150295"),
    ("f5f920d1-f493-4c27-89eb-6c89c612fb05", "DoorDash", "+18559731040"),
    ("d88b327f-ce38-4b78-bbea-316128a69fb7", "FedEx", "+18004633339"),
    ("78217fb8-1a5b-4a75-abd4-f0b68c14f114", "UPS", "+18007425877"),
]

for i, (company_id, name, phone) in enumerate(companies):
    print(f"[{i+1}/7] Calling {name}...")
    
    res = requests.post(
        "https://api.bland.ai/v1/calls",
        headers=HEADERS_BLAND,
        json={
            "phone_number": phone,
            "task": (
                f"You are calling {name} to navigate their IVR phone system. "
                f"Your goal is to reach a live customer service representative. "
                f"Navigate through all menu options choosing the most general customer service option. "
                f"If asked which product: say the most general option. "
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
                "company_id": company_id,
                "goal": "reach customer service",
                "mode": "discovery"
            }
        }
    )
    
    result = res.json()
    if result.get("status") == "success":
        print(f"  ✓ Queued: {result.get('call_id')}")
    else:
        print(f"  ✗ Failed: {result}")
    
    if i < len(companies) - 1:
        print(f"  Waiting 90 seconds...")
        time.sleep(90)

print("\nDone. Run transcript recovery in batch_mapper.py in ~30 minutes.")