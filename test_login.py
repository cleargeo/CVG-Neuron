import asyncio
import sys
sys.path.insert(0, r'C:\Users\AlexZelenski\CVG-Neuron')

from app.core.security import hash_password, verify_password
from app.routers.users import _seed_default_admin, _users

print("Testing password hash/verify...")
try:
    h = hash_password("cvg-neuron-admin")
    print(f"  hash: {h[:30]}...")
    v = verify_password("cvg-neuron-admin", h)
    print(f"  verify: {v}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print("\nTesting _seed_default_admin...")
try:
    _seed_default_admin()
    print(f"  users: {list(_users.keys())}")
    for uid, u in _users.items():
        print(f"  {uid}: {u.username}, roles={u.roles}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

print("\nTesting login flow...")
try:
    from app.routers.users import UserLogin
    creds = UserLogin(username="admin", password="cvg-neuron-admin")
    user = next((u for u in _users.values() if u.username == creds.username), None)
    print(f"  user found: {user is not None}")
    if user:
        v = verify_password(creds.password, user.hashed_password)
        print(f"  password verify: {v}")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
