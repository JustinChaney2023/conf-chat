#!/usr/bin/env python3
"""
conf-chat minimal P2P-style implementation in Python (single file, CLI only).

Design assumptions (keep README in sync with this):

- Each running node is:
    - A TCP server (listens on host:port)
    - A CLI client for exactly one logged-in user at a time.

- Each user has a *home node*:
    - The node where they REGISTERed.
    - They must LOG IN on that same node (no roaming logins).
    - The home node stores:
        - The user's password hash and full name
        - Their friend list
        - Incoming friend requests
        - Their conference membership index
        - Their offline messages

- Nodes talk directly to each other's home nodes over TCP,
  exchanging single-line JSON messages.

- Presence:
    - The home node tracks whether its local user is currently logged in.
    - Other nodes query presence via CHECK_ONLINE messages.

- Messages:
    - SEND_DM goes to the recipient's home node.
    - The home node delivers instantly if the user is logged in,
      otherwise stores it in offline_messages[username] for later.

- Conferences:
    - A conference is “hosted” on its creator's home node.
    - The host node keeps the participant list.
    - When anyone sends to a conference, the host node fan-outs
      to each participant's home node as a SEND_DM with a conf_id.
"""

import argparse
import json
import socket
import threading
import hashlib
import time
import os
from typing import Dict, Any, Tuple, Optional, List

# --------------------------- Global State ---------------------------

host: str = "127.0.0.1"
port: int = 0
state_file: str = ""

# All users for whom THIS node is the home
# users[username] = {
#   "password_hash": str,
#   "full_name": str,
#   "home_host": str,
#   "home_port": int,
#   "logged_in": bool,
#   "friends": {
#       friend_username: {
#           "home_host": str,
#           "home_port": int
#       }, ...
#   },
#   "incoming_requests": {
#       other_username: {
#           "home_host": str,
#           "home_port": int
#       }, ...
#   },
#   "conferences": {
#       conf_id: {
#           "name": str,
#           "owner": str,
#           "host_host": str,
#           "host_port": int
#       }, ...
#   }
# }
users: Dict[str, Dict[str, Any]] = {}

# Offline messages stored at THIS node for our home users
# offline_messages[username] = [
#   {
#       "from": str,
#       "content": str,
#       "timestamp": float,
#       "conf_id": Optional[str]
#   }, ...
# ]
offline_messages: Dict[str, List[Dict[str, Any]]] = {}

# Conferences hosted on THIS node
# conferences[conf_id] = {
#   "name": str,
#   "owner": str,
#   "participants": {
#       username: {
#           "home_host": str,
#           "home_port": int
#       }, ...
#   }
# }
conferences: Dict[str, Dict[str, Any]] = {}

# Currently logged-in local user (for the CLI)
current_user: Optional[str] = None

# Lock to protect shared state
state_lock = threading.Lock()

# --------------------------- Utilities ---------------------------

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def check_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def load_state() -> None:
    global users, offline_messages, conferences
    if not os.path.exists(state_file):
        return
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        users = data.get("users", {})
        offline_messages = data.get("offline_messages", {})
        conferences = data.get("conferences", {})
        # Reset logged_in flags on restart
        for u in users.values():
            u["logged_in"] = False
        # Ensure offline_messages entry exists for every user
        for uname in users.keys():
            offline_messages.setdefault(uname, [])
    except Exception as e:
        print(f"[state] Failed to load state: {e}")


def save_state() -> None:
    data = {
        "users": users,
        "offline_messages": offline_messages,
        "conferences": conferences,
    }
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[state] Failed to save state: {e}")


def generate_conf_id() -> str:
    # Simple timestamp-based ID (good enough for assignment)
    return f"conf-{int(time.time() * 1000)}"


# --------------------------- Networking Helpers ---------------------------

def send_message(addr: Tuple[str, int], msg: Dict[str, Any], expect_reply: bool = True) -> Optional[Dict[str, Any]]:
    """
    Connects to addr, sends msg as JSON line, optionally waits for reply.
    Returns reply dict or None on error / if expect_reply=False.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(addr)
        data = (json.dumps(msg) + "\n").encode("utf-8")
        s.sendall(data)
        if not expect_reply:
            s.close()
            return None
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        if not buf:
            return None
        return json.loads(buf.decode("utf-8").strip())
    except Exception as e:
        print(f"[net] Failed to send {msg.get('type')} to {addr}: {e}")
        return None


def server_loop() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen()
    print(f"[node] Listening on {host}:{port}")
    while True:
        try:
            conn, addr = srv.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except Exception as e:
            print(f"[server] accept error: {e}")


def handle_client(conn: socket.socket, addr: Tuple[str, int]) -> None:
    try:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            conn.close()
            return
        msg = json.loads(buf.decode("utf-8").strip())
        reply = handle_message(msg, addr)
        if reply is not None:
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    except Exception as e:
        print(f"[server] handle_client error: {e}")
    finally:
        conn.close()


# --------------------------- Message Handlers ---------------------------

def handle_message(msg: Dict[str, Any], addr: Tuple[str, int]) -> Optional[Dict[str, Any]]:
    t = msg.get("type")
    if t == "FRIEND_REQUEST":
        return handle_friend_request_msg(msg)
    elif t == "FRIEND_ACCEPT":
        return handle_friend_accept_msg(msg)
    elif t == "CHECK_ONLINE":
        return handle_check_online_msg(msg)
    elif t == "SEND_DM":
        return handle_send_dm_msg(msg)
    elif t == "CONF_INVITE":
        return handle_conf_invite_msg(msg)
    elif t == "CONF_SEND":
        return handle_conf_send_msg(msg)
    else:
        print(f"[server] Unknown message type from {addr}: {t}")
        return {"status": "error", "error": "unknown_type"}


def handle_friend_request_msg(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Incoming FRIEND_REQUEST at the *to_user* home node.
    """
    from_user = msg["from_user"]
    from_home_host = msg["from_home_host"]
    from_home_port = msg["from_home_port"]
    to_user = msg["to_user"]

    with state_lock:
        if to_user not in users:
            return {"status": "error", "error": "no_such_user"}
        u = users[to_user]
        inc = u.setdefault("incoming_requests", {})
        inc[from_user] = {
            "home_host": from_home_host,
            "home_port": from_home_port,
        }
        save_state()
    print(f"[friend] {from_user} requested friendship with {to_user}")
    return {"status": "ok"}


def handle_friend_accept_msg(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Incoming FRIEND_ACCEPT at the *to_user* home node.
    """
    from_user = msg["from_user"]   # acceptor
    from_home_host = msg["from_home_host"]
    from_home_port = msg["from_home_port"]
    to_user = msg["to_user"]       # original requester

    with state_lock:
        if to_user not in users:
            return {"status": "error", "error": "no_such_user"}
        u = users[to_user]
        friends = u.setdefault("friends", {})
        friends[from_user] = {
            "home_host": from_home_host,
            "home_port": from_home_port,
        }
        save_state()
    print(f"[friend] {to_user} and {from_user} are now friends (this side)")
    return {"status": "ok"}


def handle_check_online_msg(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Incoming CHECK_ONLINE at the user's home node.
    """
    username = msg["username"]
    with state_lock:
        u = users.get(username)
        if not u:
            return {"status": "error", "error": "no_such_user"}
        online = bool(u.get("logged_in", False))
    return {"status": "ok", "online": online}


def handle_send_dm_msg(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Incoming SEND_DM at the recipient's home node.
    """
    from_user = msg["from_user"]
    to_user = msg["to_user"]
    content = msg["content"]
    timestamp = msg.get("timestamp", time.time())
    conf_id = msg.get("conf_id")

    with state_lock:
        if to_user not in users:
            return {"status": "error", "error": "no_such_user"}
        m = {
            "from": from_user,
            "content": content,
            "timestamp": timestamp,
            "conf_id": conf_id,
        }
        # Immediate delivery if logged in at this node
        u = users[to_user]
        if u.get("logged_in", False):
            # print directly to console
            prefix = f"[DM from {from_user}]"
            if conf_id:
                prefix = f"[CONF {conf_id} / from {from_user}]"
            ts_str = time.strftime("%H:%M:%S", time.localtime(timestamp))
            print(f"\n{prefix} ({ts_str}) {content}")
            # Reprint prompt if needed
        else:
            offline_messages.setdefault(to_user, []).append(m)
            save_state()
    return {"status": "ok"}


def handle_conf_invite_msg(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Incoming CONF_INVITE at a participant's home node.
    """
    conf_id = msg["conf_id"]
    name = msg["name"]
    owner = msg["owner"]
    host_host = msg["host_host"]
    host_port = msg["host_port"]
    to_user = msg["to_user"]

    with state_lock:
        if to_user not in users:
            return {"status": "error", "error": "no_such_user"}
        u = users[to_user]
        confs = u.setdefault("conferences", {})
        confs[conf_id] = {
            "name": name,
            "owner": owner,
            "host_host": host_host,
            "host_port": host_port,
        }
        save_state()
    print(f"[conf] User {to_user} invited to conference {conf_id} ({name}) by {owner}")
    return {"status": "ok"}


def handle_conf_send_msg(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Incoming CONF_SEND at the conference host node.
    Fan-out to all participants' home nodes as SEND_DM.
    """
    conf_id = msg["conf_id"]
    from_user = msg["from_user"]
    content = msg["content"]
    timestamp = msg.get("timestamp", time.time())

    with state_lock:
        conf = conferences.get(conf_id)
        if not conf:
            return {"status": "error", "error": "no_such_conference"}
        participants = conf.get("participants", {})

    for uname, info in participants.items():
        home_addr = (info["home_host"], info["home_port"])
        send_message(
            home_addr,
            {
                "type": "SEND_DM",
                "from_user": from_user,
                "to_user": uname,
                "content": content,
                "timestamp": timestamp,
                "conf_id": conf_id,
            },
            expect_reply=True,
        )
    return {"status": "ok"}


# --------------------------- CLI Commands ---------------------------

def require_login() -> bool:
    if current_user is None:
        print("You must be logged in. Use: login")
        return False
    return True


def cmd_register() -> None:
    username = input("New username: ").strip()
    password = input("Password: ").strip()
    full_name = input("Full name: ").strip()

    with state_lock:
        if username in users:
            print("That username already exists on this node.")
            return
        users[username] = {
            "password_hash": hash_password(password),
            "full_name": full_name,
            "home_host": host,
            "home_port": port,
            "logged_in": False,
            "friends": {},
            "incoming_requests": {},
            "conferences": {},
        }
        offline_messages.setdefault(username, [])
        save_state()
    print(f"[register] User '{username}' created on this node ({host}:{port})")


def cmd_login() -> None:
    global current_user
    username = input("Username: ").strip()
    password = input("Password: ").strip()

    with state_lock:
        u = users.get(username)
        if not u:
            print("No such user on this node. (You must login at your home node.)")
            return
        if not check_password(password, u["password_hash"]):
            print("Incorrect password.")
            return
        u["logged_in"] = True
        current_user = username
        save_state()

        # fetch offline messages
        msgs = offline_messages.get(username, [])
        if msgs:
            print(f"[offline] You have {len(msgs)} message(s):")
            for m in msgs:
                ts_str = time.strftime("%H:%M:%S", time.localtime(m["timestamp"]))
                if m["conf_id"]:
                    print(f"  [{ts_str}] [CONF {m['conf_id']} / from {m['from']}] {m['content']}")
                else:
                    print(f"  [{ts_str}] [DM from {m['from']}] {m['content']}")
            offline_messages[username] = []
            save_state()
    print(f"[login] Logged in as {username}")


def cmd_logout() -> None:
    global current_user
    if current_user is None:
        print("Not logged in.")
        return
    with state_lock:
        u = users.get(current_user)
        if u:
            u["logged_in"] = False
            save_state()
    print(f"[logout] {current_user} logged out.")
    current_user = None


def cmd_passwd() -> None:
    if not require_login():
        return
    old = input("Old password: ").strip()
    new = input("New password: ").strip()
    with state_lock:
        u = users[current_user]
        if not check_password(old, u["password_hash"]):
            print("Old password incorrect.")
            return
        u["password_hash"] = hash_password(new)
        save_state()
    print("[passwd] Password changed.")


def cmd_whoami() -> None:
    if current_user is None:
        print("Not logged in.")
    else:
        with state_lock:
            u = users.get(current_user)
            if not u:
                print("Current user record missing!?")
                return
            print(f"{current_user} ({u['full_name']}) @ {host}:{port}")


def cmd_friends() -> None:
    if not require_login():
        return
    with state_lock:
        u = users[current_user]
        friends = u.get("friends", {})
        if not friends:
            print("No friends yet.")
            return
        print("Friends:")
        for name, info in friends.items():
            print(f"  {name} (home {info['home_host']}:{info['home_port']})")


def cmd_friend_requests() -> None:
    if not require_login():
        return
    with state_lock:
        u = users[current_user]
        reqs = u.get("incoming_requests", {})
        if not reqs:
            print("No pending friend requests.")
            return
        print("Pending friend requests:")
        for name, info in reqs.items():
            print(f"  {name} (home {info['home_host']}:{info['home_port']})")


def cmd_friend_add(args: List[str]) -> None:
    if not require_login():
        return
    if len(args) < 1:
        print("Usage: friend-add <username>")
        return
    friend_name = args[0]
    friend_host = input(f"Friend {friend_name} home host (e.g. 127.0.0.1): ").strip() or "127.0.0.1"
    try:
        friend_port = int(input(f"Friend {friend_name} home port: ").strip())
    except ValueError:
        print("Invalid port.")
        return

    with state_lock:
        my_user = users[current_user]
        # Optimistically add friend record (it becomes "active" once accepted)
        friends = my_user.setdefault("friends", {})
        friends.setdefault(friend_name, {
            "home_host": friend_host,
            "home_port": friend_port,
        })
        save_state()

    reply = send_message(
        (friend_host, friend_port),
        {
            "type": "FRIEND_REQUEST",
            "from_user": current_user,
            "from_home_host": host,
            "from_home_port": port,
            "to_user": friend_name,
        },
        expect_reply=True,
    )

    if not reply or reply.get("status") != "ok":
        print(f"[friend-add] Failed to send friend request: {reply}")
    else:
        print(f"[friend-add] Friend request sent to {friend_name} @ {friend_host}:{friend_port}")


def cmd_friend_accept(args: List[str]) -> None:
    if not require_login():
        return
    if len(args) < 1:
        print("Usage: friend-accept <username>")
        return
    other = args[0]

    with state_lock:
        u = users[current_user]
        reqs = u.get("incoming_requests", {})
        info = reqs.get(other)
        if not info:
            print(f"No pending request from {other}.")
            return
        # Add other to my friends
        friends = u.setdefault("friends", {})
        friends[other] = {
            "home_host": info["home_host"],
            "home_port": info["home_port"],
        }
        # Remove from incoming_requests
        del reqs[other]
        save_state()
        other_home = (info["home_host"], info["home_port"])

    # Notify other user's home node
    reply = send_message(
        other_home,
        {
            "type": "FRIEND_ACCEPT",
            "from_user": current_user,
            "from_home_host": host,
            "from_home_port": port,
            "to_user": other,
        },
        expect_reply=True,
    )

    if not reply or reply.get("status") != "ok":
        print(f"[friend-accept] Warning: remote side may not have recorded friendship correctly: {reply}")
    else:
        print(f"[friend-accept] You and {other} are now friends.")


def cmd_online_friends() -> None:
    if not require_login():
        return
    with state_lock:
        u = users[current_user]
        friends = u.get("friends", {}).copy()
    if not friends:
        print("No friends.")
        return

    print("Friend online status:")
    for name, info in friends.items():
        reply = send_message(
            (info["home_host"], info["home_port"]),
            {
                "type": "CHECK_ONLINE",
                "username": name,
            },
            expect_reply=True,
        )
        if not reply or reply.get("status") != "ok":
            status = "unknown"
        else:
            status = "online" if reply.get("online") else "offline"
        print(f"  {name}: {status}")


def cmd_msg(args: List[str]) -> None:
    if not require_login():
        return
    if len(args) < 2:
        print("Usage: msg <friend> <message...>")
        return
    to_user = args[0]
    content = " ".join(args[1:])
    with state_lock:
        u = users[current_user]
        friends = u.get("friends", {})
        finfo = friends.get(to_user)
        if not finfo:
            print(f"{to_user} is not in your friend list.")
            return
        home_host = finfo["home_host"]
        home_port = finfo["home_port"]

    reply = send_message(
        (home_host, home_port),
        {
            "type": "SEND_DM",
            "from_user": current_user,
            "to_user": to_user,
            "content": content,
            "timestamp": time.time(),
            "conf_id": None,
        },
        expect_reply=True,
    )

    if not reply or reply.get("status") != "ok":
        print(f"[msg] Failed to send message: {reply}")
    else:
        print("[msg] Sent.")


def cmd_conf_create(args: List[str]) -> None:
    if not require_login():
        return
    if len(args) < 2:
        print("Usage: conf-create <name> <user1> [user2 ...]")
        return
    name = args[0]
    participants = args[1:]

    with state_lock:
        me = users[current_user]
        my_friends = me.get("friends", {})
        # Build participants dict with home info
        part_info: Dict[str, Dict[str, Any]] = {}
        # Always include current_user
        part_info[current_user] = {
            "home_host": host,
            "home_port": port,
        }
        for uname in participants:
            if uname not in my_friends:
                print(f"[conf-create] Warning: {uname} is not your friend; skipping.")
                continue
            finfo = my_friends[uname]
            part_info[uname] = {
                "home_host": finfo["home_host"],
                "home_port": finfo["home_port"],
            }
        if len(part_info) < 2:
            print("[conf-create] Need at least you + 1 more participant.")
            return

        conf_id = generate_conf_id()
        conferences[conf_id] = {
            "name": name,
            "owner": current_user,
            "participants": part_info,
        }

        # Add to local user's conference index
        confs = me.setdefault("conferences", {})
        confs[conf_id] = {
            "name": name,
            "owner": current_user,
            "host_host": host,
            "host_port": port,
        }
        save_state()

    # Invite all other participants via their home nodes
    for uname, info in part_info.items():
        if uname == current_user:
            continue
        home_addr = (info["home_host"], info["home_port"])
        send_message(
            home_addr,
            {
                "type": "CONF_INVITE",
                "conf_id": conf_id,
                "name": name,
                "owner": current_user,
                "host_host": host,
                "host_port": port,
                "to_user": uname,
            },
            expect_reply=True,
        )

    print(f"[conf-create] Created conference {conf_id} ({name}) with participants: {', '.join(part_info.keys())}")


def cmd_conf_list() -> None:
    if not require_login():
        return
    with state_lock:
        u = users[current_user]
        confs = u.get("conferences", {})
        if not confs:
            print("You are not in any conferences.")
            return
        print("Your conferences:")
        for cid, info in confs.items():
            print(f"  {cid}: {info['name']} (owner {info['owner']}) host {info['host_host']}:{info['host_port']}")


def cmd_conf_send(args: List[str]) -> None:
    if not require_login():
        return
    if len(args) < 2:
        print("Usage: conf-send <conf_id> <message...>")
        return
    conf_id = args[0]
    content = " ".join(args[1:])

    with state_lock:
        u = users[current_user]
        confs = u.get("conferences", {})
        info = confs.get(conf_id)
        if not info:
            print(f"You are not in conference {conf_id}.")
            return
        host_host = info["host_host"]
        host_port = info["host_port"]

    reply = send_message(
        (host_host, host_port),
        {
            "type": "CONF_SEND",
            "conf_id": conf_id,
            "from_user": current_user,
            "content": content,
            "timestamp": time.time(),
        },
        expect_reply=True,
    )

    if not reply or reply.get("status") != "ok":
        print(f"[conf-send] Failed to send conference message: {reply}")
    else:
        print("[conf-send] Sent to conference.")


def print_help() -> None:
    print("Commands:")
    print("  help                     Show this help")
    print("  register                 Create a user on this node (home node)")
    print("  login                    Login as a local user")
    print("  logout                   Logout current user")
    print("  passwd                   Change current user's password")
    print("  whoami                   Show current user info")
    print("  friends                  List friends")
    print("  friend-requests          List pending friend requests")
    print("  friend-add <user>        Send a friend request (will ask for friend home host/port)")
    print("  friend-accept <user>     Accept a pending friend request")
    print("  online-friends           Check which friends are currently online")
    print("  msg <friend> <msg...>    Send a direct message")
    print("  conf-create <name> <users...>   Create a conference and invite users")
    print("  conf-list                List conferences you are in")
    print("  conf-send <conf_id> <msg...>    Send message to conference")
    print("  quit                     Exit this node")

def cli_loop() -> None:
    print("Type 'help' for commands.")
    while True:
        try:
            raw = input("> ").strip()
        except EOFError:
            break
        if not raw:
            continue
        parts = raw.split()
        cmd = parts[0]
        args = parts[1:]

        if cmd == "help":
            print_help()
        elif cmd == "register":
            cmd_register()
        elif cmd == "login":
            cmd_login()
        elif cmd == "logout":
            cmd_logout()
        elif cmd == "passwd":
            cmd_passwd()
        elif cmd == "whoami":
            cmd_whoami()
        elif cmd == "friends":
            cmd_friends()
        elif cmd == "friend-requests":
            cmd_friend_requests()
        elif cmd == "friend-add":
            cmd_friend_add(args)
        elif cmd == "friend-accept":
            cmd_friend_accept(args)
        elif cmd == "online-friends":
            cmd_online_friends()
        elif cmd == "msg":
            cmd_msg(args)
        elif cmd == "conf-create":
            cmd_conf_create(args)
        elif cmd == "conf-list":
            cmd_conf_list()
        elif cmd == "conf-send":
            cmd_conf_send(args)
        elif cmd == "quit":
            cmd_logout()
            print("Bye.")
            break
        else:
            print("Unknown command. Type 'help'.")


# --------------------------- Main ---------------------------

def main() -> None:
    global host, port, state_file
    parser = argparse.ArgumentParser(description="('Minimal' LOL UR RIGHT!) P2P conf-chat node (CLI only 4 now)")
    parser.add_argument("--host", default="127.0.0.1", help="Host/IP to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on")
    args = parser.parse_args()

    host = args.host
    port = args.port
    state_file = f"state_{port}.json"

    load_state()

    threading.Thread(target=server_loop, daemon=True).start()
    cli_loop()


if __name__ == "__main__":
    main()
