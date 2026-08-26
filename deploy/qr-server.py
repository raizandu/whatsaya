#!/usr/bin/env python3
"""Standalone HTML QR page for WhatsApp pairing.

Reads live status from the Baileys bridge inside the hermes container.
A leftover creds.json is NOT treated as connected — Hermes 0.20 and the
bridge can be down while that file still exists.

  GET /                  HTML
  GET /whatsapp          HTML
  GET /whatsapp/qr       HTML (PNG if ?format=png)
  GET /qr.png            PNG
  GET /whatsapp/status   JSON
"""
import io
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import qrcode

PORT = int(os.environ.get("WHATSAPP_QR_PORT", "8080"))
HOST = os.environ.get("WHATSAPP_QR_HOST", "127.0.0.1")
CONTAINER = os.environ.get("WHATSAPP_QR_CONTAINER", "hermes")
BRIDGE = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3000").rstrip("/")


def _curl(path, binary=False):
    cmd = [
        "docker", "exec", CONTAINER,
        "curl", "-sS", "-f", "--max-time", "3",
        "-H", "Host: 127.0.0.1",
        f"{BRIDGE}{path}",
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if binary:
        return out
    try:
        return json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def latest():
    data = _curl("/whatsapp/status")
    if not data:
        return "disconnected", None, False
    connected = bool(data.get("connected") or data.get("status") == "connected")
    qr_available = bool(data.get("qrAvailable"))
    qr = data.get("qr") if qr_available else None
    if connected:
        return "connected", None, True
    if qr_available:
        return "waiting", qr, True
    status = str(data.get("status") or "waiting")
    if status == "connected":
        return "disconnected", None, True
    return status, None, True


HTML = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="8">
  <title>Parear WhatsApp — Hermes</title>
  <style>
    body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
           background:#0b0d10; color:#f4f4f5; font-family:-apple-system,BlinkMacSystemFont,sans-serif; }}
    .card {{ width:min(420px,92vw); text-align:center; padding:28px 20px; }}
    h1 {{ font-size:20px; margin:0 0 8px; }}
    p {{ color:#a1a1aa; font-size:14px; line-height:1.45; }}
    img {{ width:280px; height:280px; background:#fff; padding:12px; border-radius:12px; }}
    .ok {{ color:#4ade80; font-weight:600; }}
    .wait {{ color:#fbbf24; }}
  </style>
</head>
<body>
  <div class="card">
    {body}
  </div>
</body>
</html>
"""


def page_body(status, qr, live):
    if status == "connected":
        return (
            "<h1 class='ok'>WhatsApp conectado</h1>"
            "<p>A ponte está no ar. Pode voltar ao Hermes.</p>"
        )
    if qr or status == "waiting":
        return (
            "<h1>Escaneie o QR</h1>"
            "<p class='wait'>WhatsApp → Aparelhos conectados → Conectar um aparelho</p>"
            "<p><img src='/qr.png' alt='QR WhatsApp'></p>"
            "<p>A página atualiza sozinha a cada 8 segundos.</p>"
        )
    if not live:
        return (
            "<h1>Ponte desligada</h1>"
            "<p>O Hermes ainda não subiu o WhatsApp. Recarregue em alguns segundos.</p>"
        )
    if status == "error":
        return "<h1>Falha na conexão</h1><p>Recarregue em alguns segundos. Um novo QR deve aparecer.</p>"
    return "<h1>Gerando QR…</h1><p>Recarregue em alguns segundos.</p>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _html(self, body, code=200):
        data = HTML.format(body=body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _png_bytes(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _png(self, qr):
        live_png = _curl("/whatsapp/qr?format=png", binary=True)
        if live_png and live_png[:8] == b"\x89PNG\r\n\x1a\n":
            self._png_bytes(live_png)
            return
        if not qr:
            self.send_error(404, "QR not ready")
            return
        img = qrcode.make(qr)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self._png_bytes(buf.getvalue())

    def _json(self, payload, code=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        fmt = (parse_qs(parsed.query).get("format") or [""])[0].lower()
        status, qr, live = latest()

        if path in ("/qr.png",) or (path == "/whatsapp/qr" and fmt in ("png", "image")):
            self._png(qr)
            return

        if path == "/whatsapp/status":
            self._json({
                "status": status,
                "qrAvailable": bool(qr) or status == "waiting",
                "connected": status == "connected",
                "live": live,
            })
            return

        if path in ("/", "/index.html", "/whatsapp", "/whatsapp/qr"):
            self._html(page_body(status, qr, live))
            return

        self.send_error(404)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
