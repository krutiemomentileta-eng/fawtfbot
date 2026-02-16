import os
import json
from http.server import BaseHTTPRequestHandler
from _utils import (
    BOT_TOKEN, get_db, validate_init_data,
    get_or_create_user, get_channels, get_prizes, json_response
)

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        
        init_data = body.get("initData", "")
        validated = validate_init_data(init_data, BOT_TOKEN)
        
        if not validated or not validated.get("user"):
            resp = json_response({"error": "Invalid initData"}, 401)
            self._send(resp)
            return
        
        user_info = validated["user"]
        db = get_db()
        user = get_or_create_user(db, user_info["id"], user_info)
        
        # Каналы из БД (бот распарсил их сам)
        channels = get_channels(db)
        channels_out = []
        for ch in channels:
            channels_out.append({
                "id": ch["channel_id"],
                "name": ch["title"],
                "link": ch["invite_link"],
                "avatar": ch["avatar_base64"],  # data:URL — работает в <img src="">
            })
        
        # Призы из БД
        prizes = get_prizes(db)
        prizes_out = []
        for p in prizes:
            prizes_out.append({
                "key": p["key"],
                "tgs": p["tgs_file"],
                "name": p["name"],
                "emoji": p["emoji"],
            })
        
        resp = json_response({
            "ok": True,
            "user": {
                "telegram_id": user["telegram_id"],
                "first_name": user.get("first_name", ""),
                "state": user["state"],
                "prize_key": user.get("prize_key"),
                "prize_name": user.get("prize_name"),
            },
            "channels": channels_out,
            "prizes": prizes_out,
        })
        self._send(resp)
    
    def _send(self, resp):
        self.send_response(resp["statusCode"])
        for k, v in resp["headers"].items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp["body"].encode())