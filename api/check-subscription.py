import os
import json
from http.server import BaseHTTPRequestHandler
from _utils import (
    BOT_TOKEN, get_db, validate_init_data,
    get_or_create_user, get_channels, check_membership, json_response
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
        action = body.get("action", "check")
        
        validated = validate_init_data(init_data, BOT_TOKEN)
        if not validated or not validated.get("user"):
            self._send(json_response({"error": "Invalid initData"}, 401))
            return
        
        user_info = validated["user"]
        telegram_id = user_info["id"]
        db = get_db()
        user = get_or_create_user(db, telegram_id, user_info)
        
        # ── Сохранить результат рулетки ──
        if action == "save_roll":
            if user["state"] != "new":
                self._send(json_response({"error": "Already rolled"}, 400))
                return
            
            db.table("users").update({
                "state": "rolled",
                "prize_key": body.get("prize_key", ""),
                "prize_name": body.get("prize_name", ""),
                "rolled_at": "now()",
            }).eq("telegram_id", telegram_id).execute()
            
            self._send(json_response({"ok": True, "state": "rolled"}))
            return
        
        # ── Проверить подписки ──
        if action == "check":
            channels = get_channels(db)
            results = {}
            all_ok = True
            
            for ch in channels:
                is_member = check_membership(ch["channel_id"], telegram_id)
                results[str(ch["channel_id"])] = is_member
                if not is_member:
                    all_ok = False
            
            new_state = user["state"]
            if all_ok and user["state"] == "rolled":
                db.table("users").update({
                    "state": "claimed",
                    "claimed_at": "now()",
                }).eq("telegram_id", telegram_id).execute()
                new_state = "claimed"
            
            self._send(json_response({
                "ok": True,
                "all_subscribed": all_ok,
                "results": results,
                "state": new_state,
            }))
            return
        
        self._send(json_response({"error": "Unknown action"}, 400))
    
    def _send(self, resp):
        self.send_response(resp["statusCode"])
        for k, v in resp["headers"].items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp["body"].encode())