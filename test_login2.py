"""Quick debug test for login issue."""
import sys
sys.path.insert(0, r'C:\Users\AlexZelenski\CVG-Neuron')

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

# Test login
print("Testing /api/users/login...")
resp = client.post("/api/users/login", json={"username": "admin", "password": "cvg-neuron-admin"})
print(f"Status: {resp.status_code}")
print(f"Body: {resp.text}")

# Test with wrong password
print("\nTesting with wrong password...")
resp = client.post("/api/users/login", json={"username": "admin", "password": "wrong"})
print(f"Status: {resp.status_code}")
print(f"Body: {resp.text}")
