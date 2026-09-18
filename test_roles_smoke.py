"""Throwaway smoke test: admin/manager/user matrix + role migrations."""
import os
import tempfile

from fastapi.testclient import TestClient

from app.controller import app
from app.db import Database
from app.repositories.users import UsersRepo
from app.repositories.web_requests import WebRequestsRepo
from app.repositories.settings import SettingsRepo
from app.repositories.prompts import PromptsRepo

tmp = tempfile.mkdtemp()

# an old-style DB: role='admin' always meant full rights — stays admin
db0 = Database(os.path.join(tmp, "old.db"))
db0.conn.execute("INSERT INTO auth_user(username, password, role, created)"
                 " VALUES ('boss', 'x', 'admin', 1)")
db0.conn.commit()
u0 = UsersRepo(db0.conn)
assert u0.get("boss")["role"] == "admin"
# a manager created now stays manager across re-init
u0.create("helper", "x", "manager")
assert UsersRepo(db0.conn).get("helper")["role"] == "manager"
db0.close()

# a draft-era DB (meta roles_v2 set): 'superadmin' was full rights and
# 'admin' was the prompt editor — collapse to admin/manager exactly once
db1 = Database(os.path.join(tmp, "draft.db"))
db1.conn.execute("INSERT INTO auth_user(username, password, role, created)"
                 " VALUES ('boss', 'x', 'superadmin', 1)")
db1.conn.execute("INSERT INTO auth_user(username, password, role, created)"
                 " VALUES ('editor', 'x', 'admin', 1)")
db1.conn.execute("INSERT INTO meta(key, value) VALUES('roles_v2', '1')")
db1.conn.commit()
u1 = UsersRepo(db1.conn)
assert u1.get("boss")["role"] == "admin"
assert u1.get("editor")["role"] == "manager"
# an admin created after the collapse keeps full rights across re-init
u1.create("boss2", "x", "admin")
assert UsersRepo(db1.conn).get("boss2")["role"] == "admin"
db1.close()

db = Database(os.path.join(tmp, "t.db"))
users = UsersRepo(db.conn)
assert users.get("admin")["role"] == "admin"        # fresh seed
app.state.users = users
app.state.web_requests = WebRequestsRepo(db.conn)
app.state.settings = SettingsRepo(db.conn)
app.state.prompts = PromptsRepo(db.conn)

root = TestClient(app)
assert root.post("/api/auth/login",
                 json={"username": "admin", "password": "admin"}).status_code == 200
assert root.post("/api/users", json={"username": "m1", "password": "p",
                                     "role": "manager"}).status_code == 200
# the retired role name is rejected
assert root.post("/api/users", json={"username": "s1", "password": "p",
                                     "role": "superadmin"}).status_code == 422
root.post("/api/users", json={"username": "u1", "password": "p", "role": "user"})

mgr = TestClient(app)
mgr.post("/api/auth/login", json={"username": "m1", "password": "p"})
usr = TestClient(app)
usr.post("/api/auth/login", json={"username": "u1", "password": "p"})

# admin: everything
assert root.get("/api/users").status_code == 200
assert root.get("/api/settings").status_code == 200
assert root.post("/api/prompts", json={"kind": "problem", "name": "n",
                                       "text": "t"}).status_code == 200

# manager: prompts yes (both kinds), admin areas no
assert mgr.get("/api/prompts").status_code == 200
r = mgr.post("/api/prompts", json={"kind": "problem", "name": "n2", "text": "t"})
assert r.status_code == 200
pid = r.json()["id"]
assert mgr.put(f"/api/prompts/{pid}", json={"text": "t2"}).status_code == 200
r = mgr.post("/api/prompts", json={"kind": "role", "name": "r1", "text": "t"})
assert r.status_code == 200
assert mgr.delete(f"/api/prompts/{r.json()['id']}").status_code == 200
assert mgr.delete(f"/api/prompts/{pid}").status_code == 200
assert mgr.get("/api/users").status_code == 403
assert mgr.get("/api/settings").status_code == 403
assert mgr.get("/api/projects").status_code == 403

# user: no prompt writes, reads ok
assert usr.get("/api/prompts").status_code == 200
assert usr.post("/api/prompts", json={"kind": "problem", "name": "x",
                                      "text": "y"}).status_code == 403
assert usr.get("/api/users").status_code == 403

# last admin is protected (demote and delete)
users_list = root.get("/api/users").json()["users"]
root_id = next(u["id"] for u in users_list if u["username"] == "admin")
assert root.put(f"/api/users/{root_id}", json={"role": "manager"}).status_code == 400
assert root.delete(f"/api/users/{root_id}").status_code == 400
# but a second admin unlocks the demotion
root.post("/api/users", json={"username": "a2", "password": "p",
                              "role": "admin"})
assert root.put(f"/api/users/{root_id}", json={"role": "manager"}).status_code == 200
# the demoted account immediately loses user management…
assert root.get("/api/users").status_code == 403
# …but keeps prompt editing (manager)
assert root.post("/api/prompts", json={"kind": "problem", "name": "n3",
                                       "text": "t"}).status_code == 200
# …and the remaining admin can restore it
a2 = TestClient(app)
a2.post("/api/auth/login", json={"username": "a2", "password": "p"})
assert a2.put(f"/api/users/{root_id}", json={"role": "admin"}).status_code == 200

print("ALL ROLE TESTS PASSED")
