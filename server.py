import os
import json
import hmac
import hashlib
import base64
import re
import asyncio
from urllib.parse import parse_qs, unquote
from datetime import datetime, timezone

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
#  SUPABASE REST
# ══════════════════════════════════════════════════
class SupabaseREST:
    def __init__(self, url, key):
        self.base = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _req(self, method, table, params=None, data=None):
        url = f"{self.base}/{table}"
        with httpx.Client() as c:
            r = c.request(method, url, params=params, json=data,
                          headers=self.headers, timeout=15)
            if r.status_code >= 400:
                print(f"DB error: {r.status_code} {r.text}")
                return []
            try:
                return r.json()
            except:
                return []

    def select(self, table, filters=None, order=None, limit=None):
        p = {"select": "*"}
        if filters: p.update(filters)
        if order: p["order"] = order
        if limit: p["limit"] = str(limit)
        return self._req("GET", table, params=p)

    def insert(self, table, data):
        return self._req("POST", table, data=data)

    def update(self, table, data, filters):
        return self._req("PATCH", table, params=filters, data=data)

    def select_eq(self, table, col, val):
        return self.select(table, {col: f"eq.{val}"})

    def update_eq(self, table, data, col, val):
        return self.update(table, data, {col: f"eq.{val}"})


db = SupabaseREST(SUPABASE_URL, SUPABASE_KEY)


# ══════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════
def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except:
        return None


# ══════════════════════════════════════════════════
#  TELEGRAM API
# ══════════════════════════════════════════════════
async def tg(method, data=None):
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                             json=data or {}, timeout=15)
            return r.json()
        except Exception as e:
            print(f"TG error [{method}]: {e}")
            return {"ok": False}


async def send_msg(chat_id, text, markup=None):
    d = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        d["reply_markup"] = markup
    return await tg("sendMessage", d)


async def edit_msg(chat_id, msg_id, text, markup=None):
    d = {"chat_id": chat_id, "message_id": msg_id,
         "text": text, "parse_mode": "HTML"}
    if markup:
        d["reply_markup"] = markup
    return await tg("editMessageText", d)


async def answer_cb(cb_id, text="", alert=False):
    return await tg("answerCallbackQuery",
                    {"callback_query_id": cb_id, "text": text, "show_alert": alert})


# ══════════════════════════════════════════════════
#  PARSERS
# ══════════════════════════════════════════════════
async def download_file_b64(file_id):
    try:
        r = await tg("getFile", {"file_id": file_id})
        if not r.get("ok"):
            return ""
        path = r["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
        async with httpx.AsyncClient() as c:
            resp = await c.get(url)
            b64 = base64.b64encode(resp.content).decode()
            mime = "image/png" if path.endswith(".png") else "image/jpeg"
            return f"data:{mime};base64,{b64}"
    except:
        return ""


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
        "channel_id": chat["id"], "type": "channel",
        "title": chat.get("title", ""), "username": chat.get("username", ""),
        "invite_link": "", "avatar_base64": "", "member_count": 0,
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


async def parse_bot(bot_input):
    bot_input = bot_input.strip()
    if "t.me/" in bot_input:
        bot_input = bot_input.split("t.me/")[-1].split("/")[0].split("?")[0]
    bot_input = bot_input.lstrip("@")
    if not bot_input:
        return None

    r = await tg("getChat", {"chat_id": f"@{bot_input}"})
    if r.get("ok"):
        chat = r["result"]
        avatar = ""
        if chat.get("photo"):
            fid = chat["photo"].get("big_file_id") or chat["photo"].get("small_file_id")
            if fid:
                avatar = await download_file_b64(fid)
        return {
            "channel_id": chat["id"], "type": "bot",
            "title": chat.get("first_name") or chat.get("title") or bot_input,
            "username": chat.get("username", bot_input),
            "invite_link": f"https://t.me/{chat.get('username', bot_input)}",
            "avatar_base64": avatar, "member_count": 0,
        }

    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            resp = await c.get(f"https://t.me/{bot_input}",
                               headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            html = resp.text
            m_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            title = m_title.group(1) if m_title else bot_input
            avatar = ""
            m_img = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
            if m_img:
                img_url = m_img.group(1)
                if img_url and "telegram-logo" not in img_url and "telegram_logo" not in img_url:
                    try:
                        img_resp = await c.get(img_url, timeout=10)
                        if img_resp.status_code == 200 and len(img_resp.content) > 100:
                            b64 = base64.b64encode(img_resp.content).decode()
                            mime = "image/png" if img_url.endswith(".png") else "image/jpeg"
                            avatar = f"data:{mime};base64,{b64}"
                    except:
                        pass
            uid = int(hashlib.md5(bot_input.encode()).hexdigest()[:15], 16)
            return {
                "channel_id": uid, "type": "bot", "title": title,
                "username": bot_input, "invite_link": f"https://t.me/{bot_input}",
                "avatar_base64": avatar, "member_count": 0,
            }
    except Exception as e:
        print(f"parse_bot error: {e}")
        uid = int(hashlib.md5(bot_input.encode()).hexdigest()[:15], 16)
        return {
            "channel_id": uid, "type": "bot", "title": bot_input,
            "username": bot_input, "invite_link": f"https://t.me/{bot_input}",
            "avatar_base64": "", "member_count": 0,
        }


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
        pairs = sorted(f"{k}={unquote(v[0])}" for k, v in parsed.items() if k != "hash")
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


def get_sponsors():
    return db.select("channels", {"is_active": "eq.true"}, order="added_at.asc")


def get_prizes():
    return db.select("prizes", {"is_active": "eq.true"}, order="sort_order.asc")


def count_referrals(tg_id):
    refs = db.select("users", {"referred_by": f"eq.{tg_id}", "state": "eq.claimed"})
    return len(refs)


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
    sponsors = get_sponsors()
    prizes = get_prizes()
    ref_count = count_referrals(user["telegram_id"])

    sponsors.sort(key=lambda x: (0 if x.get("type", "channel") == "channel" else 1))

    claimed_at = user.get("claimed_at")
    claimed_at_unix = None
    if claimed_at:
        dt = parse_dt(claimed_at)
        if dt:
            claimed_at_unix = int(dt.timestamp())

    return {
        "ok": True,
        "user": {
            "telegram_id": user["telegram_id"],
            "first_name": user.get("first_name", ""),
            "state": user["state"],
            "prize_key": user.get("prize_key"),
            "prize_name": user.get("prize_name"),
            "claimed_at": claimed_at_unix,
            "referral_count": ref_count,
            "is_admin": user["telegram_id"] == ADMIN_ID,
        },
        "channels": [
            {
                "id": str(c["channel_id"]),
                "name": c["title"],
                "type": c.get("type", "channel"),
                "link": c["invite_link"] if c["invite_link"].startswith("http")
                        else f"https://t.me/{c['username']}" if c.get("username")
                        else c["invite_link"],
                "avatar": c.get("avatar_base64", ""),
            }
            for c in sponsors
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

    if action == "mark_bot_opened":
        bot_id = body.get("bot_id")
        if bot_id:
            bot_id_str = str(bot_id)
            fresh = db.select_eq("users", "telegram_id", tg_id)
            opened = json.loads(fresh[0].get("opened_bots") or "[]") if fresh else []
            if bot_id_str not in opened:
                opened.append(bot_id_str)
                db.update_eq("users", {"opened_bots": json.dumps(opened)},
                             "telegram_id", tg_id)
        return {"ok": True}

    if action == "check":
        sponsors = get_sponsors()
        fresh = db.select_eq("users", "telegram_id", tg_id)
        fresh_user = fresh[0] if fresh else user
        opened_bots = json.loads(fresh_user.get("opened_bots") or "[]")

        results = {}
        all_ok = True
        for sp in sponsors:
            sp_type = sp.get("type", "channel")
            sp_id = str(sp["channel_id"])
            if sp_type == "bot":
                ok = sp_id in opened_bots
            else:
                ok = await check_member(sp["channel_id"], tg_id)
            results[sp_id] = ok
            if not ok:
                all_ok = False

        new_state = fresh_user["state"]
        if all_ok and fresh_user["state"] == "rolled":
            now_iso = datetime.now(timezone.utc).isoformat()
            db.update_eq("users", {
                "state": "claimed",
                "claimed_at": now_iso,
            }, "telegram_id", tg_id)
            new_state = "claimed"

            # Уведомляем реферера
            referred_by = fresh_user.get("referred_by")
            if referred_by:
                asyncio.create_task(notify_referrer(int(referred_by)))

        ref_count = count_referrals(tg_id)
        return {
            "ok": True, "all_subscribed": all_ok,
            "results": results, "state": new_state,
            "referral_count": ref_count,
        }

    return JSONResponse({"error": "Unknown action"}, 400)


async def notify_referrer(referrer_id):
    """Уведомление реферера о новом друге"""
    try:
        ref_count = count_referrals(referrer_id)
        speed = 2 ** (ref_count // 2)
        await send_msg(referrer_id,
            f"🎉 <b>Ваш друг получил подарок!</b>\n\n"
            f"👥 Приглашено друзей: <b>{ref_count}</b>\n"
            f"🚀 Скорость вывода: <b>x{speed}</b>\n\n"
            f"Продолжайте приглашать для ускорения! 🔥",
            {"inline_keyboard": [[
                {"text": "🎁 Открыть", "web_app": {"url": WEBAPP_URL}}
            ]]}
        )
    except Exception as e:
        print(f"notify_referrer error: {e}")


# ── PAGES API ──
@app.post("/api/get-page")
async def api_get_page(req: Request):
    body = await req.json()
    key = body.get("key", "")
    rows = db.select_eq("pages", "key", key)
    return {"ok": True, "content": rows[0]["content"] if rows else ""}


@app.post("/api/save-page")
async def api_save_page(req: Request):
    body = await req.json()
    v = validate_init(body.get("initData", ""))
    if not v or v["user"]["id"] != ADMIN_ID:
        return JSONResponse({"error": "Forbidden"}, 403)
    key = body.get("key", "")
    content = body.get("content", "")
    existing = db.select_eq("pages", "key", key)
    if existing:
        db.update_eq("pages", {"content": content}, "key", key)
    else:
        db.insert("pages", {"key": key, "content": content})
    return {"ok": True}


# ══════════════════════════════════════════════════
#  WEBHOOK
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

    if text.startswith("/start"):
        parts = text.split()
        ref_id = None
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                ref_id = int(parts[1][4:])
            except:
                pass

        name = msg["from"].get("first_name", "Друг")
        user = get_or_create(uid, msg["from"])

        # Сохраняем реферала (только для новых)
        if ref_id and ref_id != uid and not user.get("referred_by"):
            db.update_eq("users", {"referred_by": ref_id}, "telegram_id", uid)

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
        elif st == "add_bot":
            await process_add_bot(cid, text)
            db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
        elif st and st.startswith("edit_prize:"):
            key = st.split(":")[1]
            db.update_eq("prizes", {"name": text}, "key", key)
            db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
            await send_msg(cid, f"✅ Приз переименован в: <b>{text}</b>")
        elif st == "broadcast_text":
            db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
            await start_broadcast(cid, msg)
        elif st == "broadcast_confirm":
            db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
            await send_msg(cid, "Отменено. /a")


# ══════════════════════════════════════════════════
#  ADMIN MENU
# ══════════════════════════════════════════════════
async def show_admin_menu(cid, msg_id=None):
    sponsors = get_sponsors()
    prs = get_prizes()
    users = db.select("users")
    total = len(users)
    ch_count = sum(1 for s in sponsors if s.get("type", "channel") == "channel")
    bot_count = sum(1 for s in sponsors if s.get("type") == "bot")

    text = (
        f"⚙️ <b>Панель администратора</b>\n\n"
        f"📢 Каналов: <b>{ch_count}</b>\n"
        f"🤖 Ботов: <b>{bot_count}</b>\n"
        f"🎁 Призов: <b>{len(prs)}</b>\n"
        f"👥 Пользователей: <b>{total}</b>"
    )
    kb = {"inline_keyboard": [
        [{"text": f"📢 Каналы ({ch_count})", "callback_data": "adm_channels"}],
        [{"text": f"🤖 Боты ({bot_count})", "callback_data": "adm_bots"}],
        [{"text": f"🎁 Призы ({len(prs)})", "callback_data": "adm_prizes"}],
        [{"text": "📨 Рассылка", "callback_data": "adm_broadcast"}],
        [{"text": "📊 Статистика", "callback_data": "adm_stats"}],
        [{"text": "🔄 Обновить данные", "callback_data": "adm_refresh"}],
    ]}
    if msg_id:
        await edit_msg(cid, msg_id, text, kb)
    else:
        await send_msg(cid, text, kb)


async def process_add_channel(cid, text):
    await send_msg(cid, "⏳ Проверяю канал...")
    info = await parse_channel(text)
    if not info:
        await send_msg(cid, "❌ Не удалось найти канал.\nОтправьте @username ещё раз:")
        db.update_eq("users", {"admin_state": "add_channel"}, "telegram_id", ADMIN_ID)
        return
    bot_info = await tg("getMe")
    bot_id = bot_info["result"]["id"] if bot_info.get("ok") else 0
    bm = await tg("getChatMember", {"chat_id": info["channel_id"], "user_id": bot_id})
    if not bm.get("ok") or bm["result"]["status"] not in ("administrator", "creator"):
        await send_msg(cid, f"⚠️ Бот не админ в «{info['title']}».")
        return
    existing = db.select_eq("channels", "channel_id", info["channel_id"])
    if existing:
        db.update_eq("channels", {**info, "is_active": True}, "channel_id", info["channel_id"])
    else:
        db.insert("channels", info)
    await send_msg(cid, f"✅ Канал <b>{info['title']}</b> добавлен!")


async def process_add_bot(cid, text):
    await send_msg(cid, "⏳ Проверяю бота...")
    info = await parse_bot(text)
    if not info:
        await send_msg(cid, "❌ Не удалось найти бота.\nОтправьте @username ещё раз:")
        db.update_eq("users", {"admin_state": "add_bot"}, "telegram_id", ADMIN_ID)
        return
    existing = db.select_eq("channels", "channel_id", info["channel_id"])
    if existing:
        db.update_eq("channels", {**info, "is_active": True}, "channel_id", info["channel_id"])
    else:
        db.insert("channels", info)
    await send_msg(cid, f"✅ Бот <b>{info['title']}</b> добавлен!")


# ══════════════════════════════════════════════════
#  BROADCAST
# ══════════════════════════════════════════════════
broadcast_status = {"running": False, "total": 0, "sent": 0, "failed": 0, "blocked": 0}


async def start_broadcast(admin_cid, msg):
    content = {}
    if msg.get("photo"):
        content = {"type": "photo", "file_id": msg["photo"][-1]["file_id"],
                   "caption": msg.get("caption", ""),
                   "caption_entities": msg.get("caption_entities", [])}
    elif msg.get("video"):
        content = {"type": "video", "file_id": msg["video"]["file_id"],
                   "caption": msg.get("caption", ""),
                   "caption_entities": msg.get("caption_entities", [])}
    elif msg.get("animation"):
        content = {"type": "animation", "file_id": msg["animation"]["file_id"],
                   "caption": msg.get("caption", ""),
                   "caption_entities": msg.get("caption_entities", [])}
    elif msg.get("sticker"):
        content = {"type": "sticker", "file_id": msg["sticker"]["file_id"]}
    elif msg.get("document"):
        content = {"type": "document", "file_id": msg["document"]["file_id"],
                   "caption": msg.get("caption", ""),
                   "caption_entities": msg.get("caption_entities", [])}
    elif msg.get("voice"):
        content = {"type": "voice", "file_id": msg["voice"]["file_id"],
                   "caption": msg.get("caption", "")}
    elif msg.get("video_note"):
        content = {"type": "video_note", "file_id": msg["video_note"]["file_id"]}
    else:
        content = {"type": "text", "text": msg.get("text", ""),
                   "entities": msg.get("entities", [])}

    db.update_eq("users", {
        "admin_state": "broadcast_confirm",
        "broadcast_data": json.dumps(content),
    }, "telegram_id", ADMIN_ID)

    total = len(db.select("users"))
    preview = content.get("text", content.get("caption", ""))[:200]
    await send_msg(admin_cid,
        f"📨 <b>Подтверждение рассылки</b>\n\n"
        f"📝 Тип: <b>{content['type']}</b>\n"
        f"{'📄 ' + preview if preview else ''}\n\n"
        f"👥 Получателей: <b>{total}</b>",
        {"inline_keyboard": [
            [{"text": "✅ Отправить", "callback_data": "adm_broadcast_go"},
             {"text": "❌ Отмена", "callback_data": "adm_broadcast_cancel"}],
            [{"text": "📨 Тест", "callback_data": "adm_broadcast_test"}],
        ]}
    )


async def send_broadcast_msg(chat_id, content):
    t = content["type"]
    if t == "text":
        r = await tg("sendMessage", {"chat_id": chat_id, "text": content["text"],
                                      "entities": content.get("entities", [])})
    elif t == "photo":
        r = await tg("sendPhoto", {"chat_id": chat_id, "photo": content["file_id"],
                                    "caption": content.get("caption", ""),
                                    "caption_entities": content.get("caption_entities", [])})
    elif t == "video":
        r = await tg("sendVideo", {"chat_id": chat_id, "video": content["file_id"],
                                    "caption": content.get("caption", ""),
                                    "caption_entities": content.get("caption_entities", [])})
    elif t == "animation":
        r = await tg("sendAnimation", {"chat_id": chat_id, "animation": content["file_id"],
                                        "caption": content.get("caption", ""),
                                        "caption_entities": content.get("caption_entities", [])})
    elif t == "sticker":
        r = await tg("sendSticker", {"chat_id": chat_id, "sticker": content["file_id"]})
    elif t == "document":
        r = await tg("sendDocument", {"chat_id": chat_id, "document": content["file_id"],
                                       "caption": content.get("caption", ""),
                                       "caption_entities": content.get("caption_entities", [])})
    elif t == "voice":
        r = await tg("sendVoice", {"chat_id": chat_id, "voice": content["file_id"],
                                    "caption": content.get("caption", "")})
    elif t == "video_note":
        r = await tg("sendVideoNote", {"chat_id": chat_id, "video_note": content["file_id"]})
    else:
        return False

    if not r.get("ok"):
        desc = r.get("description", "")
        if "blocked" in desc or "deactivated" in desc or "not found" in desc:
            return False
        return False
    return True


async def do_broadcast(admin_cid):
    global broadcast_status
    if broadcast_status["running"]:
        await send_msg(admin_cid, "⚠️ Рассылка уже идёт!")
        return
    admin = db.select_eq("users", "telegram_id", ADMIN_ID)
    if not admin:
        return
    content = json.loads(admin[0].get("broadcast_data") or "{}")
    if not content:
        return

    users = db.select("users")
    broadcast_status = {"running": True, "total": len(users), "sent": 0, "failed": 0, "blocked": 0}

    sm = await send_msg(admin_cid,
        f"🚀 Рассылка запущена!\n👥 {broadcast_status['total']}")
    sm_id = sm.get("result", {}).get("message_id") if sm.get("ok") else None

    for i, u in enumerate(users):
        try:
            ok = await send_broadcast_msg(u["telegram_id"], content)
            if ok:
                broadcast_status["sent"] += 1
            else:
                broadcast_status["blocked"] += 1
        except:
            broadcast_status["failed"] += 1

        if sm_id and (i + 1) % 25 == 0:
            await edit_msg(admin_cid, sm_id,
                f"🚀 Рассылка...\n✅ {broadcast_status['sent']}\n"
                f"🚫 {broadcast_status['blocked']}\n📊 {i+1}/{broadcast_status['total']}")
        await asyncio.sleep(0.05)

    broadcast_status["running"] = False
    final = (f"✅ <b>Рассылка завершена!</b>\n\n"
             f"✅ {broadcast_status['sent']}  ❌ {broadcast_status['failed']}  "
             f"🚫 {broadcast_status['blocked']}")
    if sm_id:
        await edit_msg(admin_cid, sm_id, final)
    else:
        await send_msg(admin_cid, final)
    db.update_eq("users", {"admin_state": "", "broadcast_data": ""}, "telegram_id", ADMIN_ID)


# ══════════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════════
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
        sponsors = get_sponsors()
        chs = [s for s in sponsors if s.get("type", "channel") == "channel"]
        text = "📢 <b>Каналы:</b>\n\n"
        if not chs:
            text += "Пусто."
        for i, c in enumerate(chs, 1):
            text += f"{i}. <b>{c['title']}</b> 👥{c.get('member_count',0)}\n"
        btns = [[{"text": f"❌ {c['title'][:20]}", "callback_data": f"adm_del_sp:{c['channel_id']}"}] for c in chs]
        btns.append([{"text": "➕ Добавить", "callback_data": "adm_add_ch"}])
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data == "adm_add_ch":
        db.update_eq("users", {"admin_state": "add_channel"}, "telegram_id", ADMIN_ID)
        await edit_msg(cid, mid, "📢 Отправьте @username канала\n⚠️ Бот должен быть админом!",
                       {"inline_keyboard": [[{"text": "← Отмена", "callback_data": "adm_channels"}]]})

    elif data == "adm_bots":
        sponsors = get_sponsors()
        bots = [s for s in sponsors if s.get("type") == "bot"]
        text = "🤖 <b>Боты:</b>\n\n"
        if not bots:
            text += "Пусто."
        for i, b in enumerate(bots, 1):
            text += f"{i}. <b>{b['title']}</b>\n"
        btns = [[{"text": f"❌ {b['title'][:20]}", "callback_data": f"adm_del_sp:{b['channel_id']}"}] for b in bots]
        btns.append([{"text": "➕ Добавить", "callback_data": "adm_add_bot"}])
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data == "adm_add_bot":
        db.update_eq("users", {"admin_state": "add_bot"}, "telegram_id", ADMIN_ID)
        await edit_msg(cid, mid, "🤖 Отправьте @username бота:",
                       {"inline_keyboard": [[{"text": "← Отмена", "callback_data": "adm_bots"}]]})

    elif data.startswith("adm_del_sp:"):
        sp_id = data.split(":")[1]
        items = db.select_eq("channels", "channel_id", sp_id)
        sp_type = items[0].get("type", "channel") if items else "channel"
        db.update_eq("channels", {"is_active": False}, "channel_id", sp_id)
        if sp_type == "bot":
            # Trigger bots list refresh
            sponsors = get_sponsors()
            bots = [s for s in sponsors if s.get("type") == "bot"]
            text = "🤖 <b>Боты:</b>\n\n"
            for i, b in enumerate(bots, 1): text += f"{i}. <b>{b['title']}</b>\n"
            if not bots: text += "Пусто."
            btns = [[{"text": f"❌ {b['title'][:20]}", "callback_data": f"adm_del_sp:{b['channel_id']}"}] for b in bots]
            btns.append([{"text": "➕ Добавить", "callback_data": "adm_add_bot"}])
            btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        else:
            sponsors = get_sponsors()
            chs = [s for s in sponsors if s.get("type", "channel") == "channel"]
            text = "📢 <b>Каналы:</b>\n\n"
            for i, c in enumerate(chs, 1): text += f"{i}. <b>{c['title']}</b>\n"
            if not chs: text += "Пусто."
            btns = [[{"text": f"❌ {c['title'][:20]}", "callback_data": f"adm_del_sp:{c['channel_id']}"}] for c in chs]
            btns.append([{"text": "➕ Добавить", "callback_data": "adm_add_ch"}])
            btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data == "adm_prizes":
        prs = db.select("prizes", order="sort_order.asc")
        text = "🎁 <b>Призы:</b>\n\n"
        for p in prs:
            s = "✅" if p["is_active"] else "❌"
            text += f"{s} {p['emoji']} <b>{p['name']}</b>\n"
        btns = [[
            {"text": f"✏️ {p['name'][:15]}", "callback_data": f"adm_edit_pr:{p['key']}"},
            {"text": "🟢" if p["is_active"] else "🔴", "callback_data": f"adm_toggle_pr:{p['key']}"},
        ] for p in prs]
        btns.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        await edit_msg(cid, mid, text, {"inline_keyboard": btns})

    elif data.startswith("adm_edit_pr:"):
        key = data.split(":")[1]
        db.update_eq("users", {"admin_state": f"edit_prize:{key}"}, "telegram_id", ADMIN_ID)
        p = db.select_eq("prizes", "key", key)
        name = p[0]["name"] if p else key
        await edit_msg(cid, mid, f"✏️ Текущее: <b>{name}</b>\nОтправьте новое название:",
                       {"inline_keyboard": [[{"text": "← Отмена", "callback_data": "adm_prizes"}]]})

    elif data.startswith("adm_toggle_pr:"):
        key = data.split(":")[1]
        p = db.select_eq("prizes", "key", key)
        if p:
            db.update_eq("prizes", {"is_active": not p[0]["is_active"]}, "key", key)
        prs = db.select("prizes", order="sort_order.asc")
        text = "🎁 <b>Призы:</b>\n\n"
        for p in prs:
            s = "✅" if p["is_active"] else "❌"
            text += f"{s} {p['emoji']} <b>{p['name']}</b>\n"
        btns = [[
            {"text": f"✏️ {p['name'][:15]}", "callback_data": f"adm_edit_pr:{p['key']}"},
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
        with_ref = sum(1 for u in users if u.get("referred_by"))
        recent = db.select("users", order="created_at.desc", limit=5)

        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего: <b>{total}</b>\n"
            f"🆕 Новые: <b>{new}</b>\n"
            f"🎰 Крутили: <b>{rolled}</b>\n"
            f"✅ Подписались: <b>{claimed}</b>\n"
            f"🔗 По рефералам: <b>{with_ref}</b>\n\n"
            f"📈 Конверсия: <b>{round(((rolled+claimed)/total)*100) if total else 0}%</b> крутили → "
            f"<b>{round((claimed/total)*100) if total else 0}%</b> подписались\n"
        )
        if recent:
            text += "\n👤 <b>Последние:</b>\n"
            for u in recent:
                n = u.get("first_name") or u.get("username") or str(u["telegram_id"])
                text += f"  • {n} — {u['state']}"
                if u.get("prize_name"): text += f" ({u['prize_name']})"
                if u.get("referred_by"): text += " 🔗"
                text += "\n"
        await edit_msg(cid, mid, text,
                       {"inline_keyboard": [[{"text": "← Назад", "callback_data": "adm_menu"}]]})

    elif data == "adm_refresh":
        sponsors = get_sponsors()
        for s in sponsors:
            if s.get("type") == "bot":
                info = await parse_bot(f"@{s['username']}" if s.get("username") else str(s["channel_id"]))
            else:
                info = await parse_channel(str(s["channel_id"]))
            if info:
                db.update_eq("channels", {
                    "title": info["title"], "username": info["username"],
                    "invite_link": info["invite_link"], "avatar_base64": info["avatar_base64"],
                    "member_count": info.get("member_count", 0),
                }, "channel_id", s["channel_id"])
        await show_admin_menu(cid, mid)

    elif data == "adm_broadcast":
        if broadcast_status["running"]:
            await edit_msg(cid, mid, f"⏳ Рассылка идёт! ✅ {broadcast_status['sent']}/{broadcast_status['total']}",
                           {"inline_keyboard": [[{"text": "← Назад", "callback_data": "adm_menu"}]]})
            return
        db.update_eq("users", {"admin_state": "broadcast_text"}, "telegram_id", ADMIN_ID)
        await edit_msg(cid, mid,
            "📨 <b>Рассылка</b>\n\nОтправьте сообщение (текст/фото/видео/стикер):",
            {"inline_keyboard": [[{"text": "← Отмена", "callback_data": "adm_menu"}]]})

    elif data == "adm_broadcast_test":
        admin = db.select_eq("users", "telegram_id", ADMIN_ID)
        if admin and admin[0].get("broadcast_data"):
            content = json.loads(admin[0]["broadcast_data"])
            ok = await send_broadcast_msg(ADMIN_ID, content)
            await send_msg(cid, "✅ Тест отправлен!" if ok else "❌ Ошибка",
                {"inline_keyboard": [
                    [{"text": "✅ Всем", "callback_data": "adm_broadcast_go"},
                     {"text": "❌ Отмена", "callback_data": "adm_broadcast_cancel"}],
                ]})

    elif data == "adm_broadcast_go":
        db.update_eq("users", {"admin_state": ""}, "telegram_id", ADMIN_ID)
        asyncio.create_task(do_broadcast(cid))

    elif data == "adm_broadcast_cancel":
        db.update_eq("users", {"admin_state": "", "broadcast_data": ""}, "telegram_id", ADMIN_ID)
        await show_admin_menu(cid, mid)


# ══════════════════════════════════════════════════
#  BACKGROUND TASKS
# ══════════════════════════════════════════════════
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(background_worker())


async def background_worker():
    await asyncio.sleep(60)  # подождать запуска
    while True:
        try:
            await check_reactivation()
        except Exception as e:
            print(f"[bg reactivation] {e}")
        try:
            await check_antifraud()
        except Exception as e:
            print(f"[bg antifraud] {e}")
        await asyncio.sleep(300)  # каждые 5 минут


async def check_reactivation():
    """Через 1 час после /start если не дошёл до конца — пушим"""
    users = db.select("users", {"state": "eq.new", "notified_1h": "eq.false"})
    now = datetime.now(timezone.utc)
    for u in users:
        created = parse_dt(u.get("created_at"))
        if not created or (now - created).total_seconds() < 3600:
            continue
        try:
            await send_msg(u["telegram_id"],
                "🔥 <b>ОСТАЛОСЬ ОЧЕНЬ МАЛО ВРЕМЕНИ ДО КОНЦА РАЗДАЧИ ПОДАРКОВ!</b>\n\n"
                "⏰ Поспеши забрать свой подарок!\n"
                "Не упусти шанс — это бесплатно! 🎁",
                {"inline_keyboard": [[
                    {"text": "🎁 ЗАБРАТЬ ПОДАРОК!", "web_app": {"url": WEBAPP_URL}}
                ]]}
            )
        except:
            pass
        db.update_eq("users", {"notified_1h": True}, "telegram_id", u["telegram_id"])
        await asyncio.sleep(0.1)


async def check_antifraud():
    """Через 24ч проверяем подписки. Если отписался — уведомляем."""
    users = db.select("users", {"state": "eq.claimed", "notified_unsub": "eq.false"})
    sponsors = get_sponsors()
    channels_only = [s for s in sponsors if s.get("type", "channel") == "channel"]

    if not channels_only:
        return

    now = datetime.now(timezone.utc)
    for u in users:
        claimed = parse_dt(u.get("claimed_at"))
        if not claimed or (now - claimed).total_seconds() < 86400:
            continue

        unsubbed = []
        for ch in channels_only:
            ok = await check_member(ch["channel_id"], u["telegram_id"])
            if not ok:
                unsubbed.append(ch["title"])
            await asyncio.sleep(0.05)

        if unsubbed:
            names = "\n".join(f"• {n}" for n in unsubbed)
            try:
                await send_msg(u["telegram_id"],
                    f"❌ <b>Приз не может быть выдан!</b>\n\n"
                    f"Вы отписались от каналов:\n{names}\n\n"
                    f"Подпишитесь обратно для получения приза. 👇",
                    {"inline_keyboard": [[
                        {"text": "📢 Подписаться", "web_app": {"url": WEBAPP_URL}}
                    ]]}
                )
            except:
                pass

        db.update_eq("users", {"notified_unsub": True}, "telegram_id", u["telegram_id"])
        await asyncio.sleep(0.1)


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