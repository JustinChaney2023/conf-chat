# conf-chat Python

This is a **pure-CLI Python implementation** of the `conf-chat` assignment.  
Instead of using Java/OpenChord, this version implements a **simple peer-to-peer (P2P)** overlay using only the Python standard library (`socket`, `threading`, `json`, `hashlib`, `time`, `os`).  

Each process is a **P2P node** that:
- Listens on a TCP port.  
- Acts as the **home node** for users registered on it.  
- Provides a **CLI** for exactly one logged-in user at a time.  
- Communicates directly with other nodes using JSON messages over TCP.  
There is **no central server**: all nodes are symmetric and fully independent.

---

## 1. Relationship to the Assignment

The assignment asks for:
- User registration/authentication  
- Friend system with online/offline presence  
- Direct and offline messaging  
- Conference (group) chat  
- Pure P2P overlay (no central server)  

This Python build fulfills those requirements through a self-contained overlay where:
- Each `node.py` instance is both a **server and client**.  
- Each user has a **home node** (the one they registered on).  
- Nodes communicate directly using JSON-encoded TCP messages.  

### Simplifications
To keep it minimal:
- **Friend discovery** is by **username only** (no full-name search).  
- **Users must log in at their home node** (no roaming logins).  
- **Only the owner** can invite users into a conference at creation time.  

---

## 2. Architecture Overview

### Node and Home Model
Each node:
- Listens on `<host>:<port>`.  
- Runs two threads:  
  1. A TCP server for message handling.  
  2. A CLI loop for local commands.  

Each node stores:
- Registered users and hashed passwords.  
- Their friend and conference lists.  
- Pending friend requests.  
- Offline messages.  
- Hosted conferences.  

All persistent data is saved in:
```
state_<port>.json
```
Example: node on port `5000` → file `state_5000.json`.

---

### Message Flow

Nodes communicate with simple JSON messages over TCP, one request per connection.

#### Key Message Types

| Type | Purpose |
|------|----------|
| `FRIEND_REQUEST` | Sent to the target’s home node when a user requests friendship. |
| `FRIEND_ACCEPT` | Sent back to the requester’s home node to confirm mutual friendship. |
| `CHECK_ONLINE` | Queries a user’s home node to see if they’re online. |
| `SEND_DM` | Sent to recipient’s home node for message delivery or offline storage. |
| `CONF_INVITE` | Sent to each invited user’s home node when creating a conference. |
| `CONF_SEND` | Sent to the conference host node, which fans out the message to all participants. |

---

## 3. Implementation Details

### State Layout

#### Users
```python
users[username] = {
  "password_hash": "...",
  "full_name": "...",
  "home_host": "127.0.0.1",
  "home_port": 5000,
  "logged_in": False,
  "friends": { "bob": {"home_host": "127.0.0.1", "home_port": 5001} },
  "incoming_requests": {},
  "conferences": {}
}
```

#### Offline Messages
```python
offline_messages[username] = [
  {"from": "bob", "content": "hi", "timestamp": 1732000000.0, "conf_id": None}
]
```

#### Conferences
```python
conferences[conf_id] = {
  "name": "teamchat",
  "owner": "alice",
  "participants": {"alice": {"home_host": "127.0.0.1", "home_port": 5000}}
}
```

---

## 4. Installation

### Requirements
- Python 3.8+  
- No external libraries  

### Steps
```bash
git clone <your-repo-url> conf-chat
cd conf-chat
python node.py --port 5000
```
Each node will save state in `state_<port>.json`.

---

## 5. Running and Demo

### Starting Nodes
**Terminal 1 (Alice’s node):**
```bash
python node.py --port 5000
```
**Terminal 2 (Bob’s node):**
```bash
python node.py --port 5001
```

Each runs independently as a P2P node.

---

### Register and Login
**Alice (port 5000):**
```text
> register
New username: alice
Password: ****
Full name: Alice Smith
[register] User 'alice' created on this node (127.0.0.1:5000)

> login
Username: alice
Password: ****
[login] Logged in as alice
```

**Bob (port 5001):**
```text
> register
New username: bob
Password: ****
Full name: Bob Jones
[register] User 'bob' created on this node (127.0.0.1:5001)

> login
Username: bob
Password: ****
[login] Logged in as bob
```

---

### Friend System
**Alice adds Bob:**
```text
> friend-add bob
Friend bob home host (e.g. 127.0.0.1): 127.0.0.1
Friend bob home port: 5001
[friend-add] Friend request sent to bob @ 127.0.0.1:5001
```

**Bob accepts:**
```text
> friend-requests
Pending friend requests:
  alice (home 127.0.0.1:5000)
> friend-accept alice
[friend-accept] You and alice are now friends.
```

**Alice checks online status:**
```text
> online-friends
Friend online status:
  bob: online
```

---

### Direct Messages
**Online case:**
```text
# Alice:
> msg bob hello bob!
[msg] Sent.
# Bob sees:
[DM from alice] (HH:MM:SS) hello bob!
```

**Offline case:**
```text
# Bob:
> logout
[logout] bob logged out.
# Alice:
> msg bob are you there?
[msg] Sent.
# Bob later:
> login
Username: bob
Password: ****
[offline] You have 1 message(s):
  [HH:MM:SS] [DM from alice] are you there?
```

---

### Conference Chat
**Alice creates and invites Bob:**
```text
> conf-create teamchat bob
[conf-create] Created conference conf-XXXXXXXXXXXX (teamchat) with participants: alice, bob
```
**Alice’s conference list:**
```text
> conf-list
Your conferences:
  conf-XXXXXXXXXXXX: teamchat (owner alice) host 127.0.0.1:5000
```
**Bob receives invite:**
```text
[conf] User bob invited to conference conf-XXXXXXXXXXXX (teamchat) by alice
```
**Send message:**
```text
> conf-send conf-XXXXXXXXXXXX hello team!
[conf-send] Sent to conference.
```
**Bob sees:**
```text
[CONF conf-XXXXXXXXXXXX / from alice] (HH:MM:SS) hello team!
```

---

## 6. CLI Command Reference

### User Commands
| Command | Description |
|----------|-------------|
| `register` | Register a new user on this node. |
| `login` | Log in as a local user. |
| `logout` | Log out current user. |
| `passwd` | Change password. |
| `whoami` | Show current user and node info. |

### Friend Commands
| Command | Description |
|----------|-------------|
| `friends` | List friends. |
| `friend-requests` | List incoming requests. |
| `friend-add <user>` | Send friend request. |
| `friend-accept <user>` | Accept a request. |
| `online-friends` | Check friend online status. |

### Messaging
| Command | Description |
|----------|-------------|
| `msg <friend> <message>` | Send a direct message. |

### Conferences
| Command | Description |
|----------|-------------|
| `conf-create <name> <user1> [user2 ...]` | Create and invite to a new conference (owner-only). |
| `conf-list` | List conferences. |
| `conf-send <conf_id> <message>` | Send message to conference. |

### Misc
| Command | Description |
|----------|-------------|
| `help` | Show all commands. |
| `quit` | Logout and exit. |

---

## 7. Notes & Limitations

- **Local or LAN Use:**  
  Use `127.0.0.1` for local testing.  
  For LAN, start with `--host 0.0.0.0` and use LAN IPs when adding friends.  

- **Security:**  
  - No encryption (plain TCP).  
  - Passwords hashed with SHA-256 (no salt).  
  - No network authentication.  

- **Simplifications:**  
  - No global user search, username-based only.  
  - Must log in on the user’s home node.  
  - Only the creator invites participants in conferences.

Despite simplifications, the implementation satisfies all **core features** of the assignment:
- Pure P2P overlay (no central coordinator).  
- User registration, login, and authentication.  
- Friend relationships and online presence.  
- Direct + offline messaging.  
- Conference (group) chat with offline delivery.  
- Persistent local state via JSON.  

All functionality exists within a **single Python file** and is easily demonstrated via two or more terminals.
