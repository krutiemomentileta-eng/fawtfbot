import os
import json
import hmac
import hashlib
import base64
from urllib.parse import parse_qs, unquote
from urllib.request import Request, urlopen
from supabase import create_client

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

# ─── Supabase ───
_db = None
def get_db():
    global _db
    if not _db:
        _db = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"]
        )
    return _db

# ─── Telegram API ───
def tg_api(method, data=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if data:
        req = Request(url, data=json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    else:
        req = Request(url)
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"TG API error [{method}]: {e}")
        return {"ok": False}

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_api("sendMessage", data)

def edit_message(chat_id, message_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": message_id,
            "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg_api("editMessageText", data)

def answer_callback(callback_id, text="", show_alert=False):
    return tg_api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text, "show_alert": show_alert
    })

# ─── Парсинг канала ───
def parse_channel_info(channel_input):
    """
    Парсит канал по @username, ID или ссылке.
    Возвращает dict с инфой или None.
    """
    # Чистим ввод
    channel_input = channel_input.strip()
    
    # Если это ссылка t.me/xxx
    if "t.me/" in channel_input:
        parts = channel_input.split("t.me/")[-1].split("/")[0].split("?")[0]
        channel_input = "@" + parts
    
    # Если нет @ и не число — добавляем @
    if not channel_input.startswith("@") and not channel_input.lstrip("-").isdigit():
        channel_input = "@" + channel_input
    
    # Запрашиваем инфо у Telegram
    result = tg_api("getChat", {"chat_id": channel_input})
    
    if not result.get("ok"):
        return None
    
    chat = result["result"]
    
    if chat.get("type") not in ("channel", "supergroup"):
        return None
    
    info = {
        "channel_id": chat["id"],
        "title": chat.get("title", ""),
        "username": chat.get("username", ""),
        "invite_link": "",
        "avatar_base64": "",
        "member_count": 0,
    }
    
    # Ссылка
    if info["username"]:
        info["invite_link"] = f"https://t.me/{info['username']}"
    elif chat.get("invite_link"):
        info["invite_link"] = chat["invite_link"]
    else:
        # Пробуем получить invite link
        r = tg_api("exportChatInviteLink", {"chat_id": chat["id"]})
        if r.get("ok"):
            info["invite_link"] = r["result"]
    
    # Количество участников
    r = tg_api("getChatMemberCount", {"chat_id": chat["id"]})
    if r.get("ok"):
        info["member_count"] = r["result"]
    
    # Аватарка
    if chat.get("photo"):
        file_id = chat["photo"].get("big_file_id") or chat["photo"].get("small_file_id")
        if file_id:
            info["avatar_base64"] = download_file_base64(file_id)
    
    return info

def download_file_base64(file_id):
    """Скачивает файл из Telegram и возвращает data:URL"""
    try:
        r = tg_api("getFile", {"file_id": file_id})
        if not r.get("ok"):
            return ""
        
        file_path = r["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        with urlopen(file_url) as resp:
            file_data = resp.read()
        
        b64 = base64.b64encode(file_data).decode()
        
        # Определяем тип
        if file_path.endswith(".png"):
            mime = "image/png"
        else:
            mime = "image/jpeg"
        
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"Download file error: {e}")
        return ""

# ─── Проверка подписки ───
def check_membership(channel_id, user_id):
    r = tg_api("getChatMember", {"chat_id": channel_id, "user_id": user_id})
    if r.get("ok"):
        status = r["result"]["status"]
        return status in ("member", "administrator", "creator")
    return False

# ─── Валидация initData ───
def validate_init_data(init_data_raw, bot_token):
    try:
        parsed = parse_qs(init_data_raw)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            return None
        
        pairs = []
        for key, values in parsed.items():
            if key == "hash":
                continue
            pairs.append(f"{key}={unquote(values[0])}")
        pairs.sort()
        check_string = "\n".join(pairs)
        
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        
        if computed != received_hash:
            return None
        
        user_raw = parsed.get("user", [None])[0]
        user = json.loads(unquote(user_raw)) if user_raw else None
        return {"user": user}
    except Exception as e:
        print(f"Validate error: {e}")
        return None

# ─── DB helpers ───
def get_or_create_user(db, telegram_id, user_info=None):
    r = db.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if r.data:
        return r.data[0]
    
    new_user = {
        "telegram_id": telegram_id,
        "username": (user_info or {}).get("username", ""),
        "first_name": (user_info or {}).get("first_name", ""),
        "last_name": (user_info or {}).get("last_name", ""),
        "state": "new",
    }
    r = db.table("users").insert(new_user).execute()
    return r.data[0] if r.data else new_user

def get_channels(db):
    r = db.table("channels").select("*").eq("is_active", True).order("added_at").execute()
    return r.data or []

def get_prizes(db):
    r = db.table("prizes").select("*").eq("is_active", True).order("sort_order").execute()
    return r.data or []

def json_response(data, status=200):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(data, ensure_ascii=False)
    }