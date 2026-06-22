"""Debug: test login against running server and capture full error."""
import httpx
import json
import traceback

try:
    with httpx.Client() as client:
        # Test login
        resp = client.post(
            "http://127.0.0.1:8808/api/users/login",
            json={"username": "admin", "password": "cvg-neuron-admin"},
            timeout=10,
        )
        print(f"Status: {resp.status_code}")
        print(f"Body: {resp.text[:500]}")
        
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("data", {}).get("access_token", "")
            print(f"\nToken received: {token[:30]}...")
            
            # Test authenticated endpoint
            resp2 = client.get(
                "http://127.0.0.1:8808/api/users/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            print(f"\n/me Status: {resp2.status_code}")
            print(f"/me Body: {resp2.text[:500]}")
            
            # Test settings with auth
            resp3 = client.get(
                "http://127.0.0.1:8808/api/settings",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            print(f"\n/settings Status: {resp3.status_code}")
            print(f"/settings Body: {resp3.text[:500]}")
            
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    traceback.print_exc()
