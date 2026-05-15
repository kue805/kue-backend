import requests
import os
from dotenv import load_dotenv

load_dotenv()

BLAND_API_KEY = os.getenv("BLAND_API_KEY")

HEADERS_BLAND = {
    "authorization": BLAND_API_KEY,
    "Content-Type": "application/json"
}

# Get recent calls
res = requests.get(
    "https://api.bland.ai/v1/calls?limit=5",
    headers=HEADERS_BLAND
)

calls = res.json().get("calls", [])
print(f"Found {len(calls)} calls\n")

if calls:
    # Look at the first discovery call
    for call in calls:
        if call.get("metadata", {}).get("mode") == "discovery":
            call_id = call.get("call_id") or call.get("id")
            print(f"Discovery call ID: {call_id}")
            
            # Fetch full call details
            detail_res = requests.get(
                f"https://api.bland.ai/v1/calls/{call_id}",
                headers=HEADERS_BLAND
            )
            detail = detail_res.json()
            
            # Print all top-level keys
            print(f"Response keys: {list(detail.keys())}")
            
            # Print transcript field specifically
            print(f"Transcript type: {type(detail.get('transcript'))}")
            print(f"Transcript value: {str(detail.get('transcript'))[:500]}")
            
            # Print concatenated_transcript if exists
            print(f"Concatenated: {str(detail.get('concatenated_transcript', ''))[:500]}")
            break