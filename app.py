import os
import sys
import uuid
import time
import queue
import json
import threading
import subprocess
from flask import Flask, render_template, request, Response, jsonify

app = Flask(__name__)

# Active shell sessions: session_id -> dict
sessions = {}

def cleanup_sessions_loop():
    """Background thread loop to terminate idle sessions."""
    while True:
        time.sleep(60)
        now = time.time()
        idle_timeout = 600  # 10 minutes of inactivity
        to_delete = []
        
        for sid, sess in list(sessions.items()):
            if now - sess["last_activity"] > idle_timeout:
                to_delete.append(sid)
                
        for sid in to_delete:
            print(f"Cleaning up inactive session {sid}")
            sess = sessions.pop(sid, None)
            if sess:
                try:
                    sess["proc"].terminate()
                    sess["proc"].wait(timeout=2)
                except Exception:
                    try:
                        sess["proc"].kill()
                    except Exception:
                        pass

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_sessions_loop, daemon=True)
cleanup_thread.start()


@app.route("/")
def index():
    """Render the main shell interface."""
    return render_template("index.html")


@app.route("/api/session", methods=["POST"])
def create_session():
    """Spawn a new shell subprocess and return its session ID and welcome prompt."""
    sid = str(uuid.uuid4())
    shell_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "myshell.py")
    
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", shell_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=0
        )
    except Exception as e:
        return jsonify({"error": f"Failed to start shell process: {e}"}), 500
        
    q = queue.Queue()
    
    def reader_thread():
        while True:
            try:
                char = proc.stdout.read(1)
                if not char:
                    break
                q.put(char)
            except Exception:
                break
                
    t = threading.Thread(target=reader_thread, daemon=True)
    t.start()
    
    # Wait briefly to capture the welcome banner and prompt
    time.sleep(0.5)
    welcome = ""
    while not q.empty():
        welcome += q.get_nowait()
        
    sessions[sid] = {
        "proc": proc,
        "queue": q,
        "thread": t,
        "last_activity": time.time()
    }
    
    return jsonify({
        "session_id": sid,
        "welcome": welcome
    })


@app.route("/api/session/<sid>", methods=["DELETE"])
def delete_session(sid):
    """Terminate and clean up a specific session."""
    sess = sessions.pop(sid, None)
    if sess:
        try:
            sess["proc"].terminate()
            sess["proc"].wait(timeout=1)
        except Exception:
            try:
                sess["proc"].kill()
            except Exception:
                pass
        return jsonify({"status": "closed"})
    return jsonify({"error": "Session not found"}), 404


@app.route("/api/execute", methods=["POST"])
def execute():
    """Receive a shell command and stream execution output using Server-Sent Events (SSE)."""
    data = request.json or {}
    sid = data.get("session_id")
    cmd = data.get("command", "")
    
    sess = sessions.get(sid)
    if not sess:
        return jsonify({"error": "Invalid or expired session"}), 404
        
    sess["last_activity"] = time.time()
    proc = sess["proc"]
    q = sess["queue"]
    
    token = uuid.uuid4().hex
    sentinel = f"__MYSHELL_FINISHED_{token}__"
    
    # Write command and sentinel to subprocess stdin
    try:
        proc.stdin.write(f"{cmd} ; echo {sentinel} $?\n")
        proc.stdin.flush()
    except Exception as e:
        return jsonify({"error": f"Failed to send command to shell: {e}"}), 500
        
    def generate():
        buffer = ""
        while True:
            try:
                char = q.get(timeout=30.0)
                buffer += char
                
                # Check for sentinel completion
                if sentinel in buffer:
                    while not buffer.endswith("\n"):
                        buffer += q.get(timeout=2.0)
                        
                    idx = buffer.find(sentinel)
                    before_sentinel = buffer[:idx]
                    if before_sentinel:
                        yield f"data: {json.dumps(before_sentinel)}\n\n"
                        
                    # Let the shell print the prompt, then grab it
                    time.sleep(0.05)
                    prompt = ""
                    while not q.empty():
                        prompt += q.get_nowait()
                    if prompt:
                        yield f"data: {json.dumps(prompt)}\n\n"
                    break
                
                # Match longest prefix of the sentinel to avoid partial yield
                longest_prefix_len = 0
                for i in range(1, min(len(buffer), len(sentinel)) + 1):
                    prefix = sentinel[:i]
                    if buffer.endswith(prefix):
                        longest_prefix_len = i
                        
                if longest_prefix_len > 0:
                    yield_len = len(buffer) - longest_prefix_len
                    if yield_len > 0:
                        yield f"data: {json.dumps(buffer[:yield_len])}\n\n"
                        buffer = buffer[yield_len:]
                else:
                    yield f"data: {json.dumps(buffer)}\n\n"
                    buffer = ""
                    
            except queue.Empty:
                yield f"data: {json.dumps('\n[Command execution timed out]\n')}\n\n"
                break
                
    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    # Start Flask server locally
    app.run(host="127.0.0.1", port=5000, debug=True)
