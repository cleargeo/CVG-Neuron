"""CVG Neuron Webhook Client - sends deployment events to Neuron."""
import json, urllib.request, urllib.error, os
NEURON_URL = os.getenv("NEURON_URL", "http://10.10.10.200:8808")
def notify_neuron(app_name, status, environment="production", message="", metadata=None):
    payload = {"app_name": app_name, "status": status, "environment": environment, "message": message, "metadata": metadata or {}}
    req = urllib.request.Request(f"{NEURON_URL}/api/webhook/deploy", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp: return json.loads(resp.read())
    except Exception as e: return {"status": "error", "detail": str(e)}
