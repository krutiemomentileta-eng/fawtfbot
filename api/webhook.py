import os
import json
from http.server import BaseHTTPRequestHandler
from _utils import (
    BOT_TOKEN, ADMIN_ID, WEBAPP_URL,
    get_db, tg_api, send_message, edit_message, answer_callback,
    parse_channel_info, download_file_base64,
    get_channels, get_prizes, get_or_create_user
)

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        
        try:
            if "message" in body:
                self.handle_message(body["message"])
            elif "callback_query" in body:
                self.handle_callback(body["callback_query"])
        except Exception as e:
            print(f"Webhook error: {e}")
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
    
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot active")
    
    # ══════════════════════════════════════
    #  MESSAGES
    # ══════════════════════════════════════
    def handle_message(self, msg):
        user_id = msg["from"]["id"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()
        
        if text == "/start":
            self.cmd_start(msg)
        elif text == "/a" and user_id == ADMIN_ID:
            self.cmd_admin(chat_id)
        elif user_id == ADMIN_ID:
            self.handle_admin_input(msg)
    
    # ── /start ──
    def cmd_start(self, msg):
        name = msg["from"].get("first_name", "Боец")
        chat_id = msg["chat"]["id"]
        
        text = (
            f"🎖 <b>Привет, {name}!</b>\n\n"
            f"🇷🇺 <b>С 23 Февраля — Днём Защитника Отечества!</b>\n\n"
            f"Сегодня мы подготовили для тебя особенный подарок! 🎁\n\n"
            f"🎰 Крути праздничную рулетку и получи свой приз "
            f"<b>абсолютно бесплатно!</b>\n\n"
            f"Жми на кнопку ниже 👇"
        )
        
        keyboard = {"inline_keyboard": [[
            {"text": "🎁 Открыть рулетку!", "web_app": {"url": WEBAPP_URL}}
        ]]}
        
        send_message(chat_id, text, keyboard)
        
        # Создаём юзера в БД
        db = get_db()
        get_or_create_user(db, msg["from"]["id"], msg["from"])
    
    # ── /a — Админка ──
    def cmd_admin(self, chat_id):
        db = get_db()
        channels = get_channels(db)
        prizes = get_prizes(db)
        
        # Считаем статистику
        stats = db.table("users").select("state", count="exact").execute()
        total = len(stats.data) if stats.data else 0
        
        text = (
            f"⚙️ <b>Панель администратора</b>\n\n"
            f"📢 Каналов: <b>{len(channels)}</b>\n"
            f"🎁 Призов: <b>{len(prizes)}</b>\n"
            f"👥 Пользователей: <b>{total}</b>"
        )
        
        keyboard = {"inline_keyboard": [
            [{"text": f"📢 Каналы ({len(channels)})", "callback_data": "adm_channels"}],
            [{"text": f"🎁 Призы ({len(prizes)})", "callback_data": "adm_prizes"}],
            [{"text": "📊 Статистика", "callback_data": "adm_stats"}],
            [{"text": "🔄 Обновить данные каналов", "callback_data": "adm_refresh"}],
        ]}
        
        send_message(chat_id, text, keyboard)
    
    # ── Ввод от админа (FSM) ──
    def handle_admin_input(self, msg):
        db = get_db()
        user = get_or_create_user(db, ADMIN_ID)
        admin_state = user.get("admin_state", "")
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()
        
        if not admin_state:
            return
        
        # ── Добавление канала ──
        if admin_state == "add_channel":
            self.process_add_channel(chat_id, text, db)
        
        # ── Редактирование приза ──
        elif admin_state.startswith("edit_prize:"):
            prize_key = admin_state.split(":")[1]
            self.process_edit_prize(chat_id, prize_key, text, db)
        
        # Сбрасываем состояние
        db.table("users").update({"admin_state": ""}).eq("telegram_id", ADMIN_ID).execute()
    
    def process_add_channel(self, chat_id, text, db):
        send_message(chat_id, "⏳ Проверяю канал...")
        
        info = parse_channel_info(text)
        
        if not info:
            send_message(chat_id,
                "❌ <b>Не удалось найти канал.</b>\n\n"
                "Убедитесь что:\n"
                "• Бот добавлен в канал как администратор\n"
                "• Канал существует\n"
                "• Вы отправили верный @username или ссылку\n\n"
                "Попробуйте ещё раз — отправьте @username канала:"
            )
            # Не сбрасываем состояние — пусть попробует снова
            db.table("users").update({"admin_state": "add_channel"}).eq("telegram_id", ADMIN_ID).execute()
            return
        
        # Проверяем что бот — админ канала
        bot_info = tg_api("getMe")
        bot_id = bot_info["result"]["id"] if bot_info.get("ok") else 0
        
        bot_member = tg_api("getChatMember", {
            "chat_id": info["channel_id"], "user_id": bot_id
        })
        
        if not bot_member.get("ok") or bot_member["result"]["status"] not in ("administrator", "creator"):
            send_message(chat_id,
                f"⚠️ <b>Бот не является администратором канала</b> «{info['title']}».\n\n"
                f"Добавьте бота в канал как администратора, затем попробуйте снова."
            )
            return
        
        # Сохраняем в БД
        try:
            # Upsert
            existing = db.table("channels").select("id").eq("channel_id", info["channel_id"]).execute()
            
            if existing.data:
                db.table("channels").update({
                    "title": info["title"],
                    "username": info["username"],
                    "invite_link": info["invite_link"],
                    "avatar_base64": info["avatar_base64"],
                    "member_count": info["member_count"],
                    "is_active": True,
                }).eq("channel_id", info["channel_id"]).execute()
            else:
                db.table("channels").insert(info).execute()
            
            avatar_emoji = "🖼" if info["avatar_base64"] else "📢"
            link = info["invite_link"] or "нет ссылки"
            
            send_message(chat_id,
                f"✅ <b>Канал добавлен!</b>\n\n"
                f"{avatar_emoji} <b>{info['title']}</b>\n"
                f"👤 @{info['username']}\n" if info['username'] else "" +
                f"🔗 {link}\n"
                f"👥 {info['member_count']} подписчиков"
            )
            
            # Показываем обновлённый список
            self.send_channels_list(chat_id, db)
            
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка сохранения: {e}")
    
    def process_edit_prize(self, chat_id, prize_key, new_name, db):
        try:
            db.table("prizes").update({"name": new_name}).eq("key", prize_key).execute()
            send_message(chat_id, f"✅ Приз <b>{prize_key}</b> переименован в: <b>{new_name}</b>")
            self.send_prizes_list(chat_id, db)
        except Exception as e:
            send_message(chat_id, f"❌ Ошибка: {e}")
    
    # ══════════════════════════════════════
    #  CALLBACK QUERIES
    # ══════════════════════════════════════
    def handle_callback(self, cb):
        user_id = cb["from"]["id"]
        data = cb["data"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        
        if user_id != ADMIN_ID:
            answer_callback(cb["id"], "⛔ Нет доступа", True)
            return
        
        answer_callback(cb["id"])
        db = get_db()
        
        # ── Меню ──
        if data == "adm_menu":
            channels = get_channels(db)
            prizes = get_prizes(db)
            r = db.table("users").select("*", count="exact").execute()
            total = len(r.data) if r.data else 0
            
            text = (
                f"⚙️ <b>Панель администратора</b>\n\n"
                f"📢 Каналов: <b>{len(channels)}</b>\n"
                f"🎁 Призов: <b>{len(prizes)}</b>\n"
                f"👥 Пользователей: <b>{total}</b>"
            )
            kb = {"inline_keyboard": [
                [{"text": f"📢 Каналы ({len(channels)})", "callback_data": "adm_channels"}],
                [{"text": f"🎁 Призы ({len(prizes)})", "callback_data": "adm_prizes"}],
                [{"text": "📊 Статистика", "callback_data": "adm_stats"}],
                [{"text": "🔄 Обновить данные каналов", "callback_data": "adm_refresh"}],
            ]}
            edit_message(chat_id, msg_id, text, kb)
        
        # ── Каналы ──
        elif data == "adm_channels":
            self.cb_channels(chat_id, msg_id, db)
        
        elif data == "adm_add_ch":
            db.table("users").update({"admin_state": "add_channel"}).eq("telegram_id", ADMIN_ID).execute()
            edit_message(chat_id, msg_id,
                "📢 <b>Добавление канала</b>\n\n"
                "Отправьте мне одно из:\n"
                "• @username канала\n"
                "• Ссылку https://t.me/channel\n"
                "• ID канала (число)\n\n"
                "⚠️ Бот должен быть администратором канала!",
                {"inline_keyboard": [[
                    {"text": "← Отмена", "callback_data": "adm_channels"}
                ]]}
            )
        
        elif data.startswith("adm_del_ch:"):
            ch_id = int(data.split(":")[1])
            db.table("channels").update({"is_active": False}).eq("channel_id", ch_id).execute()
            answer_callback(cb["id"], "✅ Канал удалён")
            self.cb_channels(chat_id, msg_id, db)
        
        # ── Призы ──
        elif data == "adm_prizes":
            self.cb_prizes(chat_id, msg_id, db)
        
        elif data.startswith("adm_edit_pr:"):
            prize_key = data.split(":")[1]
            db.table("users").update({"admin_state": f"edit_prize:{prize_key}"}).eq("telegram_id", ADMIN_ID).execute()
            prize = db.table("prizes").select("*").eq("key", prize_key).execute()
            name = prize.data[0]["name"] if prize.data else prize_key
            
            edit_message(chat_id, msg_id,
                f"✏️ <b>Редактирование приза</b>\n\n"
                f"Текущее название: <b>{name}</b>\n\n"
                f"Отправьте новое название:",
                {"inline_keyboard": [[
                    {"text": "← Отмена", "callback_data": "adm_prizes"}
                ]]}
            )
        
        elif data.startswith("adm_toggle_pr:"):
            prize_key = data.split(":")[1]
            prize = db.table("prizes").select("*").eq("key", prize_key).execute()
            if prize.data:
                new_active = not prize.data[0]["is_active"]
                db.table("prizes").update({"is_active": new_active}).eq("key", prize_key).execute()
            self.cb_prizes(chat_id, msg_id, db)
        
        # ── Статистика ──
        elif data == "adm_stats":
            self.cb_stats(chat_id, msg_id, db)
        
        # ── Обновить каналы ──
        elif data == "adm_refresh":
            self.cb_refresh(chat_id, msg_id, cb["id"], db)
    
    # ══════════════════════════════════════
    #  CALLBACK RENDERERS
    # ══════════════════════════════════════
    
    def cb_channels(self, chat_id, msg_id, db):
        channels = get_channels(db)
        
        if not channels:
            text = "📢 <b>Каналы</b>\n\nНет добавленных каналов."
        else:
            text = "📢 <b>Каналы-спонсоры:</b>\n\n"
            for i, ch in enumerate(channels, 1):
                title = ch["title"] or "Без названия"
                uname = f"@{ch['username']}" if ch["username"] else ""
                members = ch.get("member_count", 0)
                avatar = "🖼" if ch["avatar_base64"] else "📢"
                text += f"{i}. {avatar} <b>{title}</b> {uname}\n"
                text += f"   👥 {members} подписчиков\n\n"
        
        buttons = []
        for ch in channels:
            title = ch["title"][:20] if ch["title"] else str(ch["channel_id"])
            buttons.append([
                {"text": f"❌ {title}", "callback_data": f"adm_del_ch:{ch['channel_id']}"}
            ])
        
        buttons.append([{"text": "➕ Добавить канал", "callback_data": "adm_add_ch"}])
        buttons.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        
        edit_message(chat_id, msg_id, text, {"inline_keyboard": buttons})
    
    def cb_prizes(self, chat_id, msg_id, db):
        prizes = db.table("prizes").select("*").order("sort_order").execute()
        all_prizes = prizes.data or []
        
        text = "🎁 <b>Призы:</b>\n\n"
        for p in all_prizes:
            status = "✅" if p["is_active"] else "❌"
            text += f"{status} {p['emoji']} <b>{p['name']}</b>\n"
            text += f"   Файл: <code>{p['tgs_file']}</code>\n\n"
        
        buttons = []
        for p in all_prizes:
            status = "🟢" if p["is_active"] else "🔴"
            buttons.append([
                {"text": f"✏️ {p['name']}", "callback_data": f"adm_edit_pr:{p['key']}"},
                {"text": f"{status} Вкл/Выкл", "callback_data": f"adm_toggle_pr:{p['key']}"},
            ])
        
        buttons.append([{"text": "← Назад", "callback_data": "adm_menu"}])
        
        edit_message(chat_id, msg_id, text, {"inline_keyboard": buttons})
    
    def cb_stats(self, chat_id, msg_id, db):
        all_users = db.table("users").select("state").execute()
        users = all_users.data or []
        
        total = len(users)
        new = sum(1 for u in users if u["state"] == "new")
        rolled = sum(1 for u in users if u["state"] == "rolled")
        claimed = sum(1 for u in users if u["state"] == "claimed")
        
        # Последние 5 юзеров
        recent = db.table("users").select("*").order("created_at", desc=True).limit(5).execute()
        
        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего: <b>{total}</b>\n"
            f"🆕 Новые (не крутили): <b>{new}</b>\n"
            f"🎰 Крутили (не подписались): <b>{rolled}</b>\n"
            f"✅ Подписались (заявка): <b>{claimed}</b>\n\n"
            f"📈 Конверсия:\n"
            f"   Крутили: <b>{round(((rolled+claimed)/total)*100) if total else 0}%</b>\n"
            f"   Подписались: <b>{round((claimed/total)*100) if total else 0}%</b>\n"
        )
        
        if recent.data:
            text += "\n👤 <b>Последние пользователи:</b>\n"
            for u in recent.data:
                name = u.get("first_name", "") or u.get("username", "") or str(u["telegram_id"])
                text += f"   • {name} — {u['state']}"
                if u.get("prize_name"):
                    text += f" ({u['prize_name']})"
                text += "\n"
        
        kb = {"inline_keyboard": [[{"text": "← Назад", "callback_data": "adm_menu"}]]}
        edit_message(chat_id, msg_id, text, kb)
    
    def cb_refresh(self, chat_id, msg_id, cb_id, db):
        channels = get_channels(db)
        
        if not channels:
            answer_callback(cb_id, "Нет каналов для обновления", True)
            return
        
        updated = 0
        for ch in channels:
            info = parse_channel_info(str(ch["channel_id"]))
            if info:
                db.table("channels").update({
                    "title": info["title"],
                    "username": info["username"],
                    "invite_link": info["invite_link"],
                    "avatar_base64": info["avatar_base64"],
                    "member_count": info["member_count"],
                }).eq("channel_id", ch["channel_id"]).execute()
                updated += 1
        
        answer_callback(cb_id, f"✅ Обновлено {updated}/{len(channels)} каналов", True)
        
        # Обновляем меню
        channels = get_channels(db)
        prizes = get_prizes(db)
        r = db.table("users").select("*", count="exact").execute()
        total = len(r.data) if r.data else 0
        
        text = (
            f"⚙️ <b>Панель администратора</b>\n\n"
            f"📢 Каналов: <b>{len(channels)}</b>\n"
            f"🎁 Призов: <b>{len(prizes)}</b>\n"
            f"👥 Пользователей: <b>{total}</b>\n\n"
            f"✅ Данные каналов обновлены!"
        )
        kb = {"inline_keyboard": [
            [{"text": f"📢 Каналы ({len(channels)})", "callback_data": "adm_channels"}],
            [{"text": f"🎁 Призы ({len(prizes)})", "callback_data": "adm_prizes"}],
            [{"text": "📊 Статистика", "callback_data": "adm_stats"}],
            [{"text": "🔄 Обновить данные каналов", "callback_data": "adm_refresh"}],
        ]}
        edit_message(chat_id, msg_id, text, kb)
    
    def send_channels_list(self, chat_id, db):
        """Отправляет список каналов как новое сообщение"""
        channels = get_channels(db)
        if channels:
            text = "📢 <b>Текущие каналы:</b>\n\n"
            for i, ch in enumerate(channels, 1):
                text += f"{i}. <b>{ch['title']}</b>"
                if ch["username"]:
                    text += f" (@{ch['username']})"
                text += f"\n   👥 {ch.get('member_count', 0)}\n"
            send_message(chat_id, text)
    
    def send_prizes_list(self, chat_id, db):
        prizes = get_prizes(db)
        if prizes:
            text = "🎁 <b>Текущие призы:</b>\n\n"
            for p in prizes:
                text += f"• {p['emoji']} <b>{p['name']}</b>\n"
            send_message(chat_id, text)