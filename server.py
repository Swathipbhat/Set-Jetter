import json
import os
import threading
import time
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import agent

app = Flask(__name__, static_folder="static")
# Behind Render's proxy: trust one X-Forwarded-* hop so request.remote_addr is the real client.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Public endpoint spends the owner's Qloo (and optionally Anthropic) quota, so cap it per client.
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", 8))
_hits, _lock = defaultdict(deque), threading.Lock()


def _allowed(ip):
    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        if len(_hits) > 5000:  # keep memory bounded
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                _hits.pop(k, None)
        return True


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)  # Render health check; reveals nothing about configuration


@app.get("/api/plan")
def plan():
    inp = agent.normalize(request.args)
    ok = _allowed(request.remote_addr or "?")

    def stream():
        if not ok:
            yield "data: " + json.dumps({"type": "error", "message": "Too many searches from your connection. Please wait a minute and try again."}) + "\n\n"
        else:
            for ev in agent.run(inp):
                yield f"data: {json.dumps(ev)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})  # no proxy buffering of the live trace


@app.after_request
def secure(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")  # OSM tiles 403 without a Referer
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
