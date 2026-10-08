import os
import re
import json
import logging
import asyncio
import time
import random
import secrets
import sys
from datetime import datetime
from pathlib import Path

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import RetryAfter, ChatMigrated, Forbidden, BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)

# التوكن لازم ينحط كمتغير بيئة (BOT_TOKEN) ولا يُكتب داخل الكود أبداً
TOKEN = os.environ.get("8897589222:AAGVfPw75tiG3__W17p3qqpO_MwaykpvDc8", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "8446946673"))
DB_FILE = Path("database.json")

DEFAULT_SETTINGS = {
    "daily_gift": 10,
    "referral_points": 5,
    "join_reward": 2,
    "order_cost": 2,
    "min_order_members": 20,
    "min_order_points": 40,
    "transfer_fee_percent": 10,
    "unsub_penalty": 4,
    "daily_task_bonus": 5,
    "daily_task_joins": 3,
    "proofs_channel": "",
    "smm_url": "https://smm-provider.com/api/v2",
    "smm_key": "",
}

SETTING_LABELS = {
    "daily_gift": "🎁 الهدية اليومية",
    "referral_points": "🔔 نقاط الدعوة",
    "join_reward": "➕ مكافأة الانضمام لقناة",
    "order_cost": "💰 سعر العضو (نقاط)",
    "min_order_members": "👥 أقل عدد أعضاء للطلب",
    "transfer_fee_percent": "🔀 عمولة التحويل %",
    "unsub_penalty": "🚪 غرامة الخروج من قناة",
    "daily_task_bonus": "🎯 مكافأة المهمة اليومية",
    "daily_task_joins": "📌 عدد قنوات المهمة اليومية",
}


# ============================ قاعدة البيانات ============================

def load_db():
    data = {}
    if DB_FILE.exists():
        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logging.error(f"خطأ أثناء قراءة قاعدة البيانات: {e}")
            try:
                DB_FILE.replace(DB_FILE.with_suffix(".corrupt"))
            except Exception:
                pass
            data = {}

    data.setdefault("users", {})
    data.setdefault("channels", {})
    data.setdefault("forced_channels", [])
    data.setdefault("gifts", {})        # أكواد الهدايا (يكتبها العضو)
    data.setdefault("gift_links", {})   # روابط الهدايا (يفتحها العضو)
    data.setdefault("chats", {})

    # ترحيل الصيغة القديمة (قائمة آيديات) إلى الصيغة الجديدة (قاموس بمعلومات كل كروب/قناة)
    if isinstance(data["chats"], list):
        old = data["chats"]
        data["chats"] = {}
        for c in old:
            try:
                cid = int(c)
            except (TypeError, ValueError):
                continue
            data["chats"][str(cid)] = {
                "id": cid, "title": "غير معروف", "type": "group",
                "username": "", "can_post": True, "added": 0,
            }
    data.setdefault("total_orders_completed", 0)

    s = data.setdefault("settings", {})
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)

    data.setdefault("banned", [])
    data.setdefault("maintenance", False)
    return data


db = load_db()


def save_db():
    try:
        tmp = DB_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DB_FILE)
    except Exception as e:
        logging.error(f"خطأ أثناء حفظ قاعدة البيانات: {e}")


# ============================ دوال مساعدة ============================

def clean(s):
    return re.sub(r"[*_`\[\]]", "", str(s or ""))


def parse_channel(arg):
    arg = str(arg).strip()
    arg = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", arg)
    arg = arg.lstrip("@").split("/")[0].split("?")[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", arg):
        return arg
    return None


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def get_daily(user):
    d = user.get("daily")
    if not d or d.get("date") != today_str():
        d = {"date": today_str(), "joins": 0, "claimed": False}
        user["daily"] = d
    return d


def ensure_user(update: Update, referrer_id=None):
    user = update.effective_user
    if not user:
        return None
    uid = str(user.id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "id": user.id,
            "name": user.first_name or "عضو",
            "username": user.username or "",
            "points": 0,
            "referrals": 0,
            "last_gift": 0,
            "last_wheel": 0,
            "joined_channels": []
        }

        if referrer_id and str(referrer_id) in db["users"] and str(referrer_id) != uid:
            ref_pts = db["settings"]["referral_points"]
            db["users"][str(referrer_id)]["points"] += ref_pts
            db["users"][str(referrer_id)]["referrals"] = db["users"][str(referrer_id)].get("referrals", 0) + 1

        save_db()

    u = db["users"][uid]
    u.setdefault("joined_channels", [])
    return u


async def reply(update: Update, text, **kwargs):
    if update.message:
        return await update.message.reply_text(text, **kwargs)
    if update.callback_query:
        return await update.callback_query.message.reply_text(text, **kwargs)


async def is_member(bot, chat_id, user_id):
    member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    if member.status in ("member", "administrator", "creator"):
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def check_forced_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return True
    uid = user.id
    if uid == OWNER_ID:
        return True

    if not db.get("forced_channels"):
        return True

    unsubscribed = []
    for ch in db.get("forced_channels", []):
        try:
            if not await is_member(context.bot, ch["chat_id"], uid):
                unsubscribed.append(ch)
        except Exception:
            unsubscribed.append(ch)

    if unsubscribed:
        buttons = []
        for ch in unsubscribed:
            buttons.append([InlineKeyboardButton(f"📢 اشترك في: {ch['title']}", url=ch["url"])])

        buttons.append([InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data="recheck_sub")])

        if update.message:
            await update.message.reply_text(
                "💖 **يا بعد قلبي، حتى نضمن خدمة ممتازة للجميع، لازم تشترك بهاي القنوات أولاً:**\n\nاشترك واضغط على زر (✅ تحققت من الاشتراك) حتى ينفتح لك البوت مباشرة 👇",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        elif update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ **بعدك ما مشترك بجميع القنوات عيوني! اشترك واضغط تحقق مرة ثانية.**",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        return False

    return True


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return True
    uid = user.id
    if uid == OWNER_ID:
        return True
    if uid in db.get("banned", []):
        await reply(update, "🚫 **عذراً يا الغالي، حسابك محظور من استخدام خدمات البوت.**", parse_mode="Markdown")
        return False
    if db.get("maintenance", False):
        await reply(update, "🛠️ **البوت حالياً تحت الصيانة والتطوير عيوني، نرجع لكم بشيء أقوى قريباً!**", parse_mode="Markdown")
        return False
    return await check_forced_sub(update, context)


# ============================ الكيبوردات ============================

def main_keyboard(user_points, uid):
    buttons = [
        [f"💎 نقاطك الحالية: {user_points} نقطة"],
        ["🚀 طلب تمويل قناتك", "⭐ تجميع نقاط"],
        ["🎡 عجلة الحظ اليومية", "🎯 المهام اليومية (+نقاط)"],
        ["🎁 الهدية اليومية", "💳 ادخال كود هدية"],
        ["🔀 تحويل نقاط", "💰 شحن نقاطك"],
        ["🔔 رابط الدعوة (+نقاط)", "📢 قنواتنا والدعم الفني"],
        ["👤 حسابي والمعلومات", "📊 متابعة طلبيات التمويل"]
    ]
    if int(uid) == int(OWNER_ID):
        buttons.append(["👑 لوحة تحكم المالك"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def owner_keyboard():
    buttons = [
        ["⚙️ تعديل الأسعار والمكافآت", "🔒 إدارة الاشتراك الإجباري"],
        ["🎁 إنشاء كود هدية", "🔗 إنشاء رابط هدية"],
        ["📋 الهدايا الفعالة", "📢 قناة إثباتات التمويل"],
        ["➕ إضافة نقاط لعضو", "➖ خصم نقاط من عضو"],
        ["📢 إضافة قناة تجميع", "📣 إذاعة (أعضاء + كروبات + قنوات)"],
        ["🗂 الكروبات والقنوات", "📊 إحصائيات البوت"],
        ["🌐 ربط موقع الرشق (API)", "🚫 حظر/فك حظر عضو"],
        ["🛠️ تفعيل/تعطيل الصيانة", "💾 نسخة احتياطية (Backup)"],
        ["🔙 الرجوع للقائمة الرئيسية"]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


BROADCAST_BTN = "📣 إذاعة (أعضاء + كروبات + قنوات)"

OWNER_BTNS = [
    "👑 لوحة تحكم المالك", "⚙️ تعديل الأسعار والمكافآت", "🔒 إدارة الاشتراك الإجباري",
    "➕ إضافة نقاط لعضو", "➖ خصم نقاط من عضو", "📢 إضافة قناة تجميع",
    BROADCAST_BTN, "📊 إحصائيات البوت", "🛠️ تفعيل/تعطيل الصيانة",
    "💾 نسخة احتياطية (Backup)", "🔙 الرجوع للقائمة الرئيسية", "🌐 ربط موقع الرشق (API)",
    "🚫 حظر/فك حظر عضو", "🎁 إنشاء كود هدية", "🔗 إنشاء رابط هدية", "📋 الهدايا الفعالة",
    "🗂 الكروبات والقنوات", "📢 قناة إثباتات التمويل", "إذاعة"
]

USER_BTNS = [
    "🎁 الهدية اليومية", "🎡 عجلة الحظ اليومية", "🔔 رابط الدعوة (+نقاط)",
    "🚀 طلب تمويل قناتك", "⭐ تجميع نقاط", "🎯 المهام اليومية (+نقاط)",
    "💳 ادخال كود هدية", "🔀 تحويل نقاط", "💰 شحن نقاطك",
    "📢 قنواتنا والدعم الفني", "👤 حسابي والمعلومات", "📊 متابعة طلبيات التمويل"
]

POINTS_LABEL = "💎 نقاطك الحالية"


def welcome_text(uid, user):
    return (
        "🔥 **يا هلا ومية هلا بـ بوت التمويل العراقي الشامل!** ✨\n\n"
        "نورت البوت يا الغالي.. هنا تقدر تمول قناتك، تجمع نقاط، وترفع تفاعل مجموعتك بسهولة وبدون أي تعقيد.\n\n"
        f"🆔 **الآيدي مالتك:** `{uid}`\n"
        f"💎 **رصيد نقاطك الحالي:** `{user['points']}` نقطة\n\n"
        "اختر من القائمة المنسدلة جوة واستمتع بالخدمات 👇"
    )


# ============================ الأوامر ============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    context.user_data.clear()

    if db.get("maintenance", False) and uid != OWNER_ID:
        await update.message.reply_text("🛠️ **البوت حالياً تحت الصيانة والتطوير عيوني، نرجع لكم بشيء أقوى قريباً!**", parse_mode="Markdown")
        return

    if uid in db.get("banned", []):
        await update.message.reply_text("🚫 **عذراً يا الغالي، حسابك محظور من استخدام خدمات البوت.**", parse_mode="Markdown")
        return

    referrer = None
    gift_token = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("gift_"):
            gift_token = arg[5:]          # رابط هدية:  ?start=gift_TOKEN
        else:
            try:
                referrer = int(arg)       # رابط دعوة:  ?start=USER_ID
            except ValueError:
                pass

    is_new = str(uid) not in db["users"]
    db_user = ensure_user(update, referrer)

    if is_new and referrer and referrer != uid and str(referrer) in db["users"]:
        try:
            await context.bot.send_message(
                chat_id=referrer,
                text=f"🎉 دخل شخص جديد عن طريق رابطك! حصلت على +{db['settings']['referral_points']} نقاط."
            )
        except Exception:
            pass

    if not await check_forced_sub(update, context):
        # نحتفظ برابط الهدية حتى يشترك العضو وبعدين نسلّمه الهدية تلقائياً
        if gift_token:
            db_user["pending_gift"] = gift_token
            save_db()
        return

    if gift_token:
        await redeem_gift_link(update, context, gift_token)

    await update.message.reply_text(
        welcome_text(uid, db_user),
        parse_mode="Markdown",
        reply_markup=main_keyboard(db_user["points"], uid)
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    db_user = ensure_user(update)
    await update.message.reply_text(
        "✅ تم إلغاء العملية.",
        reply_markup=main_keyboard(db_user["points"], user.id) if db_user else None
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    if uid == OWNER_ID:
        await update.message.reply_text(
            "👑 **أهلاً بك يا المالك! تم فتح لوحة التحكم.**",
            parse_mode="Markdown",
            reply_markup=owner_keyboard()
        )
    else:
        await update.message.reply_text("❌ هذا الأمر مخصص لمالك البوت فقط.")


# ----------------------------- الطلبات -----------------------------

async def process_order(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_arg, count_arg, free=False):
    user = update.effective_user
    if not user:
        return False
    uid = str(user.id)
    db_user = ensure_user(update)

    username = parse_channel(channel_arg)
    if not username or not str(count_arg).isdigit() or int(count_arg) <= 0:
        await reply(
            update,
            "❌ صيغة الطلب خاطئة!\n\nيرجى الإرسال بالشكل التالي:\n`/order @sheu3i 10000`",
            parse_mode="Markdown"
        )
        return False

    members_requested = int(count_arg)
    min_m = db["settings"]["min_order_members"]

    if not free and members_requested < min_m:
        await reply(update, f"❌ أقل عدد أعضاء للتمويل هو `{min_m}` عضو.", parse_mode="Markdown")
        return False

    cost_per_member = db["settings"]["order_cost"]
    total_cost = 0 if free else members_requested * cost_per_member

    if not free and db_user["points"] < total_cost:
        await reply(
            update,
            f"❌ رصيدك غير كافٍ!\n\n📌 المطلوب: `{total_cost}` نقطة.\n💎 رصيدك الحالي: `{db_user['points']}` نقطة.",
            parse_mode="Markdown"
        )
        return False

    try:
        me = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat_id=f"@{username}", user_id=me.id)
        if bot_member.status not in ("administrator", "creator"):
            raise ValueError("bot is not admin")
    except Exception:
        await reply(
            update,
            f"❌ ما گدرت أتحقق من القناة `@{username}`.\n\n"
            "تأكد من اليوزر، وضيف البوت **مشرف (Admin)** بالقناة، وبعدين كرر الطلب.",
            parse_mode="Markdown"
        )
        return False

    key = username.lower()
    owner_val = None if free else int(uid)
    existing = db["channels"].get(key)

    if existing and existing.get("owner") != owner_val:
        await reply(update, "❌ هذي القناة عليها طلب تمويل فعال لشخص آخر.")
        return False

    if not free:
        db["users"][uid]["points"] -= total_cost

    if existing:
        existing["target_count"] += members_requested
    else:
        db["channels"][key] = {
            "title": f"@{username}",
            "username": f"@{username}",
            "url": f"https://t.me/{username}",
            "target_count": members_requested,
            "current_count": 0,
            "owner": owner_val
        }
    save_db()

    if free:
        await reply(
            update,
            f"✅ **تمت إضافة قناة التجميع:** `@{username}`\n👥 العدد المطلوب: `{members_requested}`",
            parse_mode="Markdown"
        )
    else:
        await reply(
            update,
            f"✅ **تم إنشاء طلب التمويل بنجاح!**\n\n"
            f"📢 القناة: `@{username}`\n"
            f"👥 عدد الأعضاء المطلوب: `{members_requested}`\n"
            f"💰 النقاط الخصمة: `{total_cost}`\n"
            f"💎 المتبقي برصيدك: `{db['users'][uid]['points']}` نقطة.\n\n"
            f"تم إدراج القناة الآن في قسم **⭐ تجميع النقاط** ليراها جميع الأعضاء!",
            parse_mode="Markdown",
            reply_markup=main_keyboard(db["users"][uid]["points"], user.id)
        )
    return True


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, context):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ صيغة الطلب خاطئة!\n\nيرجى الإرسال بالشكل التالي:\n`/order @sheu3i 10000`",
            parse_mode="Markdown"
        )
        return
    await process_order(update, context, context.args[0], context.args[1])


async def complete_order(context: ContextTypes.DEFAULT_TYPE, key):
    ch = db["channels"].pop(key, None)
    if not ch:
        return

    db["total_orders_completed"] = db.get("total_orders_completed", 0) + 1

    for u in db["users"].values():
        if key in u.get("joined_channels", []):
            u["joined_channels"].remove(key)

    owner = ch.get("owner")
    if owner and str(owner) in db["users"]:
        hist = db["users"][str(owner)].setdefault("completed_orders", [])
        hist.append({"channel": ch["username"], "count": ch["target_count"], "time": int(time.time())})
        del hist[:-20]
    save_db()

    if owner:
        try:
            await context.bot.send_message(
                chat_id=int(owner),
                text=f"✅ اكتمل طلب تمويل قناتك {ch['username']} بنجاح!\n👥 العدد: {ch['target_count']}"
            )
        except Exception:
            pass

    proofs = db["settings"].get("proofs_channel")
    if proofs:
        try:
            await context.bot.send_message(
                chat_id=proofs,
                text=f"✅ تم اكتمال طلب تمويل جديد!\n\n📢 القناة: {ch['username']}\n👥 العدد: {ch['target_count']}"
            )
        except Exception as e:
            logging.warning(f"تعذر النشر بقناة الإثباتات: {e}")


# ----------------------------- التحويل -----------------------------

async def process_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE, target_arg, amount_arg):
    user = update.effective_user
    if not user:
        return False
    uid = str(user.id)
    db_user = ensure_user(update)

    if not str(target_arg).isdigit() or not str(amount_arg).isdigit():
        await reply(update, "❌ الصيغة خاطئة! استخدم:\n`/transfer الآيدي العدد`", parse_mode="Markdown")
        return False

    target_id = str(int(target_arg))
    amount = int(amount_arg)

    if amount <= 0:
        await reply(update, "❌ يجب إدخال عدد نقاط أكبر من 0.")
        return False

    if target_id == uid:
        await reply(update, "❌ ما تگدر تحول نقاط لنفسك.")
        return False

    if db_user["points"] < amount:
        await reply(update, "❌ رصيدك الحالي لا يكفي لإتمام هذه العملية.")
        return False

    if target_id not in db["users"]:
        await reply(update, "❌ هذا المستخدم غير مسجل بالبوت.")
        return False

    fee_percent = db["settings"].get("transfer_fee_percent", 10)
    fee = int(amount * (fee_percent / 100))
    final_amount = amount - fee

    if final_amount <= 0:
        await reply(update, "❌ المبلغ صغير جداً بعد خصم العمولة.")
        return False

    db["users"][uid]["points"] -= amount
    db["users"][target_id]["points"] += final_amount
    save_db()

    await reply(
        update,
        f"✅ **تم تحويل `{final_amount}` نقطة بنجاح إلى المستخدم `{target_id}`.**\n(العمولة الخصمة: `{fee}` نقطة)",
        parse_mode="Markdown",
        reply_markup=main_keyboard(db["users"][uid]["points"], user.id)
    )

    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text=f"🎁 وصلتك تحويلة بقيمة +{final_amount} نقاط من المستخدم {uid}!"
        )
    except Exception:
        pass
    return True


async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, context):
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ الصيغة خاطئة! استخدم:\n`/transfer الآيدي العدد`", parse_mode="Markdown")
        return
    await process_transfer(update, context, context.args[0], context.args[1])


# ----------------------------- الهدايا -----------------------------
# نظامين منفصلين:
#   1) كود هدية  : db["gifts"]      → العضو يكتبه (زر 💳 ادخال كود هدية أو /gift CODE)
#   2) رابط هدية : db["gift_links"] → العضو يفتحه (t.me/البوت?start=gift_TOKEN)

GIFT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # بدون حروف متشابهة (O/0, I/1)

GIFT_ERRORS = {
    #            كود                                            رابط
    "invalid": ("❌ **كود الهدية غير صحيح.**",                    "❌ **رابط الهدية غير صالح أو تم حذفه.**"),
    "expired": ("⌛ **انتهت صلاحية كود الهدية هذا.**",             "⌛ **انتهت صلاحية رابط الهدية هذا.**"),
    "used":    ("⚠️ **لقد استخدمت كود الهدية هذا مسبقاً!**",       "⚠️ **لقد استلمت هدية هذا الرابط مسبقاً!**"),
    "full":    ("😔 **وصل الكود للحد الأقصى من الاستخدامات.**",    "😔 **وصل الرابط للحد الأقصى من الاستخدامات.**"),
}


def gift_active(g):
    if len(g.get("used_by", [])) >= g.get("max_uses", 0):
        return False
    exp = g.get("expires", 0)
    return not (exp and time.time() > exp)


def fmt_expiry(g):
    exp = g.get("expires", 0)
    if not exp:
        return "بدون انتهاء"
    return datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")


def parse_gift_args(text):
    """'النقاط عدد_المستخدمين [المدة_بالساعات]'  →  (pts, uses, hours) أو None"""
    parts = text.split()
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    pts, uses = int(parts[0]), int(parts[1])
    hours = int(parts[2]) if len(parts) == 3 else 0
    if pts <= 0 or uses <= 0:
        return None
    return pts, uses, hours


def create_gift(store, key, pts, uses, hours):
    now = int(time.time())
    store[key] = {
        "points": pts,
        "max_uses": uses,
        "used_by": [],
        "created": now,
        "expires": now + hours * 3600 if hours else 0,
    }
    save_db()


def new_gift_code():
    while True:
        code = "".join(secrets.choice(GIFT_ALPHABET) for _ in range(8))
        if code not in db["gifts"]:
            return code


def new_gift_token():
    while True:
        token = secrets.token_urlsafe(9)
        if token not in db["gift_links"]:
            return token


def claim_gift(store, raw_key, uid, ignore_case):
    """يتحقق ويسلّم الهدية. يرجع (الحالة, النقاط)."""
    raw_key = raw_key.strip()
    key = None
    if ignore_case:
        for k in store:
            if k.lower() == raw_key.lower():
                key = k
                break
    elif raw_key in store:
        key = raw_key

    if key is None:
        return "invalid", 0
    g = store[key]
    exp = g.get("expires", 0)
    if exp and time.time() > exp:
        return "expired", 0
    if uid in g["used_by"]:
        return "used", 0
    if len(g["used_by"]) >= g["max_uses"]:
        return "full", 0

    g["used_by"].append(uid)
    db["users"][uid]["points"] += g["points"]
    save_db()
    return "ok", g["points"]


async def _gift_reply(update, status, pts, is_link):
    user = update.effective_user
    if status == "ok":
        await reply(
            update,
            f"🎉 **مبروك! استلمت هدية بقيمة +{pts} نقاط.**",
            parse_mode="Markdown",
            reply_markup=main_keyboard(db["users"][str(user.id)]["points"], user.id)
        )
    else:
        await reply(update, GIFT_ERRORS[status][1 if is_link else 0], parse_mode="Markdown")


async def redeem_gift(update: Update, context: ContextTypes.DEFAULT_TYPE, code):
    """استلام هدية عن طريق الكود."""
    user = update.effective_user
    if not user:
        return
    ensure_user(update)
    status, pts = claim_gift(db["gifts"], code, str(user.id), ignore_case=True)
    await _gift_reply(update, status, pts, is_link=False)


async def redeem_gift_link(update: Update, context: ContextTypes.DEFAULT_TYPE, token):
    """استلام هدية عن طريق الرابط."""
    user = update.effective_user
    if not user:
        return
    ensure_user(update)
    status, pts = claim_gift(db["gift_links"], token, str(user.id), ignore_case=False)
    await _gift_reply(update, status, pts, is_link=True)


async def cmd_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, context):
        return
    if not context.args:
        await update.message.reply_text("❌ يرجى إدخال الكود مثل: `/gift CODE`", parse_mode="Markdown")
        return
    await redeem_gift(update, context, context.args[0])


def gifts_content(bot_username):
    lines = ["📋 **الهدايا الفعالة**\n"]
    btns = []
    sections = (
        ("🎁 الأكواد", db["gifts"], "c"),
        ("🔗 الروابط", db["gift_links"], "l"),
    )
    for label, store, tag in sections:
        active = [(k, g) for k, g in store.items() if gift_active(g)]
        lines.append(f"{label}: `{len(active)}` فعال / `{len(store) - len(active)}` منتهي")
        for k, g in active[:10]:
            shown = k if tag == "c" else f"https://t.me/{bot_username}?start=gift_{k}"
            lines.append(
                f"• `{shown}`\n   💎 {g['points']} | 👥 {len(g['used_by'])}/{g['max_uses']} | ⏳ {fmt_expiry(g)}"
            )
            btns.append([InlineKeyboardButton(f"🗑 حذف {k[:12]}", callback_data=f"gdel:{tag}:{k}")])
        lines.append("")
    btns.append([InlineKeyboardButton("🧹 حذف المنتهية والمستهلكة", callback_data="gdel:clean")])
    return "\n".join(lines), InlineKeyboardMarkup(btns)


async def cb_gifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.from_user.id != OWNER_ID:
        await q.answer("❌ للمالك فقط.", show_alert=True)
        return

    parts = q.data.split(":", 2)
    if parts[1] == "clean":
        for store in (db["gifts"], db["gift_links"]):
            for k in [k for k, g in store.items() if not gift_active(g)]:
                del store[k]
        msg = "🧹 تم حذف المنتهية"
    else:
        store = db["gifts"] if parts[1] == "c" else db["gift_links"]
        if store.pop(parts[2], None) is None:
            msg = "⚠️ غير موجودة"
        else:
            msg = "🗑 تم الحذف"
    save_db()
    await q.answer(msg)

    text, markup = gifts_content(context.application.bot_data.get("username", "BOT"))
    try:
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=markup,
                                  disable_web_page_preview=True)
    except Exception:
        pass


# ============================ تجميع النقاط ============================

def build_collect(uid):
    user = db["users"][uid]
    s = db["settings"]
    items = [
        (k, c) for k, c in db["channels"].items()
        if k not in user.get("joined_channels", []) and str(c.get("owner")) != uid
    ]
    if not items:
        return "😔 **ما أكو قنوات متاحة حالياً.**\nارجع بعد شوية وراح تلكه قنوات جديدة 🔥", None

    buttons = []
    for k, c in items[:5]:
        buttons.append([
            InlineKeyboardButton(f"📢 {c['title']}", url=c["url"]),
            InlineKeyboardButton("✅ تحقق", callback_data=f"chk:{k}")
        ])

    text = (
        "⭐ **تجميع النقاط**\n\n"
        f"انضم للقناة ثم اضغط (✅ تحقق) حتى تستلم **+{s['join_reward']}** نقاط لكل قناة.\n\n"
        f"⚠️ إذا طلعت من القناة بعدين راح ينخصم منك `{s['unsub_penalty']}` نقاط."
    )
    return text, InlineKeyboardMarkup(buttons)


async def penalize_left(context: ContextTypes.DEFAULT_TYPE, uid):
    u = db["users"].get(str(uid))
    if not u:
        return 0
    penalty = db["settings"]["unsub_penalty"]
    total = 0
    changed = False

    for key in list(u.get("joined_channels", [])):
        ch = db["channels"].get(key)
        if not ch:
            u["joined_channels"].remove(key)
            changed = True
            continue
        try:
            still = await is_member(context.bot, ch["username"], int(uid))
        except Exception:
            continue
        if not still:
            u["joined_channels"].remove(key)
            ch["current_count"] = max(0, ch["current_count"] - 1)
            deducted = min(u["points"], penalty)
            u["points"] -= deducted
            total += deducted
            changed = True

    if changed:
        save_db()
    return total


async def cb_check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    uid_int = q.from_user.id
    uid = str(uid_int)

    if uid_int in db["banned"]:
        await q.answer("🚫 حسابك محظور.", show_alert=True)
        return
    if db["maintenance"] and uid_int != OWNER_ID:
        await q.answer("🛠️ البوت تحت الصيانة.", show_alert=True)
        return

    user = ensure_user(update)
    key = q.data.split(":", 1)[1]
    ch = db["channels"].get(key)

    if not ch:
        await q.answer("⚠️ هذا الطلب انتهى أو غير متوفر.", show_alert=True)
        return
    if key in user["joined_channels"]:
        await q.answer("✅ استلمت نقاط هذي القناة من قبل.", show_alert=True)
        return
    if str(ch.get("owner")) == uid:
        await q.answer("❌ ما تگدر تنضم لقناتك.", show_alert=True)
        return

    try:
        joined = await is_member(context.bot, ch["username"], uid_int)
    except Exception:
        await q.answer("⚠️ ما گدرت أتحقق، القناة مو جاهزة حالياً.", show_alert=True)
        return

    if not joined:
        await q.answer("❌ بعدك ما منضم للقناة! انضم أول وبعدين اضغط تحقق.", show_alert=True)
        return

    reward = db["settings"]["join_reward"]
    user["points"] += reward
    user["joined_channels"].append(key)
    get_daily(user)["joins"] += 1
    ch["current_count"] += 1
    done = ch["current_count"] >= ch["target_count"]
    save_db()

    await q.answer(f"✅ تم! حصلت على +{reward} نقاط", show_alert=False)

    if done:
        await complete_order(context, key)

    text, markup = build_collect(uid)
    try:
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        pass


# ============================ المهام اليومية ============================

def task_content(user):
    s = db["settings"]
    d = get_daily(user)
    need = s["daily_task_joins"]
    bonus = s["daily_task_bonus"]

    text = (
        "🎯 **المهام اليومية**\n\n"
        f"📌 المهمة: انضم لـ `{need}` قنوات من قسم ⭐ تجميع نقاط\n"
        f"📊 تقدمك اليوم: `{min(d['joins'], need)}/{need}`\n"
        f"🎁 المكافأة: `+{bonus}` نقاط"
    )
    markup = None
    if d["claimed"]:
        text += "\n\n✅ **استلمت مكافأة اليوم، ارجع باجر!**"
    elif d["joins"] >= need:
        text += "\n\n🎉 **كملت المهمة! اضغط لاستلام مكافأتك.**"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎁 استلام المكافأة", callback_data="claim_task")]])
    return text, markup


async def cb_claim_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.from_user.id in db["banned"]:
        await q.answer("🚫 حسابك محظور.", show_alert=True)
        return

    user = ensure_user(update)
    d = get_daily(user)
    need = db["settings"]["daily_task_joins"]
    bonus = db["settings"]["daily_task_bonus"]

    if d["claimed"]:
        await q.answer("✅ استلمت مكافأة اليوم من قبل.", show_alert=True)
        return
    if d["joins"] < need:
        await q.answer("⚠️ بعدك ما كملت المهمة.", show_alert=True)
        return

    user["points"] += bonus
    d["claimed"] = True
    save_db()
    await q.answer(f"🎉 مبروك +{bonus} نقاط!", show_alert=True)

    text, markup = task_content(user)
    try:
        await q.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        pass


# ============================ كولباك الاشتراك الإجباري ============================

async def cb_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    uid = q.from_user.id

    if uid in db["banned"]:
        await q.answer("🚫 حسابك محظور.", show_alert=True)
        return

    await q.answer()
    if await check_forced_sub(update, context):
        user = ensure_user(update)

        pending = user.pop("pending_gift", None)
        if pending:
            save_db()
            await redeem_gift_link(update, context, pending)

        try:
            await q.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=uid,
            text=welcome_text(uid, user),
            parse_mode="Markdown",
            reply_markup=main_keyboard(user["points"], uid)
        )


# ============================ لوحة المالك ============================

def settings_content():
    s = db["settings"]
    lines = ["⚙️ **الأسعار والمكافآت الحالية:**\n"]
    btns = []
    for k, label in SETTING_LABELS.items():
        lines.append(f"{label}: `{s[k]}`")
        btns.append([InlineKeyboardButton(label, callback_data=f"set:{k}")])
    lines.append("\nاضغط على الخيار اللي تريد تعدله 👇")
    return "\n".join(lines), InlineKeyboardMarkup(btns)


def forced_content():
    chans = db.get("forced_channels", [])
    lines = ["🔒 **إدارة الاشتراك الإجباري**\n"]
    btns = []
    if chans:
        for i, ch in enumerate(chans):
            lines.append(f"{i + 1}. {clean(ch['title'])}")
            btns.append([InlineKeyboardButton(f"🗑️ حذف: {ch['title']}", callback_data=f"forced_del:{i}")])
    else:
        lines.append("ماكو قنوات اشتراك إجباري حالياً.")
    btns.append([InlineKeyboardButton("➕ إضافة قناة", callback_data="forced_add")])
    return "\n".join(lines), InlineKeyboardMarkup(btns)


async def cb_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.from_user.id != OWNER_ID:
        await q.answer("❌ للمالك فقط.", show_alert=True)
        return
    key = q.data.split(":", 1)[1]
    if key not in SETTING_LABELS:
        await q.answer()
        return
    context.user_data.clear()
    context.user_data["waiting_for"] = f"setting:{key}"
    await q.answer()
    await q.message.reply_text(
        f"{SETTING_LABELS[key]}\nالقيمة الحالية: `{db['settings'][key]}`\n\nأرسل القيمة الجديدة (رقم فقط):",
        parse_mode="Markdown"
    )


async def cb_forced(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.from_user.id != OWNER_ID:
        await q.answer("❌ للمالك فقط.", show_alert=True)
        return

    if q.data == "forced_add":
        context.user_data.clear()
        context.user_data["waiting_for"] = "add_forced"
        await q.answer()
        await q.message.reply_text(
            "🔒 **أرسل يوزر القناة:** `@channel`\n\n"
            "للقناة الخاصة أرسل: `-100123456789 https://t.me/+رابط_الدعوة`\n\n"
            "⚠️ لازم يكون البوت مشرف بالقناة.",
            parse_mode="Markdown"
        )
        return

    if q.data.startswith("forced_del:"):
        try:
            idx = int(q.data.split(":", 1)[1])
            removed = db["forced_channels"].pop(idx)
            save_db()
            await q.answer(f"🗑️ تم حذف {removed['title']}")
        except Exception:
            await q.answer("⚠️ تعذر الحذف.", show_alert=True)
        text, markup = forced_content()
        try:
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass


# ============================ تتبّع الكروبات والقنوات ============================
# البوت يسجّل تلقائياً أي كروب/قناة ينضاف لها (عن طريق my_chat_member)،
# ويحذفها من القائمة إذا انطرد أو غادر، وينبّه المالك بكل حالة.

def member_can_post(chat_type, member):
    """True/False = يگدر ينشر أو لا، None = البوت مو عضو أصلاً."""
    st = member.status
    if st in ("left", "kicked"):
        return None
    if chat_type == "channel":
        return st == "creator" or (st == "administrator" and bool(getattr(member, "can_post_messages", False)))
    if st == "restricted":
        return bool(getattr(member, "can_send_messages", True))
    return True


def register_chat(chat, can_post=None):
    """يسجّل/يحدّث كروب أو قناة. يرجع True إذا كانت جديدة."""
    if chat.type == "private":
        return False
    key = str(chat.id)
    ctype = "channel" if chat.type == "channel" else "group"
    info = db["chats"].get(key)
    is_new = info is None
    before = None if is_new else dict(info)
    if is_new:
        info = {"id": chat.id, "added": int(time.time()), "can_post": True}
        db["chats"][key] = info
    info["title"] = chat.title or info.get("title") or key
    info["type"] = ctype
    info["username"] = chat.username or ""
    if can_post is not None:
        info["can_post"] = can_post
    if is_new or before != info:
        save_db()
    return is_new


def migrate_chat(old_id, new_id):
    info = db["chats"].pop(str(old_id), None)
    if info:
        info["id"] = new_id
        info["type"] = "group"
        db["chats"][str(new_id)] = info
        save_db()


async def notify_owner(context, text):
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=text)
    except Exception:
        pass


async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.my_chat_member
    if not m:
        return
    chat = m.chat
    status = m.new_chat_member.status

    # العضو حظر البوت أو رفع الحظر
    if chat.type == "private":
        u = db["users"].get(str(chat.id))
        if u is not None:
            u["blocked"] = status in ("kicked", "left")
            save_db()
        return

    label = "قناة" if chat.type == "channel" else "كروب"
    title = chat.title or chat.id

    if status in ("left", "kicked"):
        if db["chats"].pop(str(chat.id), None) is not None:
            save_db()
            await notify_owner(context, f"➖ البوت طلع/انطرد من {label}: {title}")
        return

    ctype = "channel" if chat.type == "channel" else "group"
    can_post = member_can_post(ctype, m.new_chat_member)
    is_new = register_chat(chat, can_post)
    if is_new:
        warn = "" if can_post else "\n⚠️ بس البوت ما عنده صلاحية نشر هناك."
        await notify_owner(context, f"✅ انضاف البوت إلى {label}: {title}\n🆔 {chat.id}{warn}")


async def passive_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """احتياط: يسجّل الكروبات/القنوات القديمة اللي البوت كان بيها قبل التحديث."""
    chat = update.effective_chat
    if chat and chat.type != "private":
        register_chat(chat)


def chats_content():
    chats = list(db["chats"].values())
    groups = [c for c in chats if c.get("type") != "channel"]
    chans = [c for c in chats if c.get("type") == "channel"]
    bad = [c for c in chats if not c.get("can_post", True)]
    lines = [
        "🗂 الكروبات والقنوات المسجلة للإذاعة\n",
        f"👥 كروبات: {len(groups)} | 📢 قنوات: {len(chans)} | ⚠️ بدون صلاحية نشر: {len(bad)}\n",
    ]
    for i, c in enumerate(chats[:40], 1):
        icon = "📢" if c.get("type") == "channel" else "👥"
        ok = "✅" if c.get("can_post", True) else "⚠️"
        un = f" @{c['username']}" if c.get("username") else ""
        lines.append(f"{i}. {icon} {c.get('title', '?')}{un} {ok}")
    if len(chats) > 40:
        lines.append(f"... و {len(chats) - 40} آخرين")
    if not chats:
        lines.append("ما أكو أي كروب أو قناة مسجلة.\nضيف البوت لأي كروب، أو كمشرف بالقناة، وراح يتسجل تلقائياً.")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث وتنظيف", callback_data="chats_refresh")]])
    return "\n".join(lines), markup


async def refresh_chats(bot, report_chat):
    """يتحقق من كل كروب/قناة، يحدّث الاسم والصلاحيات، ويحذف اللي البوت طلع منها."""
    me = await bot.get_me()
    removed = 0
    for key, c in list(db["chats"].items()):
        try:
            chat = await bot.get_chat(c["id"])
            member = await bot.get_chat_member(c["id"], me.id)
            ctype = "channel" if chat.type == "channel" else "group"
            can_post = member_can_post(ctype, member)
            if can_post is None:
                db["chats"].pop(key, None)
                removed += 1
            else:
                c.update(title=chat.title or c.get("title"), type=ctype,
                         username=chat.username or "", can_post=can_post)
        except RetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 30) + 1)
        except ChatMigrated as e:
            migrate_chat(c["id"], e.new_chat_id)
        except (Forbidden, BadRequest):
            db["chats"].pop(key, None)
            removed += 1
        except TelegramError:
            pass
        await asyncio.sleep(0.05)
    save_db()
    text, markup = chats_content()
    try:
        await bot.send_message(chat_id=report_chat, text=f"🔄 تم التحديث. المحذوف (غير موجود): {removed}\n\n{text}",
                               reply_markup=markup)
    except Exception:
        pass


async def cb_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.from_user.id != OWNER_ID:
        await q.answer("❌ للمالك فقط.", show_alert=True)
        return
    await q.answer("⏳ جاري الفحص...")
    context.application.create_task(refresh_chats(context.bot, q.message.chat_id))


# ============================ نظام الإذاعة (أعضاء + كروبات + قنوات) ============================

BC_LABELS = {"users": "👤 الأعضاء", "groups": "👥 الكروبات", "channels": "📢 القنوات"}


def broadcast_targets(kind):
    """يرجع (قائمة الأهداف [(chat_id, نوع)], عدد المتخطّى لعدم وجود صلاحية نشر)."""
    targets, skipped = [], 0
    if kind in ("users", "all"):
        for uid, u in db["users"].items():
            if u.get("blocked") or int(uid) in db["banned"]:
                continue
            targets.append((int(uid), "users"))
    if kind in ("groups", "channels", "all"):
        for c in db["chats"].values():
            t = "channels" if c.get("type") == "channel" else "groups"
            if kind != "all" and kind != t:
                continue
            if not c.get("can_post", True):
                skipped += 1
                continue
            targets.append((c["id"], t))
    return targets, skipped


def broadcast_menu():
    n_all = {k: len(broadcast_targets(k)[0]) for k in ("users", "groups", "channels", "all")}
    skipped = broadcast_targets("all")[1]
    text = (
        "📣 **اختر الجهة اللي تريد تذيع إلها:**\n\n"
        f"👤 أعضاء البوت: `{n_all['users']}`\n"
        f"👥 كروبات: `{n_all['groups']}`\n"
        f"📢 قنوات: `{n_all['channels']}`\n"
    )
    if skipped:
        text += f"\n⚠️ `{skipped}` كروب/قناة ما عند البوت صلاحية نشر بيها (راح تنتخطى)."
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👤 الأعضاء ({n_all['users']})", callback_data="bc:users")],
        [InlineKeyboardButton(f"👥 الكروبات ({n_all['groups']})", callback_data="bc:groups"),
         InlineKeyboardButton(f"📢 القنوات ({n_all['channels']})", callback_data="bc:channels")],
        [InlineKeyboardButton(f"🌐 الكل ({n_all['all']})", callback_data="bc:all")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="bc:cancel")],
    ])
    return text, markup


def apply_outcome(t, chat_id, status):
    if t == "users":
        u = db["users"].get(str(chat_id))
        if u is not None:
            if status == "blocked":
                u["blocked"] = True
            elif status == "ok" and u.get("blocked"):
                u["blocked"] = False
        return
    key = str(chat_id)
    c = db["chats"].get(key)
    if not c:
        return
    if status == "gone":
        db["chats"].pop(key, None)
    elif status == "blocked":
        c["can_post"] = False
    elif status == "ok":
        c["can_post"] = True


async def send_one(bot, chat_id, from_chat, msg_id):
    """نسخ الرسالة لجهة واحدة. يرجع (الحالة, آيدي الجهة): ok / blocked / gone / fail"""
    for _ in range(3):
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=from_chat, message_id=msg_id)
            return "ok", chat_id
        except RetryAfter as e:                     # ضغط من تلكرام: ننتظر ونعيد
            await asyncio.sleep(min(e.retry_after, 60) + 1)
        except ChatMigrated as e:                   # الكروب تحوّل لسوبر كروب
            migrate_chat(chat_id, e.new_chat_id)
            chat_id = e.new_chat_id
        except Forbidden:                           # حظر البوت / انطرد / ما عنده صلاحية
            return "blocked", chat_id
        except BadRequest as e:
            m = str(e).lower()
            if "chat not found" in m:
                return "gone", chat_id
            if "rights" in m or "not a member" in m or "kicked" in m:
                return "blocked", chat_id
            return "fail", chat_id
        except TelegramError:                       # مشكلة شبكة وقتية
            await asyncio.sleep(1)
    return "fail", chat_id


def bc_progress_text(done, total, stats):
    lines = [f"📣 **جاري الإذاعة...** `{done}/{total}`\n"]
    for t, label in BC_LABELS.items():
        ok, fail = stats[t]
        if ok or fail:
            lines.append(f"{label}: ✅ `{ok}` | ❌ `{fail}`")
    return "\n".join(lines)


async def run_broadcast(app, from_chat, msg_id, kind, status_msg):
    bot = app.bot
    targets, skipped = broadcast_targets(kind)
    total = len(targets)
    stats = {t: [0, 0] for t in BC_LABELS}
    stop_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف الإذاعة", callback_data="bc_stop")]])
    app.bot_data["bc_stop"] = False
    app.bot_data["bc_running"] = True
    stopped = False
    done = 0

    try:
        for chat_id, t in targets:
            if app.bot_data.get("bc_stop"):
                stopped = True
                break
            status, real_id = await send_one(bot, chat_id, from_chat, msg_id)
            apply_outcome(t, real_id, status)
            stats[t][0 if status == "ok" else 1] += 1
            done += 1

            if done % 25 == 0:
                try:
                    await status_msg.edit_text(bc_progress_text(done, total, stats),
                                               parse_mode="Markdown", reply_markup=stop_markup)
                except Exception:
                    pass
            await asyncio.sleep(0.05)          # ~20 رسالة بالثانية، أقل من حد تلكرام (30)
    finally:
        app.bot_data["bc_running"] = False
        save_db()

    title = "⏹ **تم إيقاف الإذاعة**" if stopped else "✅ **تمت الإذاعة بنجاح!**"
    lines = [title, f"\n📊 تمت معالجة `{done}` من `{total}`\n"]
    total_ok = total_fail = 0
    for t, label in BC_LABELS.items():
        ok, fail = stats[t]
        total_ok += ok
        total_fail += fail
        if ok or fail:
            lines.append(f"{label}: ✅ `{ok}` | ❌ `{fail}`")
    lines.append(f"\n✅ نجح: `{total_ok}`  |  ❌ فشل: `{total_fail}`")
    if skipped:
        lines.append(f"⚠️ تم تخطي `{skipped}` كروب/قناة بدون صلاحية نشر")
    lines.append("\n🧹 الأعضاء اللي حظروا البوت والكروبات المحذوفة انشالت تلقائياً من القوائم.")
    try:
        await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception:
        try:
            await bot.send_message(chat_id=OWNER_ID, text="\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """المالك أرسل الرسالة المراد إذاعتها (أي نوع: نص/صورة/فيديو/ملف...)."""
    msg = update.message
    context.user_data.clear()
    context.user_data["bc_msg"] = (msg.chat_id, msg.message_id)
    text, markup = broadcast_menu()
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def cb_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.from_user.id != OWNER_ID:
        await q.answer("❌ للمالك فقط.", show_alert=True)
        return

    if q.data == "bc_stop":
        context.application.bot_data["bc_stop"] = True
        await q.answer("⏹ جاري الإيقاف...")
        return

    kind = q.data.split(":", 1)[1]
    if kind == "cancel":
        context.user_data.pop("bc_msg", None)
        await q.answer("تم الإلغاء")
        try:
            await q.message.edit_text("❌ تم إلغاء الإذاعة.")
        except Exception:
            pass
        return

    if context.application.bot_data.get("bc_running"):
        await q.answer("⏳ توجد إذاعة شغالة حالياً، انتظر تنتهي أو أوقفها.", show_alert=True)
        return

    saved = context.user_data.pop("bc_msg", None)
    if not saved:
        await q.answer("⚠️ انتهت الجلسة، أعد إرسال الرسالة من زر الإذاعة.", show_alert=True)
        return

    targets, _ = broadcast_targets(kind)
    if not targets:
        await q.answer("⚠️ ما أكو أي جهة بهذا القسم.", show_alert=True)
        context.user_data["bc_msg"] = saved
        return

    await q.answer("🚀 بدأت الإذاعة")
    status_msg = await q.message.edit_text(
        f"📣 **جاري الإذاعة...** `0/{len(targets)}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف الإذاعة", callback_data="bc_stop")]])
    )
    context.application.create_task(
        run_broadcast(context.application, saved[0], saved[1], kind, status_msg)
    )


async def handle_owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    text = message.text
    user = update.effective_user
    if not user:
        return
    uid = user.id
    context.user_data.clear()

    if text in ("👑 لوحة تحكم المالك", "إذاعة"):
        await message.reply_text("👑 **لوحة تحكم المالك**", parse_mode="Markdown", reply_markup=owner_keyboard())

    elif text == "🔙 الرجوع للقائمة الرئيسية":
        db_user = ensure_user(update)
        await message.reply_text(
            "🏠 **القائمة الرئيسية**",
            parse_mode="Markdown",
            reply_markup=main_keyboard(db_user["points"], uid)
        )

    elif text == "📊 إحصائيات البوت":
        users_cnt = len(db.get("users", {}))
        channels_cnt = len(db.get("channels", {}))
        groups_cnt = sum(1 for c in db["chats"].values() if c.get("type") != "channel")
        chans_cnt = sum(1 for c in db["chats"].values() if c.get("type") == "channel")
        blocked_cnt = sum(1 for u in db["users"].values() if u.get("blocked"))
        gift_links_cnt = sum(1 for g in db["gift_links"].values() if gift_active(g))
        gifts_cnt = sum(1 for g in db["gifts"].values() if gift_active(g))
        total_pts = sum(u.get("points", 0) for u in db["users"].values())
        await message.reply_text(
            f"📊 **إحصائيات البوت:**\n\n"
            f"👥 عدد المسجلين بالخاص: `{users_cnt}` (حاظرين البوت: `{blocked_cnt}`)\n"
            f"👥 الكروبات المسجلة: `{groups_cnt}`\n"
            f"📢 القنوات المسجلة: `{chans_cnt}`\n"
            f"📢 قنوات التجميع القائمة: `{channels_cnt}`\n"
            f"✅ الطلبات المكتملة: `{db.get('total_orders_completed', 0)}`\n"
            f"🎁 أكواد الهدايا الفعالة: `{gifts_cnt}`\n"
            f"🔗 روابط الهدايا الفعالة: `{gift_links_cnt}`\n"
            f"🚫 المحظورين: `{len(db.get('banned', []))}`\n"
            f"💎 مجموع النقاط عند الأعضاء: `{total_pts}`",
            parse_mode="Markdown"
        )

    elif text == "🛠️ تفعيل/تعطيل الصيانة":
        db["maintenance"] = not db.get("maintenance", False)
        save_db()
        st = "مفعلة 🛠️" if db["maintenance"] else "معطلة ✅"
        await message.reply_text(f"حالة الصيانة الآن: **{st}**", parse_mode="Markdown")

    elif text == "🎁 إنشاء كود هدية":
        context.user_data["waiting_for"] = "make_gift_code"
        await message.reply_text(
            "🎁 **إنشاء كود هدية** (العضو يكتبه داخل البوت)\n\n"
            "أرسل: `النقاط  عدد_المستخدمين  [المدة_بالساعات]`\n"
            "• `50 10` ⇦ 50 نقطة لأول 10 أشخاص (بدون انتهاء)\n"
            "• `50 10 24` ⇦ نفس الشي بس ينتهي بعد 24 ساعة",
            parse_mode="Markdown"
        )

    elif text == "🔗 إنشاء رابط هدية":
        context.user_data["waiting_for"] = "make_gift_link"
        await message.reply_text(
            "🔗 **إنشاء رابط هدية** (العضو يفتح الرابط ويستلم الهدية تلقائياً)\n\n"
            "أرسل: `النقاط  عدد_المستخدمين  [المدة_بالساعات]`\n"
            "• `50 10` ⇦ 50 نقطة لأول 10 أشخاص (بدون انتهاء)\n"
            "• `50 10 24` ⇦ نفس الشي بس ينتهي بعد 24 ساعة",
            parse_mode="Markdown"
        )

    elif text == "📋 الهدايا الفعالة":
        t, m = gifts_content(context.application.bot_data.get("username", "BOT"))
        await message.reply_text(t, parse_mode="Markdown", reply_markup=m, disable_web_page_preview=True)

    elif text == "🗂 الكروبات والقنوات":
        t, m = chats_content()
        await message.reply_text(t, reply_markup=m)

    elif text == "📢 قناة إثباتات التمويل":
        context.user_data["waiting_for"] = "proofs_channel"
        await message.reply_text(
            "📢 **أرسل معرف القناة (معرفها أو يوزرها) الخاصة بالإثباتات:**\nمثال: `@proofs_channel`\n\n⚠️ لازم يكون البوت مشرف بالقناة.",
            parse_mode="Markdown"
        )

    elif text == "⚙️ تعديل الأسعار والمكافآت":
        t, m = settings_content()
        await message.reply_text(t, parse_mode="Markdown", reply_markup=m)

    elif text == "🔒 إدارة الاشتراك الإجباري":
        t, m = forced_content()
        await message.reply_text(t, parse_mode="Markdown", reply_markup=m)

    elif text == "➕ إضافة نقاط لعضو":
        context.user_data["waiting_for"] = "add_points"
        await message.reply_text("➕ أرسل: `الآيدي النقاط`\nمثال: `123456789 50`", parse_mode="Markdown")

    elif text == "➖ خصم نقاط من عضو":
        context.user_data["waiting_for"] = "sub_points"
        await message.reply_text("➖ أرسل: `الآيدي النقاط`\nمثال: `123456789 50`", parse_mode="Markdown")

    elif text == "📢 إضافة قناة تجميع":
        context.user_data["waiting_for"] = "add_channel"
        await message.reply_text(
            "📢 أرسل: `@channel العدد`\nمثال: `@mychannel 500`\n\n(بدون خصم نقاط - لازم البوت مشرف بالقناة)",
            parse_mode="Markdown"
        )

    elif text == BROADCAST_BTN:
        context.user_data["waiting_for"] = "broadcast"
        await message.reply_text(
            "📣 **أرسل الآن الرسالة اللي تريد تذيعها**\n"
            "(نص، صورة، فيديو، ملف، ... أي نوع)\n\n"
            "بعدها تختار تذيع للأعضاء أو الكروبات أو القنوات أو الكل.\n"
            "للإلغاء: /cancel",
            parse_mode="Markdown"
        )

    elif text == "🌐 ربط موقع الرشق (API)":
        context.user_data["waiting_for"] = "smm_url"
        await message.reply_text(
            f"🌐 الرابط الحالي:\n`{db['settings']['smm_url']}`\n\nأرسل رابط الـ API الجديد:",
            parse_mode="Markdown"
        )

    elif text == "🚫 حظر/فك حظر عضو":
        context.user_data["waiting_for"] = "ban_toggle"
        await message.reply_text("🚫 أرسل آيدي العضو (إذا محظور راح ينفك حظره):")

    elif text == "💾 نسخة احتياطية (Backup)":
        save_db()
        try:
            await message.reply_document(
                document=DB_FILE.read_bytes(),
                filename="database_backup.json",
                caption="💾 نسخة احتياطية من قاعدة البيانات"
            )
        except Exception as e:
            await message.reply_text(f"❌ تعذر إرسال النسخة: {e}")


async def handle_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE, text):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    mode = context.user_data.get("waiting_for")

    if mode == "gift_code":
        context.user_data.clear()
        await redeem_gift(update, context, text)
        return

    if mode == "transfer":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ أرسل: `الآيدي العدد`\nمثال: `123456789 50`", parse_mode="Markdown")
            return
        context.user_data.clear()
        await process_transfer(update, context, parts[0], parts[1])
        return

    if mode == "order":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ أرسل: `@channel العدد`\nمثال: `@mychannel 100`", parse_mode="Markdown")
            return
        context.user_data.clear()
        await process_order(update, context, parts[0], parts[1])
        return

    if uid != OWNER_ID:
        context.user_data.clear()
        return

    s = db["settings"]

    if mode == "proofs_channel":
        clean_ch = text.strip()
        if not clean_ch.startswith("@") and not clean_ch.startswith("-100"):
            clean_ch = f"@{clean_ch}"
        s["proofs_channel"] = clean_ch
        save_db()
        context.user_data.clear()
        await update.message.reply_text(f"✅ **تم تعيين قناة الإثباتات إلى:** `{clean_ch}`", parse_mode="Markdown")

    elif mode in ("make_gift_code", "make_gift_link"):
        parsed = parse_gift_args(text)
        if not parsed:
            await update.message.reply_text(
                "❌ صيغة خاطئة! أرسل: `النقاط عدد_المستخدمين [المدة_بالساعات]`\nمثال: `50 10` أو `50 10 24`",
                parse_mode="Markdown"
            )
            return
        pts, uses, hours = parsed
        context.user_data.clear()
        exp_txt = f"{hours} ساعة" if hours else "بدون انتهاء"

        if mode == "make_gift_code":
            code = new_gift_code()
            create_gift(db["gifts"], code, pts, uses, hours)
            await update.message.reply_text(
                f"🎁 **تم إنشاء كود الهدية بنجاح!**\n\n"
                f"🔑 الكود: `{code}`\n"
                f"💎 النقاط: `{pts}`\n"
                f"👥 عدد المستخدمين: `{uses}`\n"
                f"⏳ الصلاحية: `{exp_txt}`\n\n"
                f"📌 يستلمه العضو من زر (💳 ادخال كود هدية) أو بالأمر:\n`/gift {code}`",
                parse_mode="Markdown"
            )
        else:
            token = new_gift_token()
            create_gift(db["gift_links"], token, pts, uses, hours)
            username = context.application.bot_data.get("username", "BOT")
            link = f"https://t.me/{username}?start=gift_{token}"
            await update.message.reply_text(
                f"🔗 **تم إنشاء رابط الهدية بنجاح!**\n\n"
                f"`{link}`\n\n"
                f"💎 النقاط: `{pts}`\n"
                f"👥 عدد المستخدمين: `{uses}`\n"
                f"⏳ الصلاحية: `{exp_txt}`\n\n"
                f"📌 أي شخص يفتح الرابط ويضغط Start يستلم الهدية تلقائياً.",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )

    elif mode and mode.startswith("setting:"):
        key = mode.split(":", 1)[1]
        if key not in SETTING_LABELS or not text.isdigit():
            await update.message.reply_text("❌ أرسل رقم صحيح فقط.")
            return
        val = int(text)
        if key == "transfer_fee_percent" and val > 100:
            await update.message.reply_text("❌ النسبة لازم تكون بين 0 و 100.")
            return
        s[key] = val
        save_db()
        context.user_data.clear()
        await update.message.reply_text(f"✅ تم تحديث {SETTING_LABELS[key]} إلى `{val}`", parse_mode="Markdown")

    elif mode == "add_forced":
        parts = text.split()
        ref = parts[0]
        if re.fullmatch(r"-?\d+", ref):
            ref = int(ref)
        elif not ref.startswith("@"):
            ref = f"@{ref}"
        try:
            chat = await context.bot.get_chat(ref)
            if chat.username:
                url = f"https://t.me/{chat.username}"
            elif len(parts) > 1:
                url = parts[1]
            elif chat.invite_link:
                url = chat.invite_link
            else:
                await update.message.reply_text("❌ القناة خاصة، أرسل معها رابط الدعوة:\n`-100123456789 https://t.me/+xxxx`", parse_mode="Markdown")
                return
            db["forced_channels"].append({"chat_id": chat.id, "title": chat.title or str(chat.id), "url": url})
            save_db()
            context.user_data.clear()
            await update.message.reply_text(f"✅ تمت إضافة **{clean(chat.title)}** للاشتراك الإجباري.", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ تعذر الوصول للقناة. تأكد من اليوزر وإن البوت مشرف.\n\n{e}")

    elif mode in ("add_points", "sub_points"):
        parts = text.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text("❌ أرسل: `الآيدي النقاط`", parse_mode="Markdown")
            return
        tid, amt = str(int(parts[0])), int(parts[1])
        if tid not in db["users"]:
            await update.message.reply_text("❌ هذا المستخدم غير مسجل بالبوت.")
            return
        if mode == "add_points":
            db["users"][tid]["points"] += amt
            note = f"🎁 أضاف لك المالك +{amt} نقاط!"
            done = f"✅ تمت إضافة `{amt}` نقطة للعضو `{tid}`."
        else:
            db["users"][tid]["points"] = max(0, db["users"][tid]["points"] - amt)
            note = f"⚠️ تم خصم {amt} نقاط من رصيدك."
            done = f"✅ تم خصم `{amt}` نقطة للعضو `{tid}`."
        save_db()
        context.user_data.clear()
        await update.message.reply_text(f"{done}\n💎 رصيده الآن: `{db['users'][tid]['points']}`", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=int(tid), text=note)
        except Exception:
            pass

    elif mode == "add_channel":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ أرسل: `@channel العدد`", parse_mode="Markdown")
            return
        context.user_data.clear()
        await process_order(update, context, parts[0], parts[1], free=True)

    elif mode == "smm_url":
        s["smm_url"] = text.strip()
        save_db()
        context.user_data["waiting_for"] = "smm_key"
        await update.message.reply_text("✅ تم حفظ الرابط.\n\nأرسل الآن مفتاح الـ API (API Key):")

    elif mode == "smm_key":
        s["smm_key"] = text.strip()
        save_db()
        context.user_data.clear()
        try:
            await update.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=uid, text="✅ تم حفظ مفتاح الـ API.")

    elif mode == "ban_toggle":
        if not text.isdigit():
            await update.message.reply_text("❌ أرسل الآيدي أرقام فقط.")
            return
        tid = int(text)
        if tid == OWNER_ID:
            await update.message.reply_text("❌ ما تگدر تحظر نفسك.")
            return
        context.user_data.clear()
        if tid in db["banned"]:
            db["banned"].remove(tid)
            msg = f"✅ تم فك الحظر عن `{tid}`"
        else:
            db["banned"].append(tid)
            msg = f"🚫 تم حظر `{tid}`"
        save_db()
        await update.message.reply_text(msg, parse_mode="Markdown")


# ============================ أزرار المستخدمين ============================

async def handle_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    suid = str(uid)
    db_user = ensure_user(update)
    s = db["settings"]

    if text.startswith(POINTS_LABEL):
        await update.message.reply_text(
            f"💎 رصيدك الحالي: `{db_user['points']}` نقطة",
            parse_mode="Markdown",
            reply_markup=main_keyboard(db_user["points"], uid)
        )

    elif text == "🎁 الهدية اليومية":
        current_time = time.time()
        last_gift = db_user.get("last_gift", 0)
        cooldown = 86400

        if current_time - last_gift < cooldown:
            rem = int(cooldown - (current_time - last_gift))
            h, m = rem // 3600, (rem % 3600) // 60
            await update.message.reply_text(
                f"⌛ **استلمت الهدية اليومية مسبقاً يا الغالي!**\nارتاح وارجع أخذها بعد: `{h}` ساعة و `{m}` دقيقة.",
                parse_mode="Markdown"
            )
        else:
            gift = s["daily_gift"]
            db_user["points"] += gift
            db_user["last_gift"] = current_time
            save_db()
            await update.message.reply_text(
                f"🎉 **مبروك! استلمت الهدية اليومية +{gift} نقاط.**",
                parse_mode="Markdown",
                reply_markup=main_keyboard(db_user["points"], uid)
            )

    elif text == "🎡 عجلة الحظ اليومية":
        current_time = time.time()
        last_wheel = db_user.get("last_wheel", 0)
        cooldown = 86400

        if current_time - last_wheel < cooldown:
            rem = int(cooldown - (current_time - last_wheel))
            h, m = rem // 3600, (rem % 3600) // 60
            await update.message.reply_text(
                f"⌛ **جربت حظك اليوم مسبقاً!**\nتعال جرب حظك مرة ثانية بعد: `{h}` ساعة و `{m}` دقيقة.",
                parse_mode="Markdown"
            )
        else:
            win_points = random.choice([2, 5, 10, 15, 20, 25, 50])
            db_user["points"] += win_points
            db_user["last_wheel"] = current_time
            save_db()
            await update.message.reply_text(
                f"🎉 **مبروك! ربحت +{win_points} نقاط من عجلة الحظ!**",
                parse_mode="Markdown",
                reply_markup=main_keyboard(db_user["points"], uid)
            )

    elif text == "🔔 رابط الدعوة (+نقاط)":
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={uid}"

        top_users_sorted = sorted(
            db["users"].values(),
            key=lambda x: x.get("referrals", 0),
            reverse=True
        )[:5]

        top_text = ""
        for idx, u in enumerate(top_users_sorted, 1):
            top_text += f"{idx}. {clean(u.get('name', 'عضو'))} ⇦ عدد الدعوات: ({u.get('referrals', 0)})\n"
        if not top_text:
            top_text = "لا توجد بيانات حالياً."

        msg = (
            "✨ **أهلاً بك في نظام مشاركة الرابط ورتب المتصدرين!**\n\n"
            "🔗 **رابط الدعوة الخاص بك:**\n"
            f"`{ref_link}`\n\n"
            f"🎁 **المكافأة:** لكل شخص يدخل عبر رابطك تحصل على **+{s['referral_points']} نقاط**!\n"
            f"📊 **عدد الأشخاص الذين دعوتهم:** `{db_user.get('referrals', 0)}` شخص\n\n"
            "🏆 **قائمة أكثر الأعضاء مشاركة (TOP 5):**\n"
            f"{top_text}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif text == "💳 ادخال كود هدية":
        context.user_data["waiting_for"] = "gift_code"
        await update.message.reply_text("💳 **أرسل كود الهدية الآن:**", parse_mode="Markdown")

    elif text == "🚀 طلب تمويل قناتك":
        context.user_data["waiting_for"] = "order"
        await update.message.reply_text(
            "🚀 **طلب تمويل قناتك**\n\n"
            f"💰 سعر العضو الواحد: `{s['order_cost']}` نقطة\n"
            f"👥 أقل عدد: `{s['min_order_members']}` عضو\n"
            f"💎 رصيدك: `{db_user['points']}` نقطة\n\n"
            "1️⃣ ضيف البوت **مشرف (Admin)** بقناتك\n"
            "2️⃣ أرسل الآن: `@channel العدد`\n"
            "مثال: `@mychannel 100`\n\n"
            "أو استخدم الأمر: `/order @mychannel 100`",
            parse_mode="Markdown"
        )

    elif text == "⭐ تجميع نقاط":
        lost = await penalize_left(context, suid)
        if lost:
            await update.message.reply_text(
                f"⚠️ طلعت من بعض القنوات، انخصم منك `{lost}` نقاط.",
                parse_mode="Markdown"
            )
        t, m = build_collect(suid)
        await update.message.reply_text(t, parse_mode="Markdown", reply_markup=m)

    elif text == "🎯 المهام اليومية (+نقاط)":
        t, m = task_content(db_user)
        await update.message.reply_text(t, parse_mode="Markdown", reply_markup=m)

    elif text == "🔀 تحويل نقاط":
        context.user_data["waiting_for"] = "transfer"
        await update.message.reply_text(
            "🔀 **تحويل نقاط**\n\n"
            f"عمولة التحويل: `{s['transfer_fee_percent']}%`\n"
            f"💎 رصيدك: `{db_user['points']}` نقطة\n\n"
            "أرسل الآن: `الآيدي العدد`\n"
            "مثال: `123456789 50`\n\n"
            "أو استخدم الأمر: `/transfer الآيدي العدد`",
            parse_mode="Markdown"
        )

    elif text == "💰 شحن نقاطك":
        await update.message.reply_text(
            "💰 **شحن النقاط**\n\n"
            f"لشحن رصيدك تواصل مع [المالك](tg://user?id={OWNER_ID}) مباشرة.\n\n"
            "نقبل كروت آسيا، زين كاش، وطرق دفع أخرى.",
            parse_mode="Markdown"
        )

    elif text == "📢 قنواتنا والدعم الفني":
        await update.message.reply_text(
            "📢 **القنوات والدعم الفني**\n\n"
            f"👤 MALEK BOT: [اضغط هنا](tg://user?id={OWNER_ID})\n"
            "💬 للتواصل والأفكار والدعم الفني راسلنا عبر المالك.",
            parse_mode="Markdown"
        )

    elif text == "👤 حسابي والمعلومات":
        await update.message.reply_text(
            f"👤 **معلومات حسابك:**\n\n"
            f"🆔 الآيدي: `{uid}`\n"
            f"💎 النقاط: `{db_user['points']}`\n"
            f"🔔 دعواتك: `{db_user.get('referrals', 0)}`\n"
            f"📢 القنوات اللي اشتركت بيها: `{len(db_user.get('joined_channels', []))}`",
            parse_mode="Markdown"
        )

    elif text == "📊 متابعة طلبيات التمويل":
        active = [c for c in db["channels"].values() if str(c.get("owner")) == suid]
        if not active:
            await update.message.reply_text("📊 **ما عندك أي طلب تمويل نشط حالياً.**", parse_mode="Markdown")
        else:
            lines = ["📊 **طلبات التمويل النشطة مالتك:**\n"]
            for c in active:
                lines.append(f"📢 القناة: {c['username']}\n👥 المنجز: `{c['current_count']}/{c['target_count']}`\n")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ============================ الموجه الرئيسي ============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return

    # المالك أرسل رسالة الإذاعة (أي نوع: صورة/فيديو/ملف/نص)
    if user.id == OWNER_ID and context.user_data.get("waiting_for") == "broadcast":
        await receive_broadcast(update, context)
        return

    if not msg.text:
        return
    text = msg.text.strip()

    if not await gate(update, context):
        return

    if context.user_data.get("waiting_for"):
        await handle_waiting(update, context, text)
        return

    if text in OWNER_BTNS and user.id == OWNER_ID:
        await handle_owner_panel(update, context)
        return

    if text in USER_BTNS or text.startswith(POINTS_LABEL):
        await handle_user_button(update, context, text)
        return

    db_user = ensure_user(update)
    if db_user:
        await update.message.reply_text(
            "❓ **المعذرة يا الغالي، ما فهمت قصدك.**\nاختر من الأزرار جوة 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard(db_user["points"], user.id)
        )


# ============================ التشغيل ============================

async def post_init(app: Application):
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username
    logging.info(f"Bot username: @{me.username}")


def main():
    if not TOKEN:
        sys.exit("❌ لازم تضبط متغير البيئة BOT_TOKEN قبل التشغيل (مثال: export BOT_TOKEN=xxxx)")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    private = filters.ChatType.PRIVATE

    # الأوامر (بالخاص فقط حتى لا يزعج الكروبات)
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=private))
    app.add_handler(CommandHandler("order", cmd_order, filters=private))
    app.add_handler(CommandHandler("transfer", cmd_transfer, filters=private))
    app.add_handler(CommandHandler("gift", cmd_gift, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))

    # تتبّع إضافة/إزالة البوت من الكروبات والقنوات
    app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))

    # الأزرار الشفافة (Inline)
    app.add_handler(CallbackQueryHandler(cb_recheck, pattern="^recheck_sub$"))
    app.add_handler(CallbackQueryHandler(cb_check_join, pattern="^chk:"))
    app.add_handler(CallbackQueryHandler(cb_claim_task, pattern="^claim_task$"))
    app.add_handler(CallbackQueryHandler(cb_setting, pattern="^set:"))
    app.add_handler(CallbackQueryHandler(cb_forced, pattern="^forced_"))
    app.add_handler(CallbackQueryHandler(cb_broadcast, pattern=r"^bc[:_]"))
    app.add_handler(CallbackQueryHandler(cb_gifts, pattern="^gdel:"))
    app.add_handler(CallbackQueryHandler(cb_chats, pattern="^chats_refresh$"))

    # رسائل الخاص (نص + وسائط للمالك عند الإذاعة)
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.StatusUpdate.ALL, handle_message))

    # احتياط: تسجيل الكروبات/القنوات القديمة عند أي نشاط فيها (لا يرد على أحد)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS | filters.ChatType.CHANNEL, passive_register),
        group=1
    )

    logging.info("🚀 Bot started successfully!")
    # my_chat_member لازم يُطلب صراحةً حتى يوصل للبوت
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()