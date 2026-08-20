#!/usr/bin/env python3
"""Evolution GO webhook receiver.

Standalone HTTP server that:

  - Binds 0.0.0.0 on a free port, auto-discovered on every startup. Never
    refuses to start because "that port is taken" -- it just keeps scanning.
  - Prefers reusing the last port it picked (persisted in ``.port`` next to
    this file) so restarts don't shuffle the port pointlessly; only picks a
    new one if the old one is no longer free.
  - After binding, registers itself as the webhook for one evolution-go
    instance via ``POST /instance/connect`` (hot update, does not disconnect
    the WhatsApp session), so evolution-go always knows the *current* port
    even if it changed. The instance token is fetched fresh from
    ``/instance/all`` using GLOBAL_API_KEY -- never hardcoded here.
  - Persists every received event as one row in a ``webhook_events`` table
    on the *same* Postgres evolution-go already uses (``POSTGRES_USERS_DB``
    in .env), so events are queryable with SQL instead of grepped out of a
    flat file. The table is created on startup if it doesn't exist yet;
    nothing else in that database is touched.

Run directly for testing:
    .venv/bin/python3 tools/webhook-listener/receiver.py

Normally installed as a systemd service by
``scripts/install-webhook-service.sh`` (torn down by
``scripts/uninstall-webhook-service.sh``), which provisions ``.venv`` and
runs the listener from inside it -- ``psycopg2`` is not stdlib.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent  # evolution-go/
PORT_STATE_FILE = ROOT / ".port"
ENV_FILE = REPO_ROOT / ".env"

BIND_HOST = "0.0.0.0"
DEFAULT_BASE_PORT = int(os.environ.get("WEBHOOK_BASE_PORT", "4001"))
PORT_SCAN_RANGE = 200

EVOLUTION_BASE_URL = os.environ.get("EVOLUTION_BASE_URL", "http://127.0.0.1:4000")
INSTANCE_NAME = os.environ.get("WEBHOOK_INSTANCE_NAME", "test01")
SUBSCRIBE_EVENTS = [
    e.strip()
    for e in os.environ.get("WEBHOOK_SUBSCRIBE", "MESSAGE,SEND_MESSAGE,HISTORY_SYNC").split(",")
    if e.strip()
]


def read_env_var(name: str) -> str | None:
    """Read one KEY=value out of evolution-go/.env. Never prints secrets."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


def db_dsn() -> str:
    dsn = read_env_var("POSTGRES_USERS_DB")
    if not dsn:
        raise RuntimeError("POSTGRES_USERS_DB not set in .env -- can't store events.")
    return dsn


def get_conn():
    return psycopg2.connect(db_dsn())


def ensure_table() -> None:
    """Create webhook_events and conversation_messages if they don't exist
    yet. Only touches these two tables -- never migrates or alters anything
    evolution-go itself owns (note: evolution-go's own GORM model already
    claims the plain "messages" table name for delivery receipts, hence the
    longer name here to avoid colliding with it).

    webhook_events = raw ingestion log, every payload, unprocessed. Kept as
    an audit trail.

    conversation_messages = the one normalized place actual message content
    lives, keyed by (chat_jid, message_id) -- WhatsApp's own message ID,
    which is stable no matter which path delivers a copy of the message (a
    live webhook event today, or a bulk history-sync resync down the road).
    The unique constraint means a message can never end up duplicated even
    if two different capture paths both see it.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id BIGSERIAL PRIMARY KEY,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                instance_name TEXT,
                event_type TEXT,
                chat_jid TEXT,
                is_from_me BOOLEAN,
                remote_addr TEXT,
                payload JSONB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_events_chat_jid
                ON webhook_events (chat_jid);
            CREATE INDEX IF NOT EXISTS idx_webhook_events_event_type
                ON webhook_events (event_type);
            CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at
                ON webhook_events (received_at);

            CREATE TABLE IF NOT EXISTS conversation_messages (
                id BIGSERIAL PRIMARY KEY,
                message_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                is_from_me BOOLEAN,
                sender_jid TEXT,
                push_name TEXT,
                message_type TEXT,
                text_body TEXT,
                wa_timestamp TIMESTAMPTZ,
                captured_via TEXT NOT NULL,
                raw JSONB NOT NULL,
                inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (chat_jid, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_chat_jid
                ON conversation_messages (chat_jid);
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_wa_timestamp
                ON conversation_messages (wa_timestamp);
            """
        )
    print("[webhook-listener] webhook_events + conversation_messages tables ready.")


def extract_text(message_obj: dict) -> str | None:
    """Best-effort text extraction across the WhatsApp message shapes we've
    actually seen (and the common ones we haven't yet). Returns None rather
    than guessing for shapes it doesn't recognize (e.g. pure media)."""
    if not message_obj:
        return None
    if "conversation" in message_obj:
        return message_obj["conversation"]
    for key in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
        node = message_obj.get(key)
        if isinstance(node, dict):
            text = node.get("text") or node.get("caption")
            if text:
                return text
    return None


def upsert_message(body: dict) -> None:
    """Extract a normalized row from a live Message/SendMessage event and
    upsert it into `conversation_messages`. No-op for events that aren't a
    single message (HistorySync, Presence, etc.) -- those stay in
    webhook_events only until a HistorySync extractor is written."""
    info = (body.get("data") or {}).get("Info") or {}
    message_id = info.get("ID")
    chat_jid = info.get("Chat")
    if not message_id or not chat_jid:
        return

    text_body = extract_text((body.get("data") or {}).get("Message") or {})

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversation_messages
                (message_id, chat_jid, is_from_me, sender_jid, push_name,
                 message_type, text_body, wa_timestamp, captured_via, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_jid, message_id) DO NOTHING
            """,
            (
                message_id,
                chat_jid,
                info.get("IsFromMe"),
                info.get("Sender"),
                info.get("PushName"),
                info.get("Type"),
                text_body,
                info.get("Timestamp"),
                "live",
                psycopg2.extras.Json(body),
            ),
        )


def insert_event(remote_addr: str, path: str, payload: dict) -> None:
    body = payload.get("body", payload)  # tolerate both wrapped and raw shapes
    event_type = body.get("event")
    info = (body.get("data") or {}).get("Info") or {}
    chat_jid = info.get("Chat")
    is_from_me = info.get("IsFromMe")
    instance_name = body.get("instanceName")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_events
                (instance_name, event_type, chat_jid, is_from_me, remote_addr, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                instance_name,
                event_type,
                chat_jid,
                is_from_me,
                remote_addr,
                psycopg2.extras.Json(payload),
            ),
        )


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((BIND_HOST, port))
            return True
        except OSError:
            return False


def pick_port() -> int:
    """Reuse the last port if it's still free; otherwise scan upward from
    DEFAULT_BASE_PORT for the first free one. Raises only if the whole range
    is exhausted, which basically never happens in practice."""
    if PORT_STATE_FILE.exists():
        try:
            last_port = int(PORT_STATE_FILE.read_text().strip())
            if port_is_free(last_port):
                return last_port
        except ValueError:
            pass

    for port in range(DEFAULT_BASE_PORT, DEFAULT_BASE_PORT + PORT_SCAN_RANGE):
        if port_is_free(port):
            return port

    raise RuntimeError(
        f"No free port found in {DEFAULT_BASE_PORT}-{DEFAULT_BASE_PORT + PORT_SCAN_RANGE}"
    )


def persist_port(port: int) -> None:
    PORT_STATE_FILE.write_text(str(port))


def register_webhook(port: int) -> None:
    """Tell evolution-go where to POST events from now on. Best-effort: a
    slow or offline API here should never take the listener down."""
    global_key = read_env_var("GLOBAL_API_KEY")
    if not global_key:
        print("[webhook-listener] GLOBAL_API_KEY not found in .env, skipping registration.", file=sys.stderr)
        return

    req = urllib.request.Request(
        f"{EVOLUTION_BASE_URL}/instance/all",
        headers={"apikey": global_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            instances = json.loads(resp.read()).get("data", [])
    except (urllib.error.URLError, OSError) as exc:
        print(f"[webhook-listener] Could not reach evolution-go to list instances: {exc}", file=sys.stderr)
        return

    instance = next((i for i in instances if i.get("name") == INSTANCE_NAME), None)
    if not instance:
        print(f"[webhook-listener] Instance '{INSTANCE_NAME}' not found, skipping registration.", file=sys.stderr)
        return

    body = json.dumps(
        {
            "webhookUrl": f"http://127.0.0.1:{port}/webhook",
            "subscribe": SUBSCRIBE_EVENTS,
        }
    ).encode()

    req = urllib.request.Request(
        f"{EVOLUTION_BASE_URL}/instance/connect",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "apikey": instance["token"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            print(f"[webhook-listener] Registered on instance '{INSTANCE_NAME}' (port {port}): {resp.read().decode()}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"[webhook-listener] Failed to register webhook: {exc}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    server_version = "EvoWebhookListener/1.0"

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, {"ok": True})
        else:
            self._write_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode(errors="replace")}

        entry = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "remote_addr": self.client_address[0],
            "path": self.path,
            "body": payload,
        }

        try:
            insert_event(self.client_address[0], self.path, entry)
            if payload.get("event") in ("Message", "SendMessage"):
                upsert_message(payload)
        except Exception as exc:  # noqa: BLE001 -- never crash the listener on a DB hiccup
            sys.stderr.write(f"[webhook-listener] Failed to store event in Postgres: {exc}\n")
            self._write_json(502, {"ok": False, "error": "storage failed"})
            return

        self._write_json(200, {"ok": True})

    def log_message(self, fmt, *args):  # noqa: A003 -- stdlib override
        sys.stderr.write(f"[webhook-listener] {self.address_string()} - {fmt % args}\n")


def main() -> None:
    ensure_table()

    port = pick_port()
    persist_port(port)
    print(f"[webhook-listener] Binding {BIND_HOST}:{port}")

    server = ThreadingHTTPServer((BIND_HOST, port), Handler)

    # Registration happens in the background so a slow/offline evolution-go
    # never delays the listener itself from coming up.
    threading.Thread(target=register_webhook, args=(port,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
