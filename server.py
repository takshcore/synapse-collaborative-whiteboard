"""
server.py — Collaborative Whiteboard & Voice Chat Server
=========================================================

ARCHITECTURE OVERVIEW (Viva Talking Point):
--------------------------------------------
This server implements a "Hybrid Decentralized" architecture:

  1. CENTRALISED SIGNALING (WebSocket/SocketIO):
     All non-audio events — joining, drawing, token control, mute state —
     flow through this Flask-SocketIO server. This guarantees consistent
     shared state across all participants without complex consensus logic.

  2. DECENTRALISED MEDIA (WebRTC, Peer-to-Peer):
     Voice audio NEVER passes through this server. Once two peers exchange
     SDP offers/answers (via this server as a "signaling relay"), they open
     a direct P2P audio channel. This keeps the server lightweight and
     eliminates audio latency introduced by server routing.

WHY FLASK + FLASK-SOCKETIO?
----------------------------
  - Flask is a micro-framework — minimal overhead, no ORM/database layer
    needed because our entire state is in-memory (see `rooms` dict below).
  - Flask-SocketIO wraps the Socket.IO protocol on top of Flask's HTTP
    server, giving us persistent, bidirectional WebSocket connections with
    automatic fallback to polling for older clients.

WHY EVENTLET?
--------------
  eventlet.monkey_patch() replaces Python's standard blocking I/O functions
  (socket, time.sleep, etc.) with non-blocking cooperative-multitasking
  equivalents. This allows Flask-SocketIO to serve hundreds of concurrent
  WebSocket connections from a SINGLE THREAD — each connection yields
  control voluntarily when it waits for I/O.
  Without monkey_patch, every socket connection would block the thread,
  requiring a thread-per-client model that doesn't scale.

WHY NO DATABASE?
-----------------
  For a real-time session (like a live meeting), persistent storage adds
  latency and complexity with no benefit. When the server restarts, all
  sessions naturally end anyway. An in-memory Python dict is O(1) for
  all operations and has zero I/O overhead.

State Schema:
  rooms[room_code] = {
    "host_sid": str,
    "token_holder_sid": str | None,   # None = open floor (no one drawing)
    "participants": {
      sid: {
        "name": str,
        "is_muted": bool,
        "hand_raised": bool,
        "is_host": bool,
      }
    },
    "canvas_strokes": [...]   # persistent for late-joiner canvas replay
  }
"""

# eventlet must be imported and monkey-patched BEFORE Flask is imported.
# This is because monkey_patch replaces Python's standard socket/threading
# modules at import time — if Flask's imports run first, they will have
# already cached references to the original blocking sockets.
import eventlet
eventlet.monkey_patch()

from flask import Flask, request
# join_room / leave_room are Flask-SocketIO helpers that add/remove a
# client's SID from a named SocketIO "room" — enabling targeted broadcasts
# using `emit(..., to=room_code)` without looping over individual SIDs.
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
# SECRET_KEY is required by Flask for session management and CSRF protection.
# In production this should come from an environment variable.
app.config['SECRET_KEY'] = 'wb_secret_2024'
# cors_allowed_origins='*' allows any origin to connect — needed because
# the PyQt6 client is a desktop app, not a browser, so it has no "origin".
# async_mode='eventlet' tells Flask-SocketIO to use the eventlet WSGI server.
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

# ── In-memory state ──────────────────────────────────────────────
# A plain Python dict acts as our entire database.
# Key: room_code (str), Value: room state dict (see schema above).
# All access is single-threaded thanks to eventlet's cooperative multitasking,
# so no locking (threading.Lock) is needed.
rooms: dict = {}


# ── Helpers ──────────────────────────────────────────────────────

def participants_info(room_code: str) -> dict:
    """
    Return a JSON-serializable snapshot of all participants in a room.

    WHY a helper?
    We need to send participant data in multiple places (join, broadcast_state,
    token updates). Centralising it here ensures consistent formatting and
    means we only need to update this one function if the schema changes.

    WHY include 'sid' in each value?
    When the client receives this dict, it only has access to the dict values.
    Embedding the SID inside the value means the client can identify each
    participant without needing to enumerate the dict keys separately.
    """
    room = rooms.get(room_code, {})
    return {
        # **info unpacks the stored fields; we add 'sid' for client-side use
        sid: {**info, "sid": sid}
        for sid, info in room.get("participants", {}).items()
    }


def broadcast_state(room_code: str, skip_sid: str = None):
    """
    Push the current participant list and token state to every client in the room.

    WHY broadcast after every state change?
    Rather than calculating deltas (who changed what), we push the full state.
    This is simpler, and for small rooms (< 50 people) the payload is trivial.
    The 'participant_update' event drives UI updates like the participants panel,
    mute indicators, and hand-raise icons on all clients simultaneously.

    skip_sid: Used on join to avoid sending the update back to the joiner
    who already received their personalised 'room_joined' event.
    """
    room = rooms[room_code]
    payload = {
        "participants": participants_info(room_code),
        "host_sid": room["host_sid"],
        "token_holder_sid": room["token_holder_sid"],
    }
    if skip_sid:
        # Skip the specified client — they already got personalised data
        emit('participant_update', payload, to=room_code, skip_sid=skip_sid)
    else:
        emit('participant_update', payload, to=room_code)


# ── Join / Leave ─────────────────────────────────────────────────
# ARCHITECTURE NOTE (Viva Talking Point):
# The join flow is the most critical event. It bootstraps two separate
# communication channels for the new user:
#   1. WebSocket state channel: the 'room_joined' event sends canvas history,
#      participant list, token state, and role assignment.
#   2. WebRTC media channel: the server instructs existing peers to initiate
#      P2P WebRTC connections to the new joiner. Audio flows peer-to-peer
#      after this — the server is no longer in the audio path.

@socketio.on('join_room_event')
def handle_join(data):
    """
    Handle a new participant joining or creating a room.

    Design Decision — First-joiner is Host:
    Rather than requiring a separate "create room" API, the first person to
    join a room_code automatically becomes the Host and receives the drawing
    token. Subsequent joiners are Participants. This "implicit creation"
    pattern simplifies the client — it only needs one button: "Join / Create".

    Canvas Replay for Late Joiners:
    All completed strokes and images are stored in canvas_strokes. When a
    late joiner connects, we send them the full history so their canvas
    matches the current state without any special sync protocol.
    """
    room_code = data['room_code']
    name = data['name']
    sid = request.sid  # Flask-SocketIO assigns a unique SID per WebSocket connection

    # Add this SID to Flask-SocketIO's internal room group.
    # After this call, emit(..., to=room_code) will reach this client.
    join_room(room_code)

    if room_code not in rooms:
        # First person → becomes Host and gets the drawing token
        rooms[room_code] = {
            "host_sid": sid,
            "token_holder_sid": sid,  # Host draws first by default
            "participants": {
                sid: {"name": name, "is_muted": False,
                      "hand_raised": False, "is_host": True}
            },
            "canvas_strokes": [],  # Empty history for a brand-new room
        }
        role = "host"
    else:
        # Subsequent joiners are regular participants with no drawing rights
        rooms[room_code]["participants"][sid] = {
            "name": name, "is_muted": False,
            "hand_raised": False, "is_host": False,
        }
        role = "participant"

    room = rooms[room_code]

    # Send full room state to the joiner ONLY (emit without to= targets sender)
    # canvas_strokes enables the late-joiner canvas replay described above.
    emit('room_joined', {
        "room_code": room_code,
        "role": role,
        "your_sid": sid,
        "host_sid": room["host_sid"],
        "token_holder_sid": room["token_holder_sid"],
        "participants": participants_info(room_code),
        "canvas_strokes": room["canvas_strokes"],  # Full history for replay
    })

    # Notify existing peers about the new participant (skip the joiner themselves)
    broadcast_state(room_code, skip_sid=sid)

    # BOOTSTRAP WebRTC: Tell every existing peer to initiate a WebRTC
    # offer to the new joiner. We tell the EXISTING peers (not the new one)
    # because the initiator of the WebRTC offer should be whoever is already
    # established in the room — they already have a running asyncio loop.
    existing = [s for s in room["participants"] if s != sid]
    for peer_sid in existing:
        # Each existing peer will receive this and call create_offer(sid)
        emit('webrtc_initiate', {"peer_sid": sid}, to=peer_sid)


@socketio.on('disconnect')
def handle_disconnect():
    """
    Clean up state when a WebSocket connection drops.

    This handles both voluntary disconnects (user closes app) and involuntary
    ones (network failure, timeout). Flask-SocketIO fires 'disconnect'
    automatically — we don't need to poll or heartbeat manually.

    Host Promotion:
    If the host leaves, we promote the next participant alphabetically
    (first key in the dict). This ensures the room always has a host who
    can clear the canvas and manage participants.

    Token Fallback:
    If the token holder leaves, the token reverts to the (possibly new) host.
    This prevents the room from being permanently locked without a drawer.
    """
    sid = request.sid
    for room_code, room in list(rooms.items()):
        if sid not in room["participants"]:
            continue  # This client wasn't in this room

        # Remove the leaving participant from the room's state
        del room["participants"][sid]

        # Notify all remaining peers that this person left (triggers WebRTC cleanup)
        emit('peer_left', {"sid": sid}, to=room_code)

        if not room["participants"]:
            # Last person left — delete the room to free memory
            del rooms[room_code]
            break

        # If host left, promote the first remaining participant
        if room["host_sid"] == sid:
            new_host = next(iter(room["participants"]))  # First key in dict
            room["host_sid"] = new_host
            room["participants"][new_host]["is_host"] = True

        # If token holder left, give token to host so drawing can continue
        if room["token_holder_sid"] == sid:
            room["token_holder_sid"] = room["host_sid"]

        # Push updated state to all remaining clients
        broadcast_state(room_code)
        break


# ── Canvas ───────────────────────────────────────────────────────
# ARCHITECTURE NOTE (Viva Talking Point):
# The canvas uses a "Token System" to enforce mutual exclusion — only one
# person draws at a time. This is a deliberate design choice:
#
#   PROBLEM: If two people draw simultaneously, we'd need Operational
#   Transforms (OT) or CRDTs (like Google Docs uses) to merge concurrent
#   edits. That's complex distributed systems territory.
#
#   SOLUTION: Serialize writes. Only the token holder can send draw events.
#   The server enforces this by rejecting draw events from non-holders.
#   This trades concurrency for simplicity — acceptable for a presentation tool.

@socketio.on('draw_stroke')
def handle_draw(data):
    """
    Relay a completed stroke to all other clients in the room.

    WHY store strokes server-side?
    So that late joiners can replay the full canvas history when they connect.
    Without server-side storage, a person joining mid-session would see a
    blank canvas even though others have been drawing for 10 minutes.

    WHY validate token ownership?
    Security + correctness. Without this check, a malicious or buggy client
    could send draw events even without the token, corrupting the shared canvas.
    """
    room_code = data['room_code']
    stroke = data['stroke']  # Dict: {points: [{x,y},...], color: str, width: int}
    sid = request.sid
    room = rooms.get(room_code)
    # Reject if room doesn't exist or sender doesn't hold the drawing token
    if not room or room["token_holder_sid"] != sid:
        return
    # Persist the stroke for late-joiner replay
    room["canvas_strokes"].append(stroke)
    # Broadcast to everyone EXCEPT the sender (who already drew it locally)
    emit('draw_stroke', {"stroke": stroke}, to=room_code, skip_sid=sid)


@socketio.on('clear_canvas')
def handle_clear(data):
    """
    Clear the shared canvas — host-only operation.

    WHY only the host can clear?
    Clearing affects everyone's view permanently. Restricting it to the host
    prevents accidental or malicious wipes by participants. This mirrors
    real classroom/meeting room etiquette where a moderator controls the board.

    We also wipe canvas_strokes so late joiners see a clean canvas too.
    """
    room_code = data['room_code']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or room["host_sid"] != sid:
        return
    room["canvas_strokes"] = []  # Clear replay history
    emit('canvas_cleared', {}, to=room_code)  # Tell every client to blank their canvas


@socketio.on('insert_image')
def handle_insert_image(data):
    """
    Relay a Base64 image stamp to all peers in the room.

    WHY Base64 over the WebSocket?
    We chose Base64 encoding over a separate file upload endpoint because:
      1. Simplicity: No HTTP multipart form, no file storage, no URL generation.
      2. Atomicity: The image arrives as a single WebSocket message with its
         position (x, y), so rendering order is guaranteed by SocketIO.
      3. Acceptable size: Images are pre-scaled to max 800px on the client
         (~100-200KB as Base64), well within WebSocket message limits.

    Only the current token holder may insert images (same permission
    as drawing strokes).  The payload is stored in canvas_strokes with
    type='image' so late-joiners receive a full canvas replay.
    """
    room_code = data.get('room_code')
    sid = request.sid
    room = rooms.get(room_code)
    if not room:
        return
    # Enforce token ownership — only the drawer can stamp images
    if room['token_holder_sid'] != sid:
        return

    payload = {
        'type': 'image',          # Distinguishes images from stroke dicts in replay
        'base64_data': data.get('base64_data', ''),
        'x': data.get('x', 0),   # Top-left corner of where the image is placed
        'y': data.get('y', 0),
    }
    # Persist for late-joiner canvas replay (same list as strokes)
    room['canvas_strokes'].append(payload)
    # Broadcast to everyone else in the room (sender already stamped locally)
    emit('image', payload, to=room_code, skip_sid=sid)


# ── Token System ─────────────────────────────────────────────────
# ARCHITECTURE NOTE (Viva Talking Point):
# The token is a logical "drawing permission". Only one SID holds it at a time.
# Transferring it requires explicit approval (like a "talking stick" in meetings).
# This creates a natural turn-taking protocol without complex locking or queuing.

@socketio.on('request_token')
def handle_request_token(data):
    """
    A participant requests the drawing token.

    Two cases:
      1. Token is free (None): Grant immediately and start a fresh canvas.
         This happens after the previous holder used 'drop_token'.
      2. Token is held: Forward the request to the current holder as a
         'token_request_incoming' event. The holder's client shows a
         Yes/No dialog and responds via 'approve_token'.

    WHY clear strokes on token transfer?
    Each token holder gets a "fresh slate". This is a pedagogical design choice:
    it separates each presenter's content clearly, like changing slides.
    """
    room_code = data['room_code']
    sid = request.sid
    room = rooms.get(room_code)
    if not room:
        return
    p = room["participants"].get(sid)
    if not p:
        return

    holder = room["token_holder_sid"]
    if holder is None:
        # Token is free — grant immediately and start a fresh canvas
        room["token_holder_sid"] = sid
        room["canvas_strokes"] = []          # Wipe stored strokes for fresh start
        emit('token_update', {
            "token_holder_sid": sid,
            "participants": participants_info(room_code),
        }, to=room_code)
        emit('canvas_cleared', {}, to=room_code)  # Blank canvas for everyone
    else:
        # Ask the current holder — they will respond via 'approve_token'
        emit('token_request_incoming', {
            "requester_sid": sid,
            "requester_name": p["name"],
        }, to=holder)


@socketio.on('approve_token')
def handle_approve_token(data):
    """
    The current token holder approves or denies a transfer request.

    WHY require approval?
    Forced token transfer would be disruptive mid-drawing. The approval
    dialog ensures the holder can finish their current thought before
    passing control — similar to how a presenter hands off a slide clicker.
    """
    room_code = data['room_code']
    requester_sid = data['requester_sid']
    approved = data['approved']
    sid = request.sid
    room = rooms.get(room_code)
    # Only the actual token holder can approve — prevents spoofing
    if not room or room["token_holder_sid"] != sid:
        return

    if approved:
        # Transfer the token and give the new holder a fresh canvas
        room["token_holder_sid"] = requester_sid
        room["canvas_strokes"] = []          # New holder, fresh canvas
        emit('token_update', {
            "token_holder_sid": requester_sid,
            "participants": participants_info(room_code),
        }, to=room_code)
        emit('canvas_cleared', {}, to=room_code)  # Blank canvas for everyone
    else:
        # Notify the requester they were denied (only they see this message)
        emit('token_denied', {"reason": "The holder declined your request."}, to=requester_sid)


@socketio.on('drop_token')
def handle_drop_token(data):
    """
    The token holder voluntarily relinquishes control.

    Two behaviours:
      - Host drops → token becomes None (open floor). Any participant can
        request it and receive it immediately without needing approval.
      - Participant drops → token returns to the host automatically.
        This prevents a situation where no one can draw after a participant
        leaves without explicitly dropping the token.
    """
    room_code = data['room_code']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or room["token_holder_sid"] != sid:
        return

    if sid == room["host_sid"]:
        room["token_holder_sid"] = None   # Host drops → open floor
    else:
        room["token_holder_sid"] = room["host_sid"]  # Participant → back to host

    room["canvas_strokes"] = []              # Token moved, fresh canvas
    emit('token_update', {
        "token_holder_sid": room["token_holder_sid"],
        "participants": participants_info(room_code),
    }, to=room_code)
    emit('canvas_cleared', {}, to=room_code)  # Blank canvas for everyone


# ── Mute / Hand ──────────────────────────────────────────────────
# These events manage audio state and participant signaling.
# Note: the server only tracks IS_MUTED state and relays commands.
# Actual microphone muting/unmuting is done CLIENT-SIDE by setting
# MicrophoneTrack.is_muted, which makes recv() yield silent frames.
# The server cannot directly control a client's microphone — it can
# only tell the client to mute itself, which the client must honour.

@socketio.on('toggle_mute')
def handle_toggle_mute(data):
    """Toggle the calling participant's mute state and broadcast to all peers."""
    room_code = data['room_code']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or sid not in room["participants"]:
        return
    # XOR toggle — True→False, False→True
    room["participants"][sid]["is_muted"] = not room["participants"][sid]["is_muted"]
    broadcast_state(room_code)  # Update participant panel icons for all


@socketio.on('toggle_hand')
def handle_toggle_hand(data):
    """Toggle the calling participant's hand-raise state and broadcast to all peers."""
    room_code = data['room_code']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or sid not in room["participants"]:
        return
    room["participants"][sid]["hand_raised"] = not room["participants"][sid]["hand_raised"]
    broadcast_state(room_code)


@socketio.on('force_mute')
def handle_force_mute(data):
    """
    Host only — force-mutes a specific participant.

    WHY server-side enforcement?
    The host cannot directly control the participant's microphone (WebRTC is P2P).
    Instead, we send 'you_are_muted' to the target client. The client then
    sets MicrophoneTrack.is_muted = True, which causes recv() to emit silence.
    This is a "cooperative" mute — it relies on the client being honest.
    A truly adversarial client could ignore this, but that's acceptable for
    an academic/collaborative tool (not a high-security application).
    """
    room_code = data['room_code']
    target_sid = data['target_sid']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or room["host_sid"] != sid:  # Only host can force-mute
        return
    if target_sid in room["participants"]:
        room["participants"][target_sid]["is_muted"] = True
        emit('you_are_muted', {}, to=target_sid)  # Tell target to mute itself
        broadcast_state(room_code)  # Update mute icon for all participants


@socketio.on('ask_unmute')
def handle_ask_unmute(data):
    """
    Host only — sends an unmute request to a specific participant.

    Unlike force_mute, unmuting requires the participant's consent.
    The participant's client shows a dialog; they can accept or decline.
    This respects user privacy — a host cannot unmute someone without
    their knowledge (important for classroom or meeting contexts).
    """
    room_code = data['room_code']
    target_sid = data['target_sid']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or room["host_sid"] != sid:
        return
    if target_sid in room["participants"]:
        emit('unmute_request', {}, to=target_sid)  # Target will show a Yes/No dialog


@socketio.on('unmute_response')
def handle_unmute_response(data):
    """Participant responds to host's unmute request (accepted = True/False)."""
    room_code = data['room_code']
    accepted = data['accepted']
    sid = request.sid
    room = rooms.get(room_code)
    if not room or sid not in room["participants"]:
        return
    if accepted:
        # Update server-side state and push new participant list to all
        room["participants"][sid]["is_muted"] = False
        broadcast_state(room_code)


# ── WebRTC Signaling Relay ────────────────────────────────────────
# ARCHITECTURE NOTE — The Most Critical Part (Viva Talking Point):
#
# WebRTC requires an "out-of-band signaling channel" to bootstrap P2P
# connections. Before two peers can send audio directly to each other,
# they must exchange:
#   1. SDP Offer/Answer: describes codec capabilities, media types.
#   2. ICE Candidates: describes network paths (IP:port combos) that
#      might be used to reach each peer.
#
# This server acts as a "dumb relay" for that exchange — it FORWARDS
# these messages without INSPECTING or MODIFYING them. It has no idea
# what codec they agreed on or what IP addresses they'll use.
#
# Once the ICE negotiation completes, audio flows DIRECTLY peer-to-peer
# over UDP — this server is completely out of the audio path.
#
# This is the "decentralized" part of our hybrid architecture.

@socketio.on('webrtc_offer')
def handle_webrtc_offer(data):
    """
    Relay an SDP Offer from initiator to the target peer.
    The server adds 'from_sid' so the receiver knows who sent it.
    """
    emit('webrtc_offer', {
        "from_sid": request.sid,  # Receiver needs this to know who to answer
        "offer": data['offer'],   # SDP string — server does NOT parse this
    }, to=data['target_sid'])


@socketio.on('webrtc_answer')
def handle_webrtc_answer(data):
    """
    Relay an SDP Answer from the responder back to the offer initiator.
    After this exchange, both peers have each other's media descriptions.
    """
    emit('webrtc_answer', {
        "from_sid": request.sid,
        "answer": data['answer'],  # SDP string — server does NOT parse this
    }, to=data['target_sid'])


@socketio.on('webrtc_ice')
def handle_webrtc_ice(data):
    """
    Relay an ICE Candidate between peers.
    ICE candidates are potential network paths (local IP, STUN-derived
    public IP, TURN relay IP). Peers exchange all candidates and try
    each one until they find a working path for UDP audio.
    """
    emit('webrtc_ice', {
        "from_sid": request.sid,
        "candidate": data['candidate'],  # ICE candidate string — not parsed
    }, to=data['target_sid'])


# ── Entry Point ───────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 55)
    print("  Collaborative Whiteboard Server")
    print("  Listening on  http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    # host='0.0.0.0' binds to ALL network interfaces (not just localhost).
    # This is required so that clients on other machines on the same network
    # can reach the server using the host machine's local IP address.
    # debug=False: eventlet and Flask's debugger (Werkzeug reloader) conflict.
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
