import os
import json
import hmac
import hashlib
import base64
from urllib.parse import parse_qs, unquote

import httpx
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
WEBAPP_URL   = os.getenv("WEBAPP_URL", "")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

app = FastAPI()

# ══════════════════════════════════════════════════
#  SUPABASE REST CLIENT (без SDK — без проблем)
# ══════════════════════════════════════════════════
class SupabaseREST:
    """Простой клиент для Supabase через REST API"""

    def __init__(self, url, key):
        self.base = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _req(self, method, table, params=None, data=None, headers_extra=None):
        url = f"{self.base}/{table}"
        h = {**self.headers}
        if headers_extra:
            h.update(headers_extra)
        with httpx.Client() as client:
            r = client.request(method, url, params=params, json=data, headers=h, timeout=15)
            if r.status_code >= 400:
                print(f"Supabase error: {r.status_code} {r.text}")
                return []
            try:
                return r.json()
            except:
                return []

    def select(self, table, filters=None, order=None, limit=None):
        params = {"select": "*"}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)
        return self._req("GET", table, params=params)

    def insert(self, table, data):
        return self._req("POST", table, data=data)

    def update(self, table, data, filters):
        params = {}
        if filters:
            params.update(filters)
        return self._req("PATCH", table, params=params, data=data)

    def select_eq(self, table, column, value):
        return self.select(table, {f"{column}": f"eq.{value}"})

    def update_eq(self, table, data, column, value):
        return self.update(table, data, {f"{column}": f"eq.{value}"})

db = SupabaseREST(SUPABASE_URL, SUPABASE_KEY)

# ══════════════════════════════════════════════════
#  TELEGRAM API HELPERS
# ══════════════════════════════════════════════════
async def tg(method, data=None):
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                json=data or {}, timeout=15
            )
            return r.json()
        except Exception as e:
            print(f"TG API error [{method}]: {e}")
            return {"ok": False}

async def send_msg(chat_id, text, markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        data["reply_markup"] = markup
    return await tg("sendMessage", data)

async def edit_msg(chat_id, msg_id, text, markup=None):
    data = {"chat_id": chat_id, "message_id": msg_id,
            "text": text, "parse_mode": "HTML"}
    if markup:
        data["reply_markup"] = markup
    return await tg("editMessageText", data)

async def answer_cb(cb_id, text="", alert=False):
    return await tg("answerCallbackQuery", {
        "callback_query_id": cb_id, "text": text, "show_alert": alert
    })

# ══════════════════════════════════════════════════
#  CHANNEL PARSER
# ══════════════════════════════════════════════════
async def parse_channel(channel_input):
    channel_input = channel_input.strip()
    if "t.me/" in channel_input:
        channel_input = "@" + channel_input.split("t.me/")[-1].split("/")[0].split("?")[0]
    if not channel_input.startswith("@") and not channel_input.lstrip("-").isdigit():
        channel_input = "@" + channel_input

    r = await tg("getChat", {"chat_id": channel_input})
    if not r.get("ok"):
        return None

    chat = r["result"]
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

    if info["username"]:
        info["invite_link"] = f"https://t.me/{info['username']}"
    elif chat.get("invite_link"):
        info["invite_link"] = chat["invite_link"]
    else:
        r2 = await tg("exportChatInviteLink", {"chat_id": chat["id"]})
        if r2.get("ok"):
            info["invite_link"] = r2["result"]

    r3 = await tg("getChatMemberCount", {"chat_id": chat["id"]})
    if r3.get("ok"):
        info["member_count"] = r3["result"]

    if chat.get("photo"):
        fid = chat["photo"].get("big_file_id") or chat["photo"].get("small_file_id")
        if fid:
            info["avatar_base64"] = await download_file_b64(fid)

    return info

async def download_file_b64(file_id):
    try:
        r = await tg("getFile", {"file_id": file_id})
        if not r.get("ok"):
            return ""
        path = r["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            b64 = base64.b64encode(resp.content).decode()
            mime = "image/png" if path.endswith(".png") else "image/jpeg"
            return f"data:{mime};base64,{b64}"
    except:
        return ""

async def check_member(channel_id, user_id):
    r = await tg("getChatMember", {"chat_id": channel_id, "user_id": user_id})
    if r.get("ok"):
        return r["result"]["status"] in ("member", "administrator", "creator")
    return False

# ══════════════════════════════════════════════════
#  INIT DATA VALIDATION
# ══════════════════════════════════════════════════
def validate_init(raw):
    try:
        parsed = parse_qs(raw)
        h = parsed.get("hash", [None])[0]
        if not h:
            return None
        pairs = sorted(
            f"{k}={unquote(v[0])}" for k, v in parsed.items() if k != "hash"
        )
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        check = hmac.new(secret, "\n".join(pairs).encode(), hashlib.sha256).hexdigest()
        if check != h:
            return None
        user_raw = parsed.get("user", [None])[0]
        return {"user": json.loads(unquote(user_raw))} if user_raw else None
    except:
        return None

# ══════════════════════════════════════════════════
#  DB HELPERS
# ══════════════════════════════════════════════════
def get_or_create(tg_id, info=None):
    rows = db.select_eq("users", "telegram_id", tg_id)
    if rows:
        return rows[0]
    u = {
        "telegram_id": tg_id,
        "username": (info or {}).get("username", ""),
        "first_name": (info or {}).get("first_name", ""),
        "last_name": (info or {}).get("last_name", ""),
        "state": "new",
    }
    result = db.insert("users", u)
    return result[0] if result else u

def get_channels():
    return db.select("channels", {"is_active": "eq.true"}, order="added_at.asc")

def get_prizes():
    return db.select("prizes", {"is_active": "eq.true"}, order="sort_order.asc")

# ══════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════
@app.post("/api/get-user")
async def api_get_user(req: Request):
    body = await req.json()
    v = validate_init(body.get("initData", ""))
    if not v:
        return JSONResponse({"error": "Invalid initData"}, 401)

    user = get_or_create(v["user"]["id"], v["user"])
    channels = get_channels()
    prizes = get_prizes()

    return {
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "first_name": user.get("first_name", ""),
            "state": user["state"],
            "prize_key": user.get("prize_key"),
            "prize_name": user.get("prize_name"),
        },
        "channels": [
            {
                "id": c["channel_id"],
                "name": c["title"],
                "link": c["invite_link"] if c["invite_link"].startswith("http")
                        else f"https://t.me/{c['username']}" if c.get("username")
                        else c["invite_link"],
                "avatar": c["avatar_base64"],
            }
            for c in channels
        ],
        "prizes": [
            {"key": p["key"], "tgs": p["tgs_file"],
             "name": p["name"], "emoji": p["emoji"]}
            for p in prizes
        ],
    }

@app.post("/api/check-subscription")
async def api_check_sub(req: Request):
    body = await req.json()
    v = validate_init(body.get("initData", ""))
    if not v:
        return JSONResponse({"error": "Invalid initData"}, 401)

    tg_id = v["user"]["id"]
    user = get_or_create(tg_id, v["user"])
    action = body.get("action", "check")

    if action == "save_roll":
        if user["state"] != "new":
            return JSONResponse({"error": "Already rolled"}, 400)
        db.update_eq("users", {
            "state": "rolled",
            "prize_key": body.get("prize_key", ""),
            "prize_name": body.get("prize_name", ""),
        }, "telegram_id", tg_id)
        return {"ok": True, "state": "rolled"}

    if action == "check":
        channels = get_channels()
        results = {}
        all_ok = True
        for ch in channels:
            ok = await check_member(ch["channel_id"], tg_id)
            results[str(ch["channel_id"])] = ok
            if not ok:
                all_ok = False

        new_state = user["state"]
        if all_ok and user["state"] == "rolled":
            db.update_eq("users", {"state": "claimed"}, "telegram_id", tg_id)
            new_state = "claimed"

        return {"ok": True, "all_subscribed": all_ok, "results": results, "state": new_state}

    return JSONResponse({"error": "Unknown action"}, 400)

# ══════════════════════════════════════════════════
#  TELEGRAM WEBHOOK
# ══════════════════════════════════════════════════
@app.post("/api/webhook")
async def webhook(req: Request):
    body = await req.json()
    if "message" in body:
        await handle_message(body["message"])
    elif "callback_query" in body:
        await handle_callback(body["callback_query"])
    return {"ok": True}

async def handle_message(msg):
    uid = msg["from"]["id"]
    cid = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    if text == "/start":
        name = msg["from"].get("first_name", "Боец")
        get_or_create(uid, msg["from"])
        await send_msg(cid,
            f"🎖 <b>Привет, {name}!</b>\n\n"
            f"🇷🇺 <b>С наступающим тебя праздником — Днём Защитника Отечества!</b>\n\n"
            f"Сегодня мы подготовили для тебя особенный подарок! 🎁\n\n"
            f"🎰 Крути праздничную рулетку и получи свой приз "
            f"<b>абсолютно бесплатно!</b>\n\n"
            f"Жми на кнопку ниже 👇",
            {"inline_keyboard": [[
                {"text": "🎁 Открыть рулетку!", "web_app": {"url": WEBAPP_URL}}
            ]]}
        )

    elif text == "/a" and uid == ADMIN_ID:
        await show_admin_menu(cid)

    elif uid == ADMIN_ID:
        user = get_or_create(ADMIN_ID)
        st = user.get("admin_state", "")

        if st == "add_channel":
            await process_add_channel(cid, text)
            db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
        elif st and st.startswith("edit_prize:"):
            key = st.split(":")[1]
            db.update_eq("prizes", {"name": text}, "key", key)
            db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
            await send_msg(cid, f"✅ Приз переименован в: <b>{text}</b>")

async def show_admin_menu(cid, msg_id=None):
    chs = get_channels()
    prs = get_prizes()
    users = db.select("users")
    total = len(users)

    text = (
        f"⚙️ <b>Панель администратора</b>\n\n"
        f"📢 Каналов: <b>{len(chs)}</b>\n"
        f"🎁 Призов: <b>{len(prs)}</b>\n"
        f"👥 Пользователей: <b>{total}</b>"
    )
    kb = {"inline_keyboard": [
        [{"text": f"📢 Каналы ({len(chs)})", "callback_data": "adm_channels"}],
        [{"text": f"🎁 Призы ({len(prs)})", "callback_data": "adm_prizes"}],
        [{"text": "📊 Статистика", "callback_data": "adm_stats"}],
        [{"text": "🔄 Обновить каналы", "callback_data": "adm_refresh"}],
    ]}
    if msg_id:
        await edit_msg(cid, msg_id, text, kb)
    else:
        await send_msg(cid, text, kb)

async def process_add_channel(cid, text):
    await send_msg(cid, "⏳ Проверяю канал...")
    info = await parse_channel(text)

    if not info:
        await send_msg(cid,
            "❌ <b>Не удалось найти канал.</b>\n\n"
            "Убедитесь что бот — администратор канала.\n"
            "Отправьте @username или ссылку ещё раз:")
        db.update_eq("users", {"admin_state": "add_channel"}, "telegram_id", ADMIN_ID)
        return

    bot_info = await tg("getMe")
    bot_id = bot_info["result"]["id"] if bot_info.get("ok") else 0
    bm = await tg("getChatMember", {"chat_id": info["channel_id"], "user_id": bot_id})

    if not bm.get("ok") or bm["result"]["status"] not in ("administrator", "creator"):
        await send_msg(cid,
            f"⚠️ Бот не является администратором «{info['title']}».\n"
            "Добавьте бота как админа и попробуйте снова.")
        return

    existing = db.select_eq("channels", "channel_id", info["channel_id"])
    if existing:
        db.update_eq("channels", {
            "title": info["title"], "username": info["username"],
            "invite_link": info["invite_link"], "avatar_base64": info["avatar_base64"],
            "member_count": info["member_count"], "is_active": True,
        }, "channel_id", info["channel_id"])
    else:
        db.insert("channels", info)

    avatar = "🖼" if info["avatar_base64"] else "📢"
    uname = f" (@{info['username']})" if info["username"] else ""
    await send_msg(cid,
        f"✅ <b>Канал добавлен!</b>\n\n"
        f"{avatar} <b>{info['title']}</b>{uname}\n"
        f"🔗 {info['invite_link']}\n"
        f"👥 {info['member_count']} подписчиков")

async def handle_callback(cb):
    uid = cb["from"]["id"]
    data = cb["data"]
    cid = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]

    if uid != ADMIN_ID:
        await answer_cb(cb["id"], "⛔ Нет доступа", True)
        return

    await answer_cb(cb["id"])

    if data == "adm_menu":
        await show_admin_menu(cid, mid)

    elif data == "adm_channels":
        chs = get_channels()
        text = "📢 <b>Каналы-спонсоры:</b>\n\n"
        if not chs:
            text += "Пусто. Добавьте канал."
        for i, c in enumerate(chs, 1):
            av = "🖼" if c["avatar_base64"] else "📢"
            un = f" @{c['username']}" if c["username"] else ""
            text += f"{i}. {av} <b>{c['title']}</b>{un}\n   👥 {c.get('member_count',0)}\n\n"

        btns = [[{"text": f"❌ {c['title'][:20]}", "callback_data": f"adm_del_ch:{c['channel_id']}"}] for c in chs]
        btns.append([{"text": "➕ Добавить канал", "callback_data": "adm_add_ch"}])
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data == "adm_add_ch":
        db.update_eq("users", {"admin_state": "add_channel"}, "telegram_id", ADMIN_ID)
        await edit_msg(cid, mid,
            "📢 <b>Добавление канала</b>\n\n"
            "Отправьте @username канала или ссылку t.me/...\n\n"
            "⚠️ Бот должен быть администратором!",
            {"inline_keyboard": [[{"text": "← Отмена", "callback_data": "adm_channels"}]]})

    elif data.startswith("adm_del_ch:"):
        ch_id = int(data.split(":")[1])
        db.update_eq("channels", {"is_active": False}, "channel_id", ch_id)
        # re-render
        chs = get_channels()
        text = "📢 <b>Каналы-спонсоры:</b>\n\n"
        if not chs:
            text += "Пусто."
        for i, c in enumerate(chs, 1):
            text += f"{i}. <b>{c['title']}</b>\n"
        btns = [[{"text": f"❌ {c['title'][:20]}", "callback_data": f"adm_del_ch:{c['channel_id']}"}] for c in chs]
        btns.append([{"text": "➕ Добавить", "callback_data": "adm_add_ch"}])
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data == "adm_prizes":
        prs = db.select("prizes", order="sort_order.asc")
        text = "🎁 <b>Призы:</b>\n\n"
        for p in prs:
            s = "✅" if p["is_active"] else "❌"
            text += f"{s} {p['emoji']} <b>{p['name']}</b>\n   <code>{p['tgs_file']}</code>\n\n"
        btns = [[
            {"text": f"✏️ {p['name']}", "callback_data": f"adm_edit_pr:{p['key']}"},
            {"text": "🟢" if p["is_active"] else "🔴", "callback_data": f"adm_toggle_pr:{p['key']}"},
        ] for p in prs]
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data.startswith("adm_edit_pr:"):
        key = data.split(":")[1]
        db.update_eq("users", {"admin_state": f"edit_prize:{key}"}, "telegram_id", ADMIN_ID)
        p = db.select_eq("prizes", "key", key)
        name = p[0]["name"] if p else key
        await edit_msg(cid, mid,
            f"✏️ Текущее название: <b>{name}</b>\n\nОтправьте новое:",
            {"inline_keyboard": [[{"text": "← Отмена", "callback_data": "adm_prizes"}]]})

    elif data.startswith("adm_toggle_pr:"):
        key = data.split(":")[1]
        p = db.select_eq("prizes", "key", key)
        if p:
            db.update_eq("prizes", {"is_active": not p[0]["is_active"]}, "key", key)
        # re-render prizes
        prs = db.select("prizes", order="sort_order.asc")
        text = "🎁 <b>Призы:</b>\n\n"
        for p in prs:
            s = "✅" if p["is_active"] else "❌"
            text += f"{s} {p['emoji']} <b>{p['name']}</b>\n\n"
        btns = [[
            {"text": f"✏️ {p['name']}", "callback_data": f"adm_edit_pr:{p['key']}"},
            {"text": "🟢" if p["is_active"] else "🔴", "callback_data": f"adm_toggle_pr:{p['key']}"},
        ] for p in prs]
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data == "adm_stats":
        users = db.select("users")
        total = len(users)
        new = sum(1 for u in users if u["state"] == "new")
        rolled = sum(1 for u in users if u["state"] == "rolled")
        claimed = sum(1 for u in users if u["state"] == "claimed")
        recent = db.select("users", order="created_at.desc", limit=5)

        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего: <b>{total}</b>\n"
            f"🆕 Новые: <b>{new}</b>\n"
            f"🎰 Крутили: <b>{rolled}</b>\n"
            f"✅ Подписались: <b>{claimed}</b>\n\n"
            f"📈 Конверсия: <b>{round(((rolled+claimed)/total)*100) if total else 0}%</b> крутили → "
            f"<b>{round((claimed/total)*100) if total else 0}%</b> подписались\n"
        )
        if recent:
            text += "\n👤 <b>Последние:</b>\n"
            for u in recent:
                n = u.get("first_name") or u.get("username") or str(u["telegram_id"])
                text += f"  • {n} — {u['state']}"
                if u.get("prize_name"):
                    text += f" ({u['prize_name']})"
                text += "\n"

        await edit_msg(cid, mid, text, {"inline_keyboard": [[{"text": "← Назад", "callback_data": "adm_menu"}]]})

    elif data == "adm_refresh":
        chs = get_channels()
        ok = 0
        for c in chs:
            info = await parse_channel(str(c["channel_id"]))
            if info:
                db.update_eq("channels", {
                    "title": info["title"], "username": info["username"],
                    "invite_link": info["invite_link"],
                    "avatar_base64": info["avatar_base64"],
                    "member_count": info["member_count"],
                }, "channel_id", c["channel_id"])
                ok += 1
        await show_admin_menu(cid, mid)

# ══════════════════════════════════════════════════
#  STATIC FILES
# ══════════════════════════════════════════════════
app.mount("/assets", StaticFiles(directory="public/assets"), name="assets")

@app.get("/")
async def root():
    return FileResponse("public/index.html")

@app.get("/{path:path}")
async def catch_all(path: str):
    fp = f"public/{path}"
    if os.path.isfile(fp):
        return FileResponse(fp)
    return FileResponse("public/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)