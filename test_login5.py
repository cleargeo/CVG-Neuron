"""Minimal test: add a debug endpoint to the running server."""
import sys
sys.path.insert(0, r'C:\Users\AlexZelenski\CVG-Neuron')

# Test the actual HTTP stack by making a raw request
import http.client
import json

conn = http.client.HTTPConnection("127.0.0.1", 8808, timeout=10)

# Test 1: Simple GET
print("Test 1: GET /api/status/ping")
conn.request("GET", "/api/status/ping")
resp = conn.getresponse()
print(f"  Status: {resp.status}")
print(f"  Body: {resp.read().decode()[:200]}")

# Test 2: POST login
print("\nTest 2: POST /api/users/login")
body = json.dumps({"username": "admin", "password": "cvg-neuron-admin"})
conn.request("POST", "/api/users/login", body=body, headers={"Content-Type": "application/json"})
resp = conn.getresponse()
print(f"  Status: {resp.status}")
print(f"  Body: {resp.read().decode()[:500]}")

# Test 3: POST with form data
print("\nTest 3: POST /api/users/login (form)")
body = "username=admin&password=cvg-neuron-admin"
conn.request("POST", "/api/users/login", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
resp = conn.getresponse()
print(f"  Status: {resp.status}")
print(f"  Body: {resp.read().decode()[:500]}")

conn.close()
