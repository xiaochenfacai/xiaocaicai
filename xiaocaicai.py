"""
小跟班记账 Telegram Bot + Flask Web 看板
部署环境变量: TELEGRAM_TOKEN, WEBHOOK_URL, PORT (可选)
"""

import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta

import pytz
import requests
import telebot
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "8880294546:AAG7yXrfznOAHxvCvvlj8qnFBmG54vqRz-E")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://caicai-888yg.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", "5000"))
FOUNDER_USERS = [8807178282]
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
MAX_LEVEL2_VIPS = 5
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SETTING_KEYS = {
    "operators", "exchange_rate", "fee_rate", "is_active",
    "language", "timezone", "show_usdt", "expire_time",
}

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

bot = telebot.TeleBot(TOKEN)
flask_app = Flask(__name__)
USER_STATE = {}

# ---------------------------------------------------------------------------
# Blockchain
# ---------------------------------------------------------------------------
def fetch_blockchain_usdt_info(address):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(f"https://api.trongrid.io/v1/accounts/{address}", headers=headers, timeout=10)
        balance = 0.0
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data"):
                for item in data["data"][0].get("trc20", []):
                    if USDT_CONTRACT in item:
                        balance = float(item[USDT_CONTRACT]) / 1_000_000
                        break

        history_text = ""
        try:
            tx_resp = requests.get(
                f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
                f"?limit=5&contract_address={USDT_CONTRACT}",
                headers=headers,
                timeout=10,
            )
            if tx_resp.status_code == 200:
                tx_list = tx_resp.json().get("data", [])
                if not tx_list:
                    history_text = "  暂无最近的 USDT 转账流水。"
                else:
                    for tx in tx_list:
                        from_addr = tx.get("from", "")
                        to_addr = tx.get("to", "")
                        raw_val = tx.get("value", tx.get("amount", "0"))
                        amount = float(raw_val) / 1_000_000 if raw_val else 0.0
                        if from_addr.lower() == address.lower():
                            direction, peer = "🔴 支出", f"去往: {to_addr[:6]}***{to_addr[-6:]}"
                        else:
                            direction, peer = "🟢 收入", f"来自: {from_addr[:6]}***{from_addr[-6:]}"
                        history_text += f"  {direction} | <b>{amount:.2f} U</b>\n  └ <i>{peer}</i>\n"
            else:
                history_text = "  ⚠️ 暂时无法获取流水明细（公共通道高频受限）。"
        except Exception:
            history_text = "  ⚠️ 链上网络拥堵，流水加载失败。"

        return {"success": True, "balance": balance, "history": history_text}
    except Exception as exc:
        return {"success": False, "msg": str(exc)}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect("bot_data.db", timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            group_id INTEGER PRIMARY KEY,
            operators TEXT DEFAULT '[]',
            exchange_rate REAL DEFAULT 7.2,
            fee_rate REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            language TEXT DEFAULT 'chinese',
            timezone TEXT DEFAULT 'Asia/Shanghai',
            show_usdt INTEGER DEFAULT 1,
            expire_time TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            user_id INTEGER,
            username TEXT,
            remark TEXT,
            amount REAL,
            usdt_amount REAL,
            exchange_rate REAL,
            bill_type TEXT,
            timestamp TEXT,
            date_str TEXT,
            is_settled INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS vip_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            expire_time TEXT,
            level INTEGER DEFAULT 2
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_current_time(timezone_str="Asia/Shanghai"):
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.timezone("Asia/Shanghai")
    now = datetime.now(tz)
    return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def get_user_permission_level(user_id):
    if user_id in FOUNDER_USERS:
        return True, "最高级买家 (系统创始人)", "永久终身授权", 1

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT expire_time, level FROM vip_users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expire:
                lvl = row[1] or 2
                desc = "最高级买家 (VIP1)" if lvl == 1 else "权限人 (二级VIP2)"
                return True, desc, row[0], lvl
            return False, "已到期", row[0], 0
    except Exception as exc:
        log.exception("get_user_permission_level: %s", exc)
    return False, "普通用户", "未激活", 0


def add_vip_user(user_id, username, months=12, level=2):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    now = datetime.now()
    if row:
        try:
            current = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            base = current if current > now else now
        except Exception:
            base = now
    else:
        base = now
    expire_str = (base + timedelta(days=30 * months)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT OR REPLACE INTO vip_users (user_id, username, expire_time, level) VALUES (?, ?, ?, ?)",
        (user_id, username, expire_str, level),
    )
    conn.commit()
    conn.close()
    return expire_str


def get_level2_vip_count():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM vip_users WHERE level = 2 AND expire_time > ?",
            (now_str,),
        )
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def get_all_level2_vips():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT user_id, username FROM vip_users WHERE level = 2 AND expire_time > ?",
            (now_str,),
        )
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def remove_vip_user(user_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM vip_users WHERE user_id = ? AND level = 2", (user_id,))
        deleted = c.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return False


def get_setting(group_id, key):
    cols = [
        "group_id", "operators", "exchange_rate", "fee_rate", "is_active",
        "language", "timezone", "show_usdt", "expire_time",
    ]
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        row = c.fetchone()
        if not row:
            _, _, init_time = get_current_time()
            c.execute(
                "INSERT OR IGNORE INTO settings "
                "(group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt, expire_time) "
                "VALUES (?, '[]', 7.2, 0, 1, 'chinese', 'Asia/Shanghai', 1, ?)",
                (group_id, init_time),
            )
            conn.commit()
            c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
            row = c.fetchone()
        conn.close()
        return dict(zip(cols, row)).get(key)
    except Exception:
        return None


def update_setting(group_id, key, value):
    if key not in SETTING_KEYS:
        return
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.exception("update_setting: %s", exc)


def normalize_operator_name(name):
    name = (name or "").strip()
    if not name:
        return ""
    return name if name.startswith("@") else f"@{name}"


def get_group_operators(group_id):
    try:
        return json.loads(get_setting(group_id, "operators") or "[]")
    except Exception:
        return []


def can_operate_in_group(group_id, user_id, tg_username=None):
    has_auth, _, _, _ = get_user_permission_level(user_id)
    if has_auth:
        return True
    ops = get_group_operators(group_id)
    if user_id in ops:
        return True
    if tg_username:
        bare = tg_username.lower()
        for op in ops:
            op_str = str(op).lower().lstrip("@")
            if op_str == bare:
                return True
    return False


def can_manage_group_operators(user_id):
    if user_id in FOUNDER_USERS:
        return True
    has_auth, _, _, lvl = get_user_permission_level(user_id)
    return has_auth and lvl in (1, 2)


def extract_mention(text, entities):
    if not entities:
        return ""
    for entity in entities:
        if entity.type == "mention":
            return text[entity.offset: entity.offset + entity.length].strip()
    return ""


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, "exchange_rate") or 7.2
    usdt_amount = amount / exchange_rate if bill_type == "income" else amount
    _, _, full_time = get_current_time()
    date_str = full_time[:10]
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO bills
        (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate,
         bill_type, timestamp, date_str, is_settled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str),
    )
    conn.commit()
    conn.close()
    return usdt_amount


def get_class_bills_by_date(group_id, target_date):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp "
        "FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id ASC",
        (group_id, target_date),
    )
    income = c.fetchall()
    c.execute(
        "SELECT remark, username, usdt_amount, exchange_rate, timestamp "
        "FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id ASC",
        (group_id, target_date),
    )
    expense = c.fetchall()
    c.execute(
        "SELECT SUM(amount), SUM(usdt_amount) FROM bills "
        "WHERE group_id = ? AND date_str = ? AND bill_type = 'income'",
        (group_id, target_date),
    )
    total_income = c.fetchone()
    c.execute(
        "SELECT SUM(usdt_amount) FROM bills "
        "WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'",
        (group_id, target_date),
    )
    total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense


def _format_income_line(remark, operator, amount, usdt, rate, timestamp):
    time_s = timestamp[11:16]
    body = f"{amount:.0f}/{rate:.2f}={usdt:.2f}U"
    rem = (remark or "").strip()
    if rem:
        return f"{rem} {time_s} {body} {operator}"
    return f"{time_s} {body} {operator}"


def _format_expense_line(remark, operator, usdt, timestamp):
    time_s = timestamp[11:16]
    body = f"下发 {usdt:.2f}U"
    rem = (remark or "").strip()
    if rem:
        return f"{rem} {time_s} {body} {operator}"
    return f"{time_s} {body} {operator}"


def build_bill_report_text(group_id, target_date, show_all_categories=False):
    rate = get_setting(group_id, "exchange_rate") or 7.2
    fee_rate = get_setting(group_id, "fee_rate") or 0.0
    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)

    total_rmb = (total_income[0] or 0) if total_income else 0
    total_usdt = (total_income[1] or 0) if total_income else 0
    expense_usdt = (total_expense[0] or 0) if total_expense else 0
    remaining_usdt = total_usdt - expense_usdt

    summary = {}
    for row in income:
        rem = (row[0] or "").strip() or "无备注"
        summary.setdefault(rem, {"rmb": 0.0, "usdt": 0.0})
        summary[rem]["rmb"] += row[2]
        summary[rem]["usdt"] += row[3]

    report = f"📊 <b>账单汇总 ({target_date})</b>\n\n"
    report += f"📥 <b>入款（{len(income)}笔）</b>\n"
    if income:
        for row in income[-5:]:
            report += _format_income_line(row[0], row[1], row[2], row[3], row[4], row[5]) + "\n"
    else:
        report += "  暂无入款\n"

    report += "\n📥 <b>入款备注分类</b>\n"
    category_items = list(summary.items())
    visible_categories = category_items if show_all_categories else category_items[:3]
    if visible_categories:
        for key, val in visible_categories:
            report += f"{key} 👉 {val['rmb']:.0f} | {val['usdt']:.2f}U\n"
    else:
        report += "  暂无分类\n"

    report += f"\n📤 <b>下发（{len(expense)}笔）</b>\n"
    if expense:
        for row in expense[-5:]:
            report += _format_expense_line(row[0], row[1], row[2], row[4]) + "\n"
    else:
        report += "  暂无下发\n"

    report += (
        f"\n💰 <b>总入款:</b> {total_rmb:.0f}\n"
        f"📉 <b>费率:</b> {fee_rate * 100:.0f}%\n"
        f"💱 <b>汇率:</b> {rate:.2f}\n\n"
        f"应下发: {total_rmb:.0f} | {total_usdt:.2f} U\n"
        f"未下发: {total_rmb:.0f} | {remaining_usdt:.2f} U\n\n"
        f"<code>[核算编号: {random.randint(1000, 9999)}]</code>"
    )

    has_more_categories = len(category_items) > 3 and not show_all_categories
    return report, has_more_categories


def send_text_bill_report(chat_id, group_id, target_date):
    report, has_more = build_bill_report_text(group_id, target_date)
    markup = telebot.types.InlineKeyboardMarkup()
    if has_more:
        date_key = target_date.replace("-", "")
        markup.add(telebot.types.InlineKeyboardButton(
            "show more",
            callback_data=f"bill_cate_{group_id}_{date_key}",
        ))
    markup.add(telebot.types.InlineKeyboardButton(
        "📊 查看完整网页账单", url=f"{WEBHOOK_URL}/?group_id={group_id}"
    ))
    bot.send_message(chat_id, report, parse_mode="HTML", reply_markup=markup)


# ---------------------------------------------------------------------------
# Telegram handlers — /start
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        has_auth, lvl_desc, _, lvl = get_user_permission_level(uid)
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot.types.InlineKeyboardButton("📅 查看到期时间", callback_data="btn_check_expire"),
            telebot.types.InlineKeyboardButton("📖 详细说明书", callback_data="btn_manual_guide"),
        )
        markup.add(telebot.types.InlineKeyboardButton("💰 自助续费说明", callback_data="btn_pay_usdt"))
        if uid in FOUNDER_USERS or (has_auth and lvl == 1):
            markup.add(
                telebot.types.InlineKeyboardButton("🔑 设置权限人", callback_data="btn_grant_vip2"),
                telebot.types.InlineKeyboardButton("❌ 取掉权限人", callback_data="btn_revoke_vip2"),
            )
        bot.send_message(
            message.chat.id,
            f"🤖 <b>您好！欢迎使用小跟班记账分布式管理中心</b>\n\n"
            f"👤 <b>当前身份：</b> <code>{lvl_desc}</code>\n"
            f"📌 请通过下方菜单按纽执行管理操作：",
            parse_mode="HTML",
            reply_markup=markup,
        )
    else:
        bot.send_message(
            message.chat.id,
            "🤖 <b>小跟班智能分布式记账系统已激活</b>\n\n"
            "👉 <b>群内核心记账命令：</b>\n"
            "• 发送 <code>上课</code> / <code>下课</code> 开启或封存账单\n"
            "• 发送 <code>+1000</code> 或 <code>+1000/7.3</code> 记入款\n"
            "• 发送 <code>项目公款+5000</code> 记带备注账目\n"
            "• 发送 <code>下发500</code> 记下发\n"
            "• 发送 <code>+0</code> 查看对账大底\n\n"
            "⚙️ <b>财务群管命令（买家老板/权限人）：</b>\n"
            "• <code>设置汇率 7.35</code>\n"
            "• <code>设置费率 5</code>\n"
            "• <code>设置操作人 @用户名</code>\n"
            "• <code>取掉操作人 @用户名</code>",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Telegram handlers — private menu callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("btn_"))
def handle_private_buttons(call):
    uid = call.from_user.id
    has_auth, lvl_desc, expire_time, lvl = get_user_permission_level(uid)
    chat_id = call.message.chat.id

    if call.data == "btn_check_expire":
        status = "🟢 正常生效中" if has_auth else "🔴 资质已过期/未激活"
        bot.send_message(
            chat_id,
            f"👤 <b>您的身份体系：</b>\n"
            f"• 级别：<code>{lvl_desc}</code>\n"
            f"• 状态：{status}\n"
            f"• 有效截止期：<code>{expire_time}</code>",
            parse_mode="HTML",
        )

    elif call.data == "btn_manual_guide":
        bot.send_message(
            chat_id,
            "📖 <b>【小跟班记账】全功能业务操作指南</b>\n\n"
            "👑 <b>权限架构：</b>\n"
            "1. <b>最高级买家</b>：私聊 6 键菜单，可指派二级权限人。\n"
            "2. <b>权限人(VIP2)</b>：可进群指派群操作人。\n"
            "3. <b>操作人</b>：群内专职记账。\n\n"
            "👥 <b>群内指令集：</b>\n"
            "• <code>设置操作人 @用户名</code>\n"
            "• <code>取掉操作人 @用户名</code>\n"
            "• <code>设置汇率 7.4</code>\n"
            "• <code>+5000/7.3 飞机备注</code>\n"
            "• <code>下发 800</code>\n"
            "• <code>+0</code>",
            parse_mode="HTML",
        )

    elif call.data == "btn_pay_usdt":
        bot.send_message(
            chat_id,
            f"💰 <b>USDT 授权价格套餐：</b>\n"
            f"• 1 个月高级买家：<b>80</b> USDT\n"
            f"• 3 个月高级买家：<b>230</b> USDT\n\n"
            f"💎 <b>官方波场(TRC20)收款地址：</b>\n<code>{TRON_ADDRESS}</code>\n\n"
            f"⚠️ 转账成功后，请将【成功截图凭证】私发给机器人，创始人审核后开通。",
            parse_mode="HTML",
        )

    elif call.data == "btn_grant_vip2":
        if uid not in FOUNDER_USERS and lvl != 1:
            bot.answer_callback_query(call.id, "只有最高级买家才能指派二级权限人。", show_alert=True)
            return
        if get_level2_vip_count() >= MAX_LEVEL2_VIPS:
            bot.send_message(
                chat_id,
                f"❌ 当前已满 <b>{MAX_LEVEL2_VIPS}</b> 个二级权限人，请先移除旧成员。",
                parse_mode="HTML",
            )
        else:
            USER_STATE[uid] = "WAITING_ADD_VIP2"
            bot.send_message(
                chat_id,
                "➡️ 请直接输入要授权的二级权限人 <b>UID（纯数字）</b>：",
                parse_mode="HTML",
            )

    elif call.data == "btn_revoke_vip2":
        if uid not in FOUNDER_USERS and lvl != 1:
            bot.answer_callback_query(call.id, "只有最高级买家才能撤销二级权限人。", show_alert=True)
            return
        vip_list = get_all_level2_vips()
        if not vip_list:
            bot.send_message(chat_id, "📭 您还没有设置任何二级权限人。", parse_mode="HTML")
        else:
            lines = [
                f"👤 <b>{name}</b> | UID: <code>{vid}</code>"
                for vid, name in vip_list
            ]
            USER_STATE[uid] = "WAITING_DEL_VIP2"
            bot.send_message(
                chat_id,
                f"📋 <b>二级权限人 ({len(vip_list)}/{MAX_LEVEL2_VIPS})</b>\n\n"
                + "\n".join(lines)
                + "\n\n➡️ 请发送要移除的 UID（纯数字）：",
                parse_mode="HTML",
            )

    bot.answer_callback_query(call.id)


@bot.my_chat_member_handler()
def handle_my_chat_member(update: telebot.types.ChatMemberUpdated):
    if update.new_chat_member.status in ("member", "administrator"):
        try:
            bot.send_message(
                update.chat.id,
                "<b>感谢您把我拉进贵群！</b>\n\n"
                "我是小财财机器人🤖\n"
                "请发送 <code>上课</code> 唤醒我，"
                "并设置费率（如 <code>设置费率 5</code>），然后即可开始记账。",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.error("入群欢迎语失败: %s", exc)


@bot.message_handler(content_types=["photo"])
def handle_receipt_photo(message):
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    username = message.from_user.username or "无用户名"
    first_name = message.from_user.first_name or "买家"
    photo_id = message.photo[-1].file_id

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("✅ 开通1个月", callback_data=f"auth_1_{uid}"),
        telebot.types.InlineKeyboardButton("✅ 开通3个月", callback_data=f"auth_3_{uid}"),
    )
    markup.add(telebot.types.InlineKeyboardButton("❌ 拒绝开通", callback_data=f"auth_reject_{uid}"))

    for founder in FOUNDER_USERS:
        try:
            bot.send_message(
                founder,
                f"🔔 <b>收到续费申请</b>\n\n"
                f"👤 {first_name} (@{username})\n🆔 UID: <code>{uid}</code>",
                parse_mode="HTML",
            )
            bot.send_photo(founder, photo_id, reply_markup=markup)
        except Exception:
            pass
    bot.reply_to(message, "⏳ 续费凭证已提交，请等待 1-3 分钟审核。")


@bot.callback_query_handler(func=lambda call: call.data.startswith("auth_"))
def handle_auth_buttons(call):
    if call.from_user.id not in FOUNDER_USERS:
        bot.answer_callback_query(call.id, "您不是系统创始人，无权审核！", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]

    if action == "reject":
        buyer_id = int(parts[2])
        try:
            bot.send_message(buyer_id, "❌ <b>续费申请未通过。</b>", parse_mode="HTML")
        except Exception:
            pass
        bot.edit_message_caption("❌ 已驳回该申请。", call.message.chat.id, call.message.message_id)
    else:
        months = int(action)
        buyer_id = int(parts[2])
        expire_str = add_vip_user(buyer_id, f"user_{buyer_id}", months, level=1)
        try:
            bot.send_message(
                buyer_id,
                f"🎉 <b>最高级买家已开通 {months} 个月！</b>\n到期：{expire_str}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        bot.edit_message_caption(
            f"✅ 审核成功，到期：{expire_str}",
            call.message.chat.id,
            call.message.message_id,
        )
    bot.answer_callback_query(call.id, "操作成功！")


@bot.callback_query_handler(func=lambda call: call.data.startswith("bill_cate_"))
def handle_bill_category_more(call):
    rest = call.data[len("bill_cate_"):]
    sep = rest.rfind("_")
    if sep < 0:
        bot.answer_callback_query(call.id)
        return
    try:
        group_id = int(rest[:sep])
        date_key = rest[sep + 1:]
        target_date = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "数据解析失败", show_alert=True)
        return

    report, _ = build_bill_report_text(group_id, target_date, show_all_categories=True)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "📊 查看完整网页账单", url=f"{WEBHOOK_URL}/?group_id={group_id}"
    ))
    try:
        bot.edit_message_text(
            report,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception as exc:
        log.exception("expand bill categories: %s", exc)
    bot.answer_callback_query(call.id)


# ---------------------------------------------------------------------------
# Telegram handlers — all text messages
# ---------------------------------------------------------------------------
@bot.message_handler(func=lambda m: True)
def handle_all_messages(message):
    if not message.text:
        return

    text = message.text.strip()
    gid = message.chat.id
    uid = message.from_user.id
    tg_username = message.from_user.username
    display_name = message.from_user.first_name or "用户"

    # --- private chat ---
    if message.chat.type == "private":
        state = USER_STATE.pop(uid, None)
        if state in ("WAITING_ADD_VIP2", "WAITING_DEL_VIP2"):
            if not text.isdigit():
                bot.reply_to(message, "❌ UID 必须是纯数字，请重新点击菜单操作。", parse_mode="HTML")
                return
            target_uid = int(text)
            if state == "WAITING_ADD_VIP2":
                if get_level2_vip_count() >= MAX_LEVEL2_VIPS:
                    bot.reply_to(message, f"❌ 二级权限人已满 {MAX_LEVEL2_VIPS} 个。", parse_mode="HTML")
                    return
                expire_str = add_vip_user(target_uid, f"vip2_{target_uid}", months=12, level=2)
                bot.reply_to(
                    message,
                    f"✅ 已授权 UID <code>{target_uid}</code> 为二级权限人，到期：{expire_str}",
                    parse_mode="HTML",
                )
                try:
                    bot.send_message(target_uid, "🎉 您已被提升为二级权限人(VIP2)。", parse_mode="HTML")
                except Exception:
                    pass
            elif remove_vip_user(target_uid):
                bot.reply_to(message, f"🗑️ 已移除 UID <code>{target_uid}</code> 的二级权限。", parse_mode="HTML")
                try:
                    bot.send_message(target_uid, "⚠️ 您的二级权限人资格已被撤销。", parse_mode="HTML")
                except Exception:
                    pass
            else:
                bot.reply_to(message, "❌ 未找到该二级权限人，或移除失败。")
            return

        if text == "查看到期时间":
            _, lvl_desc, expire_time, _ = get_user_permission_level(uid)
            bot.reply_to(
                message,
                f"👤 身份：<code>{lvl_desc}</code>\n📅 到期：<code>{expire_time}</code>",
                parse_mode="HTML",
            )
            return

    # --- chain lookup (any chat) ---
    if text.startswith("查看"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            addr = parts[1].strip()
            if addr.startswith("T") and len(addr) == 34:
                wait = bot.reply_to(message, "🔍 正在查询链上数据...")
                result = fetch_blockchain_usdt_info(addr)
                try:
                    bot.delete_message(gid, wait.message_id)
                except Exception:
                    pass
                if result["success"]:
                    bot.reply_to(
                        message,
                        f"👤 地址：<code>{addr}</code>\n\n"
                        f"💰 USDT 余额：<code>{result['balance']:.2f}</code> U\n"
                        f"━━━━━━━━━━━━━━━━━━\n📊 流向明细：\n{result['history']}",
                        parse_mode="HTML",
                    )
                else:
                    bot.reply_to(message, f"❌ 检索失败: {result['msg']}")
                return

    if message.chat.type not in ("group", "supergroup"):
        return

    # --- group commands ---
    now, _, _ = get_current_time()
    today = now.strftime("%Y-%m-%d")

    if text.startswith("设置汇率"):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, "⚠️ 无权修改汇率。")
            return
        try:
            rate = float(text.replace("设置汇率", "").strip())
            update_setting(gid, "exchange_rate", rate)
            bot.reply_to(message, f"✅ 汇率已调整为 <b>{rate:.2f}</b>", parse_mode="HTML")
        except ValueError:
            bot.reply_to(message, "❌ 格式错误，例如：设置汇率 7.3")
        return

    if text.startswith("设置费率"):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, "⚠️ 无权修改费率。")
            return
        try:
            fee = float(text.replace("设置费率", "").strip()) / 100
            update_setting(gid, "fee_rate", fee)
            bot.reply_to(message, f"✅ 费率已更新为 {fee * 100:.0f}%")
        except ValueError:
            bot.reply_to(message, "❌ 格式错误，例如：设置费率 5")
        return

    if text.startswith("设置操作人"):
        if not can_manage_group_operators(uid):
            bot.reply_to(message, "⚠️ 只有买家或二级权限人才能指派操作人。")
            return
        target = extract_mention(text, message.entities) or text.replace("设置操作人", "").strip()
        target = normalize_operator_name(target)
        if not target:
            bot.reply_to(message, "💡 用法：<code>设置操作人 @用户名</code>", parse_mode="HTML")
            return
        ops = get_group_operators(gid)
        if target not in ops:
            ops.append(target)
            update_setting(gid, "operators", json.dumps(ops, ensure_ascii=False))
        bot.reply_to(message, f"✅ 已将 <b>{target}</b> 设为本群操作人。", parse_mode="HTML")
        return

    if text.startswith("取掉操作人") or text.startswith("取消操作人"):
        if not can_manage_group_operators(uid):
            bot.reply_to(message, "⚠️ 只有买家或二级权限人才能移除操作人。")
            return
        target = extract_mention(text, message.entities)
        if not target:
            target = text.replace("取掉操作人", "").replace("取消操作人", "").strip()
        target = normalize_operator_name(target)
        ops = get_group_operators(gid)
        removed = False
        for candidate in (target, target.lstrip("@"), f"@{target.lstrip('@')}"):
            if candidate in ops:
                ops.remove(candidate)
                removed = True
                break
        if removed:
            update_setting(gid, "operators", json.dumps(ops, ensure_ascii=False))
            bot.reply_to(message, f"🗑️ 已移除操作人 <b>{target}</b>。", parse_mode="HTML")
        else:
            bot.reply_to(message, f"ℹ️ <b>{target}</b> 不是本群操作人。", parse_mode="HTML")
        return

    if text in ("删最后", "删今天", "删全部"):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, "⚠️ 无权删账。")
            return
        conn = get_db()
        c = conn.cursor()
        if text == "删最后":
            c.execute("SELECT id, remark, amount FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (gid,))
            row = c.fetchone()
            if row:
                c.execute("DELETE FROM bills WHERE id = ?", (row[0],))
                bot.reply_to(message, f"🗑️ 已撤销：【{row[1] or '无备注'}: {row[2]}】")
            else:
                bot.reply_to(message, "📭 暂无账单。")
        elif text == "删今天":
            c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (gid, today))
            bot.reply_to(message, f"🗑️ 已清空今日 ({today}) 账单。")
        else:
            c.execute("DELETE FROM bills WHERE group_id = ?", (gid,))
            bot.reply_to(message, "🗑️ 已清空本群全部历史账单。")
        conn.commit()
        conn.close()
        send_text_bill_report(gid, gid, today)
        return

    if text.startswith("清单"):
        remark = text.replace("清单", "", 1).strip()
        if not remark:
            bot.reply_to(message, "💡 用法：清单 飞机群公款")
            return
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT timestamp, amount, usdt_amount, username FROM bills "
            "WHERE group_id = ? AND date_str = ? AND remark = ? AND bill_type = 'income'",
            (gid, today, remark),
        )
        rows = c.fetchall()
        conn.close()
        if not rows:
            bot.reply_to(message, f"🔍 今日无备注【{remark}】的进单。")
            return
        report = f"📋 <b>【{remark}】进单明细</b>\n\n"
        total_r, total_u = 0.0, 0.0
        for ts, amt, uamt, uname in rows:
            report += f"  🔹 {ts[11:16]} | {amt:.0f} RMB → {uamt:.1f} U ({uname})\n"
            total_r += amt
            total_u += uamt
        report += f"\n📈 合计：{total_r:.0f} RMB / {total_u:.1f} USDT"
        bot.reply_to(message, report, parse_mode="HTML")
        return

    if text == "上课":
        if not can_operate_in_group(gid, uid, tg_username):
            return
        update_setting(gid, "is_active", 1)
        bot.reply_to(message, "🟢 记账通道已开启！")
        return

    if text == "下课":
        if not can_operate_in_group(gid, uid, tg_username):
            return
        update_setting(gid, "is_active", 0)
        bot.reply_to(message, "🔴 下课成功，今日账单已封存。")
        send_text_bill_report(gid, gid, today)
        return

    if not get_setting(gid, "is_active"):
        return

    if not can_operate_in_group(gid, uid, tg_username):
        return

    if text == "+0":
        send_text_bill_report(gid, gid, today)
        return

    m_exp = re.match(r"^(.*?)(?:下发|ထုတ်)\s*(-?\d+(?:\.\d+)?)$", text)
    if m_exp:
        add_bill(gid, uid, display_name, m_exp.group(1).strip(), float(m_exp.group(2)), "expense")
        send_text_bill_report(gid, gid, today)
        return

    m_inc = re.match(r"^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$", text)
    if m_inc:
        amount = float(m_inc.group(3))
        if m_inc.group(2) == "-":
            amount = -amount
        rate = float(m_inc.group(4)) if m_inc.group(4) else None
        add_bill(gid, uid, display_name, m_inc.group(1).strip(), amount, "income", rate)
        send_text_bill_report(gid, gid, today)


# ---------------------------------------------------------------------------
# Flask web dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>分布式全功能网页账单</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,sans-serif}
body{background:#f4f6f9;color:#333;padding:12px;line-height:1.4}
.container{max-width:800px;margin:0 auto;background:#fff;border-radius:12px;padding:16px;box-shadow:0 4px 12px rgba(0,0,0,.05)}
.header{text-align:center;margin-bottom:20px;border-bottom:2px solid #edf2f7;padding-bottom:15px}
.date-picker{margin:10px 0;background:#f8fafc;padding:8px;border-radius:6px;display:flex;align-items:center;justify-content:center;gap:8px;border:1px dashed #cbd5e1}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:25px;border-top:2px dashed #cbd5e1;padding-top:20px}
.card{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;text-align:center}
.card .title{font-size:12px;color:#64748b}
.card .value{font-size:18px;font-weight:bold;margin-top:2px}
h3{font-size:15px;margin:25px 0 8px;padding-left:6px;border-left:4px solid #3b82f6}
.exp-title{border-left-color:#ef4444}.cate-title{border-left-color:#10b981}
table{width:100%;border-collapse:collapse;margin-top:5px;font-size:13px}
th,td{padding:10px;border-bottom:1px solid #e2e8f0;text-align:left}
th{background:#f1f5f9;color:#475569}
.badge{display:inline-block;padding:2px 6px;font-size:11px;border-radius:4px;font-weight:bold;background:#e2e8f0}
.bg-inc{background:#dcfce7;color:#15803d}.bg-exp{background:#fee2e2;color:#b91c1c}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h2>📊 分布式对账看板</h2>
<p id="group-text" style="font-size:12px;color:#64748b;margin-top:4px">加载中...</p>
<div class="date-picker">
<label for="date-select">📅 账单日期：</label>
<input type="date" id="date-select" onchange="dateChanged(this.value)">
</div>
</div>
<h3>📥 进单明细</h3>
<table><thead><tr><th>时间</th><th>备注</th><th>RMB</th><th>U</th><th>操作人</th></tr></thead><tbody id="income-list"></tbody></table>
<h3 class="exp-title">📤 下发明细</h3>
<table><thead><tr><th>时间</th><th>备注</th><th>USDT</th><th>操作人</th></tr></thead><tbody id="expense-list"></tbody></table>
<h3 class="cate-title">🗂️ 备注分类</h3>
<table><thead><tr><th>备注</th><th>RMB</th><th>USDT</th><th>笔数</th></tr></thead><tbody id="cate-list"></tbody></table>
<div class="summary-grid">
<div class="card"><div class="title">汇率</div><div class="value" id="rate">0</div></div>
<div class="card"><div class="title">总入款 RMB</div><div class="value" id="total_rmb">0</div></div>
<div class="card"><div class="title">总入款 USDT</div><div class="value" id="total_usdt">0U</div></div>
<div class="card"><div class="title">已下发 USDT</div><div class="value" id="expense_usdt">0U</div></div>
<div class="card" style="grid-column:span 2"><div class="title">未下发 USDT</div><div class="value" id="remaining_usdt">0U</div></div>
</div>
</div>
<script>
const params=new URLSearchParams(location.search);
const groupId=params.get('group_id')||'0';
document.getElementById('group-text').textContent='群组 ID: '+groupId;
const ds=document.getElementById('date-select');
ds.value=params.get('date')||new Date().toISOString().slice(0,10);
function dateChanged(d){location.href=`?group_id=${groupId}&date=${d}`}
async function load(){
const d=ds.value;
const r=await fetch(`/api/bill?group_id=${groupId}&date=${d}`);
const data=await r.json();
['rate','total_rmb'].forEach(k=>document.getElementById(k).textContent=data[k]);
document.getElementById('total_usdt').textContent=data.total_usdt+' U';
document.getElementById('expense_usdt').textContent=data.expense_usdt+' U';
document.getElementById('remaining_usdt').textContent=data.remaining_usdt+' U';
document.getElementById('cate-list').innerHTML=(data.category_summary||[]).length
?data.category_summary.map(c=>`<tr><td><span class="badge bg-inc">${c.remark}</span></td><td>${c.total_rmb}</td><td>${c.total_usdt} U</td><td>${c.count}</td></tr>`).join('')
:'<tr><td colspan="4" style="text-align:center;color:#94a3b8">暂无</td></tr>';
document.getElementById('income-list').innerHTML=(data.income_bills||[]).length
?data.income_bills.map(b=>`<tr><td>${b.time}</td><td>${b.remark}</td><td>+${b.amount}</td><td>${b.usdt} U</td><td>${b.username}</td></tr>`).join('')
:'<tr><td colspan="5" style="text-align:center;color:#94a3b8">暂无</td></tr>';
document.getElementById('expense-list').innerHTML=(data.expense_bills||[]).length
?data.expense_bills.map(e=>`<tr><td>${e.time}</td><td>${e.remark}</td><td>-${e.usdt} U</td><td>${e.username}</td></tr>`).join('')
:'<tr><td colspan="4" style="text-align:center;color:#94a3b8">暂无</td></tr>';
}
load();
</script>
</body>
</html>"""


@flask_app.route("/")
def index():
    return DASHBOARD_HTML


@flask_app.route("/api/bill")
def api_bill():
    try:
        group_id = int(request.args.get("group_id", "0").strip())
    except ValueError:
        group_id = 0

    tz = get_setting(group_id, "timezone") or "Asia/Shanghai"
    now, _, _ = get_current_time(tz)
    target_date = request.args.get("date", now.strftime("%Y-%m-%d"))

    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
    rate = get_setting(group_id, "exchange_rate") or 7.2
    total_rmb = (total_income[0] or 0) if total_income else 0
    total_usdt = (total_income[1] or 0) if total_income else 0
    expense_usdt = (total_expense[0] or 0) if total_expense else 0

    income_bills = [
        {
            "remark": r[0] or "无备注",
            "username": r[1] or "未知",
            "amount": f"{r[2]:.0f}",
            "usdt": f"{r[3]:.2f}",
            "time": r[5][11:19] if r[5] else "",
        }
        for r in income
    ]
    expense_bills = [
        {
            "remark": r[0] or "无备注",
            "username": r[1] or "未知",
            "usdt": f"{r[2]:.2f}",
            "time": r[4][11:19] if r[4] else "",
        }
        for r in expense
    ]

    summary = {}
    for row in income:
        rem = (row[0] or "空备注").strip()
        summary.setdefault(rem, {"total_rmb": 0.0, "total_usdt": 0.0, "count": 0})
        summary[rem]["total_rmb"] += row[2] or 0
        summary[rem]["total_usdt"] += row[3] or 0
        summary[rem]["count"] += 1

    category_summary = [
        {
            "remark": k,
            "total_rmb": f"{v['total_rmb']:.0f}",
            "total_usdt": f"{v['total_usdt']:.2f}",
            "count": v["count"],
        }
        for k, v in summary.items()
    ]

    return jsonify({
        "exchange_rate": f"{rate:.2f}",
        "total_rmb": f"{total_rmb:.0f}",
        "total_usdt": f"{total_usdt:.2f}",
        "expense_usdt": f"{expense_usdt:.2f}",
        "remaining_usdt": f"{total_usdt - expense_usdt:.2f}",
        "income_bills": income_bills,
        "expense_bills": expense_bills,
        "category_summary": category_summary,
    })


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok", 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------
