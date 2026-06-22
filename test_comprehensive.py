"""Final comprehensive test - all integrations."""
import sys, os
sys.path.insert(0, r'C:\Users\AlexZelenski\CVG-Neuron')

# Clear any cached modules
for mod in list(sys.modules.keys()):
    if mod.startswith('app.'):
        del sys.modules[mod]

from fastapi.testclient import TestClient
from app.main import app
import json

client = TestClient(app)
results = []

def test(name, method, path, data=None, headers=None, expected_status=None):
    if method == "GET":
        resp = client.get(path, headers=headers or {})
    elif method == "POST":
        resp = client.post(path, json=data, headers=headers or {})
    elif method == "PUT":
        resp = client.put(path, json=data, headers=headers or {})
    elif method == "DELETE":
        resp = client.delete(path, headers=headers or {})
    else:
        resp = client.request(method, path, json=data, headers=headers or {})
    
    status = "PASS" if (expected_status is None or resp.status_code == expected_status) else "FAIL"
    results.append((name, status, resp.status_code, resp.text[:200]))
    return resp

# === PUBLIC ENDPOINTS ===
test("root", "GET", "/", expected_status=200)
test("status", "GET", "/api/status", expected_status=200)
test("ping", "GET", "/api/status/ping", expected_status=200)
test("history", "GET", "/api/status/history", expected_status=200)
test("metrics", "GET", "/api/status/metrics", expected_status=200)
test("dashboard", "GET", "/api/dashboard", expected_status=200)
test("agents", "GET", "/api/dashboard/agents", expected_status=200)
test("cache", "GET", "/api/dashboard/cache", expected_status=200)
test("hive", "GET", "/api/dashboard/hive", expected_status=200)
test("process_list", "GET", "/api/process", expected_status=200)
test("predict_models", "GET", "/api/predict/models", expected_status=200)
test("train_domains", "GET", "/api/train/domains", expected_status=200)
test("info", "GET", "/api/info", expected_status=200)
test("permissions", "GET", "/api/permissions", expected_status=200)
test("roles", "GET", "/api/permissions/roles", expected_status=200)

# === AUTH ENDPOINTS ===
test("settings_noauth", "GET", "/api/settings", expected_status=401)
test("perm_check_noauth", "POST", "/api/permissions/check", data={"role":"admin","resource":"x","action":"read"}, expected_status=401)
test("users_noauth", "GET", "/api/users", expected_status=401)
test("create_user_noauth", "POST", "/api/users", data={"username":"x","password":"y"}, expected_status=401)

# === LOGIN ===
login_resp = test("login", "POST", "/api/users/login", data={"username":"admin","password":"cvg-neuron-admin"}, expected_status=200)

token = None
if login_resp.status_code == 200:
    token = login_resp.json().get("data", {}).get("access_token", "")
    print(f"\n[TOKEN] {token[:40]}...")

# Wrong password
test("login_wrong", "POST", "/api/users/login", data={"username":"admin","password":"wrong"}, expected_status=401)

# === AUTHENTICATED ENDPOINTS ===
if token:
    auth = {"Authorization": f"Bearer {token}"}
    test("me", "GET", "/api/users/me", headers=auth, expected_status=200)
    test("settings_auth", "GET", "/api/settings", headers=auth, expected_status=200)
    test("users_auth", "GET", "/api/users", headers=auth, expected_status=200)
    test("perm_check_auth", "POST", "/api/permissions/check", data={"role":"admin","resource":"dashboard","action":"read"}, headers=auth, expected_status=200)
    
    # Create user
    test("create_user", "POST", "/api/users", data={"username":"testuser","password":"testpass123","email":"test@test.com"}, headers=auth, expected_status=201)
    
    # Process task
    proc_resp = test("process", "POST", "/api/process", data={"input":"hello","agent_id":"agent-8ab60a0a"}, headers=auth, expected_status=200)
    
    # Predict
    test("predict", "POST", "/api/predict", data={"input":"what is 2+2","model":"llama3.1:8b"}, headers=auth, expected_status=200)

# === ERROR HANDLING ===
test("404", "GET", "/api/nonexistent", expected_status=404)

# === PRINT RESULTS ===
print("\n" + "=" * 80)
print(f"{'TEST RESULTS':^80}")
print("=" * 80)
passed = failed = 0
for name, status, code, body in results:
    icon = "PASS" if status == "PASS" else "FAIL"
    if status == "PASS":
        passed += 1
    else:
        failed += 1
    print(f"  [{icon}] {name:<35} HTTP {code}")
    if status == "FAIL":
        print(f"        Body: {body[:100]}")

print(f"\nTotal: {passed} passed, {failed} failed out of {passed + failed}")
