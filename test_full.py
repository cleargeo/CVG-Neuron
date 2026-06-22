"""Full integration test against the running server."""
import sys
sys.path.insert(0, r'C:\Users\AlexZelenski\CVG-Neuron')

from fastapi.testclient import TestClient
from app.main import app
import json

client = TestClient(app)

# Test 1: Login
print("=" * 60)
print("Test 1: POST /api/users/login")
print("=" * 60)
resp = client.post("/api/users/login", json={"username": "admin", "password": "cvg-neuron-admin"})
print(f"Status: {resp.status_code}")
print(f"Body: {resp.text[:500]}")

if resp.status_code == 200:
    data = resp.json()
    token = data.get("data", {}).get("access_token", "")
    print(f"\nToken: {token[:40]}...")
    
    # Test 2: /api/users/me
    print("\n" + "=" * 60)
    print("Test 2: GET /api/users/me")
    print("=" * 60)
    resp2 = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    print(f"Status: {resp2.status_code}")
    print(f"Body: {resp2.text[:500]}")
    
    # Test 3: /api/settings
    print("\n" + "=" * 60)
    print("Test 3: GET /api/settings")
    print("=" * 60)
    resp3 = client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
    print(f"Status: {resp3.status_code}")
    print(f"Body: {resp3.text[:500]}")
    
    # Test 4: /api/users
    print("\n" + "=" * 60)
    print("Test 4: GET /api/users")
    print("=" * 60)
    resp4 = client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
    print(f"Status: {resp4.status_code}")
    print(f"Body: {resp4.text[:500]}")

# Test 5: Wrong password
print("\n" + "=" * 60)
print("Test 5: POST /api/users/login (wrong password)")
print("=" * 60)
resp5 = client.post("/api/users/login", json={"username": "admin", "password": "wrong"})
print(f"Status: {resp5.status_code}")
print(f"Body: {resp5.text[:500]}")
