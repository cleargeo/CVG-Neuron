"""Debug: trace the exact error in login endpoint."""
import sys
sys.path.insert(0, r'C:\Users\AlexZelenski\CVG-Neuron')

import traceback

# Simulate exactly what the login endpoint does
from app.routers.users import _seed_default_admin, _users, UserLogin, TokenResponse
from app.core.security import verify_password, create_access_token, hash_password
from app.core.config import settings
from app.models.response import NeuronResponse

print("Step 1: Seed admin")
_seed_default_admin()
print(f"  Users: {list(_users.keys())}")

print("\nStep 2: Create credentials")
creds = UserLogin(username="admin", password="cvg-neuron-admin")
print(f"  username: {creds.username}")

print("\nStep 3: Find user")
user = next((u for u in _users.values() if u.username == creds.username), None)
print(f"  user: {user}")

print("\nStep 4: Verify password")
v = verify_password(creds.password, user.hashed_password)
print(f"  verified: {v}")

print("\nStep 5: Create token")
token_data = {
    "sub": user.user_id,
    "username": user.username,
    "roles": user.roles,
}
print(f"  token_data: {token_data}")
token = create_access_token(subject=token_data)
print(f"  token: {token[:30]}...")

print("\nStep 6: Create TokenResponse")
expires = settings.access_token_expire_minutes * 60
print(f"  expires_in: {expires} (type: {type(expires).__name__})")
tr = TokenResponse(
    access_token=token,
    expires_in=expires,
    user_id=user.user_id,
    username=user.username,
    roles=user.roles,
)
print(f"  TokenResponse: {tr}")

print("\nStep 7: Create NeuronResponse")
resp = NeuronResponse.ok(data=tr.model_dump(), message="Authentication successful")
print(f"  NeuronResponse: {resp}")

print("\nStep 8: Serialize")
d = resp.model_dump(mode="json")
print(f"  Serialized: {d}")

print("\nAll steps completed successfully!")
