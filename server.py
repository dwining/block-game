#!/usr/bin/env python3
"""Standalone HTTP server for Block Blast game.

Serves static files from its own directory and a JSON API backed by a
persistent SQLite database (game.db). Uses only the Python standard library.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "game.db")

NICK_RE = re.compile(r"^[A-Za-z0-9 _.\-]+$")
NICK_MIN = 3
NICK_MAX = 16

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            nickname TEXT PRIMARY KEY,
            level INTEGER NOT NULL DEFAULT 1,
            score INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def is_valid_nickname(nick):
    if nick is None:
        return False
    if not NICK_MIN <= len(nick) <= NICK_MAX:
        return False
    return bool(NICK_RE.match(nick))


def send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "BlockBlastServer/1.0"

    # ---- CORS / OPTIONS ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---- API helpers ----
    def _handle_api(self, parsed_path, query):
        path = parsed_path.path
        method = self.command

        if method == "POST" and path == "/api/register":
            return self.api_register()
        if method == "GET" and path == "/api/check":
            return self.api_check(query)
        if method == "POST" and path == "/api/progress":
            return self.api_progress()
        if method == "GET" and path == "/api/leaderboard":
            return self.api_leaderboard()

        self.send_error(404, "Not Found")

    def api_register(self):
        data = read_json_body(self)
        if data is None:
            return send_json(self, 400, {"error": "JSON tidak valid"})
        nickname = (data.get("nickname") or "").strip()
        if not is_valid_nickname(nickname):
            return send_json(
                self,
                400,
                {"error": "Nama tidak valid (3-16 huruf/angka/spasi/_.-)"},
            )
        try:
            conn = get_conn()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM users WHERE LOWER(nickname)=LOWER(?)",
                    (nickname,),
                )
                if cur.fetchone() is not None:
                    return send_json(self, 409, {"error": "Nama sudah dipakai"})
                ts = now_iso()
                conn.execute(
                    "INSERT INTO users (nickname, level, score, created_at, updated_at) "
                    "VALUES (?, 1, 0, ?, ?)",
                    (nickname, ts, ts),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            return send_json(self, 500, {"error": "Database error"})
        return send_json(
            self, 200, {"ok": True, "nickname": nickname, "level": 1, "score": 0}
        )

    def api_check(self, query):
        raw = query.get("nickname", [""])[0]
        nickname = unquote(raw).strip()
        valid = is_valid_nickname(nickname)
        if not valid:
            return send_json(self, 200, {"exists": False, "valid": False})
        try:
            conn = get_conn()
            try:
                cur = conn.execute(
                    "SELECT 1 FROM users WHERE LOWER(nickname)=LOWER(?)",
                    (nickname,),
                )
                exists = cur.fetchone() is not None
            finally:
                conn.close()
        except sqlite3.Error:
            return send_json(self, 500, {"error": "Database error"})
        return send_json(self, 200, {"exists": exists, "valid": True})

    def api_progress(self):
        data = read_json_body(self)
        if data is None:
            return send_json(self, 400, {"error": "JSON tidak valid"})
        nickname = (data.get("nickname") or "").strip()
        try:
            level = int(data.get("level", 0))
            score = int(data.get("score", 0))
        except (TypeError, ValueError):
            return send_json(self, 400, {"error": "JSON tidak valid"})

        try:
            conn = get_conn()
            try:
                cur = conn.execute(
                    "SELECT nickname FROM users WHERE LOWER(nickname)=LOWER(?)",
                    (nickname,),
                )
                row = cur.fetchone()
                if row is None:
                    return send_json(self, 404, {"error": "Pemain tidak ditemukan"})
                stored = row[0]
                ts = now_iso()
                conn.execute(
                    "UPDATE users SET level = MAX(level, ?), score = MAX(score, ?), "
                    "updated_at = ? WHERE LOWER(nickname)=LOWER(?)",
                    (level, score, ts, nickname),
                )
                conn.commit()
                cur = conn.execute(
                    "SELECT level, score FROM users WHERE LOWER(nickname)=LOWER(?)",
                    (nickname,),
                )
                upd = cur.fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return send_json(self, 500, {"error": "Database error"})
        return send_json(
            self,
            200,
            {
                "ok": True,
                "nickname": stored,
                "level": upd[0],
                "score": upd[1],
            },
        )

    def api_leaderboard(self):
        try:
            conn = get_conn()
            try:
                cur = conn.execute(
                    "SELECT nickname, level, score FROM users "
                    "ORDER BY level DESC, score DESC LIMIT 12"
                )
                rows = [
                    {"nickname": r[0], "level": r[1], "score": r[2]}
                    for r in cur.fetchall()
                ]
            finally:
                conn.close()
        except sqlite3.Error:
            return send_json(self, 500, {"error": "Database error"})
        return send_json(self, 200, {"rows": rows})

    # ---- Static files ----
    def _serve_static(self, path):
        if path == "/" or path == "":
            rel = "index.html"
        else:
            rel = path.lstrip("/")

        full = os.path.normpath(os.path.join(BASE_DIR, rel))
        if not full.startswith(BASE_DIR):
            self.send_error(403, "Forbidden")
            return
        if not os.path.isfile(full):
            self.send_error(404, "Not Found")
            return

        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "Not Found")
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ---- dispatch ----
    def _ensure_table(self):
        try:
            conn = get_conn()
            conn.close()
        except sqlite3.Error:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._handle_api(parsed, parse_qs(parsed.query))
        self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._handle_api(parsed, parse_qs(parsed.query))
        self.send_error(404, "Not Found")


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            port = 8000

    get_conn().close()  # create table once on startup
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        "Block Blast server berjalan di http://localhost:{}  "
        "(Ctrl+C untuk berhenti)".format(port)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
        server.shutdown()


if __name__ == "__main__":
    main()
