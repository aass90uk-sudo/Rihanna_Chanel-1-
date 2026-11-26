import os
import asyncio
import json
import hashlib
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import httpx
from pytz import timezone
from hijri_converter import Gregorian
from telegram import Update, Bot
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, filters, ContextTypes


# ============================================================
# الإعدادات البيئية
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.7")
XAI_API_URL = "https://api.x.ai/v1/responses"
DISCUSSION_GROUP_ID = os.getenv("DISCUSSION_GROUP_ID")

SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or os.getenv("VITE_SUPABASE_URL")
    or ""
).rstrip("/")
SUPABASE_SERVICE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("VITE_SUPABASE_ANON_KEY")
)

LOCAL_TZ = timezone("Asia/Riyadh")
STATE_FILE = Path(".bot_state.json")
CONTENT_FILE = Path("content.json")

# الطول المطلوب للمنشور: وسط تقريباً (1200 حرف)
POST_MIN_CHARS = 1100
POST_MAX_CHARS = 1300
POST_TARGET_CHARS = 1200


# ============================================================
# أدوات مساعدة
# ============================================================

def get_hijri_date():
    now_local = datetime.now(LOCAL_TZ)
    hijri = Gregorian(
        now_local.year,
        now_local.month,
        now_local.day
    ).to_hijri()

    months = [
        "محرم",
        "صفر",
        "ربيع الأول",
        "ربيع الآخر",
        "جمادى الأولى",
        "جمادى الآخرة",
        "رجب",
        "شعبان",
        "رمضان",
        "شوال",
        "ذو القعدة",
        "ذو الحجة",
    ]

    return f"📅 {hijri.day} {months[hijri.month - 1]} {hijri.year} هـ"


def footer():
    return "\n\n🖤 صدقة جارية للأخت «الأندلسية» غفر الله لها."


def content_hash(text: str) -> str:
    # نطبّع النص قليلاً حتى لا تتخطى التكرارات بفروق مسافات بسيطة
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ============================================================
# الذاكرة الدائمة (Supabase)
# ============================================================

async def _supabase_insert_history(h_hash: str, prompt_type: str, content: str):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("⚠️ Supabase غير مضبوط، لن تُحفظ المنشورات في الذاكرة الدائمة.")
        return

    url = f"{SUPABASE_URL}/rest/v1/posted_history"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "content_hash": h_hash,
        "prompt_type": prompt_type,
        "content": content,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code not in (200, 201):
                print(f"⚠️ فشل حفظ المنشور في الذاكرة: {response.status_code} {response.text[:200]}")
    except Exception as error:
        print(f"⚠️ خطأ أثناء حفظ المنشور في الذاكرة: {error}")


async def _supabase_known_hashes(prompt_type: str) -> set:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return set()

    url = f"{SUPABASE_URL}/rest/v1/posted_history"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                headers=headers,
                params={
                    "select": "content_hash",
                    "prompt_type": f"eq.{prompt_type}",
                },
            )
            if response.status_code != 200:
                print(f"⚠️ فشل قراءة الذاكرة الدائمة: {response.status_code}")
                return set()
            data = response.json()
            return {row["content_hash"] for row in data if isinstance(row, dict) and row.get("content_hash")}
    except Exception as error:
        print(f"⚠️ خطأ أثناء قراءة الذاكرة الدائمة: {error}")
        return set()


async def _supabase_has_history() -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        state = _read_state()
        return bool(state.get("first_run_done"))

    url = f"{SUPABASE_URL}/rest/v1/posted_history"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Range": "0",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                headers=headers,
                params={"select": "id", "limit": "1"},
            )
            if response.status_code == 200:
                return bool(response.json())
            print(f"⚠️ فشل التحقق من الذاكرة: {response.status_code}")
            return bool(_read_state().get("first_run_done"))
    except Exception as error:
        print(f"⚠️ خطأ أثناء التحقق من الذاكرة: {error}")
        return bool(_read_state().get("first_run_done"))


# ============================================================
# أوامر المحتوى
# ============================================================

def _post_system_instruction(prompt_type: str) -> str:
    story_rule = ""
    if prompt_type == "stories_masa":
        story_rule = (
            "قصة من التراث الأندلسي الصالح، "
            "ولا تذكر نسبها إلى الجزائر أو تسميها قصة المساء أو التراث الجزائري، "
        )

    return (
        "أنت كاتب إيماني بليغ وذو أسلوب شعري هادئ وعميق. "
        "اكتب منشوراً متكاملاً غير مبتور، "
        "بطول يتراوح بين 1100 و1300 حرف تقريباً (الهدف نحو 1200 حرف)، "
        "بحيث تكون آخر جملة مكتملة المعنى دون حشو أو إطناب. "
        "ابتعد عن التكرار والقوالب الجاهزة المستهلكة، "
        "وغيّر المقدمات والأساليب والصور التعبيرية في كل مرة، "
        "ولا تُلحق المنشور بأي تاريخ أو هجري أو خاتمة خارجية. "
        + story_rule
    )


PROMPTS = {
    "azkar_sabah":
        "اكتب منشوراً صباحياً يحتوي على أحد أذكار الصباح وفضلها بنبرة إيمانية دافئة وشرح موجز.",

    "stories_sabah":
        "اكتب قصة إسلامية جهادية مؤثرة وقعت في بلاد المغرب الإسلامي وعبرة مأثورة ملهمة، واختمها بـ (العبرة من القصة:).",

    "azkar_masa":
        "اكتب منشوراً مسائياً مؤثراً يحتوي على أحد أذكار المساء المأثورة وفضلها وشرح موجز لها.",

    "stories_masa":
        "اكتب قصة ملهمة من التراث الأندلسي القديم والصالحين في المغرب الأوسط والأندلس مليئة بالعبر والمواعظ، واختمها بـ (العبرة من القصة:).",

    "imaani_mouthza":
        "اكتب موعظة إيمانية شاعرية هادئة وعميقة تفيض فضفضة وطمأنينة، "
        "وفي خاتمتها دعاء لسكان الجزائر وأهلها بلمسة دزيرية أصيلة "
        "بأن اللهم اهد الجزائر وأهلها وأمتها إلى الخير والمحبة والسكينة، "
        "اجعلها كأنها همسة مساء دافئة لقلب متعب يلتفت إلى الله.",
}


# ============================================================
# المحتوى الاحتياطي وحالة التشغيل
# ============================================================

def _load_fallback_content():
    try:
        with CONTENT_FILE.open("r", encoding="utf-8") as content_file:
            content = json.load(content_file)
    except (OSError, json.JSONDecodeError) as error:
        print(f"⚠️ تعذر تحميل المحتوى الاحتياطي: {error}")
        return {}

    return content if isinstance(content, dict) else {}


def _fallback_content(prompt_type):
    values = _load_fallback_content().get(prompt_type, [])
    if not isinstance(values, list) or not values:
        return None

    today_index = datetime.now(LOCAL_TZ).timetuple().tm_yday % len(values)
    value = values[today_index]
    return value.strip() if isinstance(value, str) and value.strip() else None


def _read_state():
    try:
        with STATE_FILE.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return {}

    return state if isinstance(state, dict) else {}


def _write_state(state):
    temporary_file = STATE_FILE.with_suffix(".tmp")
    with temporary_file.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False)
    temporary_file.replace(STATE_FILE)


def _today_key():
    return datetime.now(LOCAL_TZ).date().isoformat()


# ============================================================
# Grok
# ============================================================

def _grok_output_text(response_data: dict) -> str:
    direct_text = response_data.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    output = response_data.get("output", [])
    if not isinstance(output, list):
        return ""

    text_parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])

    return "".join(text_parts).strip()


def _grok_chat(
    prompt_text: str,
    system_instruction: str | None = None,
    max_output_tokens: int = 2048,
) -> str:
    if not XAI_API_KEY:
        raise RuntimeError("المتغير XAI_API_KEY غير مضبوط في Railway.")

    input_messages = []
    if system_instruction:
        input_messages.append({"role": "system", "content": system_instruction})
    input_messages.append({"role": "user", "content": prompt_text})

    payload = {
        "model": XAI_MODEL,
        "input": input_messages,
        "temperature": 0.9,
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=120) as client:
            response = client.post(XAI_API_URL, headers=headers, json=payload)
    except httpx.HTTPError as error:
        raise RuntimeError(f"تعذر الاتصال بخدمة Grok: {error}") from error

    if response.status_code >= 400:
        try:
            error_body = response.json()
        except ValueError:
            error_body = response.text[:300]
        raise RuntimeError(f"فشل Grok ({response.status_code}): {error_body}")

    try:
        response_data = response.json()
    except ValueError as error:
        raise RuntimeError("أعاد Grok استجابة غير صالحة.") from error

    text = _grok_output_text(response_data)
    if not text:
        raise RuntimeError("أعاد Grok نصاً فارغاً.")

    return text


def _trim_to_complete_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_stop = max(cut.rfind("."), cut.rfind("؟"), cut.rfind("!"), cut.rfind("\n"))
    if last_stop > max_chars // 2:
        return cut[:last_stop + 1].strip()
    return cut.strip()


def _sync_generate_post(prompt_text, prompt_type, known_hashes):
    # لا يتم نشر أي منشور مقصوص أو مكرر.
    # إذا لم يكن الناتج مناسباً، تتم إعادة المحاولة.
    last_text = ""

    for attempt in range(1, 5):

        text = _grok_chat(
            prompt_text,
            system_instruction=_post_system_instruction(prompt_type),
            max_output_tokens=2048,
        )

        last_text = text
        text_length = len(text)

        if content_hash(text) in known_hashes:
            print(f"⚠️ منشور مكرر (محاولة {attempt}/4)، سيتم طلب نص جديد.")
            continue

        # المجال المقصود: 1100 إلى 1300 حرف تقريباً.
        if POST_MIN_CHARS <= text_length <= POST_MAX_CHARS:
            print(f"✅ منشور كامل جديد: {text_length} حرف | محاولة {attempt}")
            return text

        if POST_MAX_CHARS < text_length:
            trimmed = _trim_to_complete_sentence(text, POST_TARGET_CHARS)
            if POST_MIN_CHARS <= len(trimmed) <= POST_MAX_CHARS:
                print(f"✅ تم تشذيب منشور طويل إلى {len(trimmed)} حرف | محاولة {attempt}")
                return trimmed

        print(f"⚠️ طول غير مناسب: {text_length} حرف | محاولة {attempt}/4")

    # كمحل أخير، نُخرج أطول نص وصلنا إليه غير مكرر ومكتمل الجُمل.
    if content_hash(last_text) not in known_hashes:
        if len(last_text) > POST_MAX_CHARS:
            last_text = _trim_to_complete_sentence(last_text, POST_TARGET_CHARS)
        print(f"⚠️ إرجاع أفضل نص متاح بعد المحاولات: {len(last_text)} حرف.")
        return last_text

    raise RuntimeError(
        "تعذر إنشاء منشور جديد غير مكرر بالطول المطلوب. "
        f"آخر طول: {len(last_text)} حرف."
    )


async def generate_content(prompt_type):
    known_hashes = await _supabase_known_hashes(prompt_type)
    prompt_text = PROMPTS[prompt_type]

    try:
        return await asyncio.to_thread(
            _sync_generate_post,
            prompt_text,
            prompt_type,
            known_hashes,
        )
    except Exception as error:
        fallback = _fallback_content(prompt_type)
        if fallback:
            fallback_hash = content_hash(fallback)
            if fallback_hash not in known_hashes:
                print(f"⚠️ تعذر Grok ({error})، سيتم استخدام المحتوى الجاهز لـ {prompt_type}.")
                return fallback
        raise


# ============================================================
# الرد على التعليقات
# ============================================================

def _sync_fiqh_reply(user_name, user_text):

    system_instruction = (
        "أنت عالم ديني ربّاني وكاتب إيماني بليغ، "
        "ترد على أسئلة المسلمين بأدب وحكمة، "
        "وفق الكتاب والسنة دون اختلاق آيات أو أحاديث. "
        "ابدأ الرد بعبارة مناسبة مثل: "
        "'نعم أخي الموحد' أو 'نعم أختي الموحدة'. "
        "اكتب موعظة عن صبر الزوجات عن ازواجهن بسبب غيابهم للجهاد في سبيل الله مترابطة ومؤثرة، "
        "واجعلها مختصرة وغير مملة، "
        "بحيث يكون طولها بين 650 و800 حرف تقريباً. "
        "لا تكرر المقدمة، ولا تضف حشواً، "
        "واجعل آخر جملة مكتملة المعنى."
    )

    full_prompt = (
        f"{system_instruction}\n\n"
        f"السائل: {user_name or 'الأخ الموحد'}\n"
        f"السؤال: {user_text}"
    )

    last_text = ""

    # لا يتم نشر أي رد قصير أو مقطوع.
    # إذا لم يكن الناتج مناسباً، تتم إعادة المحاولة.
    for attempt in range(1, 5):

        text = _grok_chat(
            full_prompt,
            max_output_tokens=2048,
        )

        last_text = text
        text_length = len(text)

        # المجال المقصود: 650 إلى 800 حرف.
        if 650 <= text_length <= 800:
            print(
                f"✅ موعظة مكتملة: {text_length} حرف "
                f"| محاولة {attempt}"
            )
            return text

        print(
            f"⚠️ طول غير مناسب: {text_length} حرف "
            f"| محاولة {attempt}/4"
        )

    raise RuntimeError(
        "تعذر إنشاء موعظة مكتملة بالطول المطلوب. "
        f"آخر طول: {len(last_text)} حرف."
    )


# ============================================================
# إرسال المنشورات للقناة
# ============================================================

async def send_daily_post(
    bot: Bot,
    prompt_type: str,
    label: str
):
    try:
        print(f"📤 {label}...")

        text = await generate_content(prompt_type)

        post_text = (
            f"{get_hijri_date()}\n\n"
            f"{text}"
            f"{footer()}"
        )

        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text,
                parse_mode="Markdown",
            )

        except BadRequest as error:
            print(
                f"⚠️ تعذر تنسيق منشور {label} بـ Markdown، "
                f"سيتم إرساله كنص عادي: {error}"
            )

            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text
            )

        # التسجيل في الذاكرة الدائمة لضمان عدم التكرار مستقبلاً.
        await _supabase_insert_history(content_hash(text), prompt_type, text)

        print(f"✅ تم النشر: {label}")
        return True

    except Exception as error:
        print(f"❌ خطأ في النشر ({label}): {error}")
        return False


# ============================================================
# وظائف JobQueue
# ============================================================

async def job_azkar_sabah(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "azkar_sabah",
        "أذكار الصباح"
    )


async def job_stories_sabah(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "stories_sabah",
        "قصة الظهر"
    )


async def job_azkar_masa(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "azkar_masa",
        "أذكار المساء"
    )


async def job_stories_masa(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "stories_masa",
        "قصة الليل"
    )


# ============================================================
# الرد على التعليقات
# ============================================================

async def reply_to_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if (
        not message
        or not message.text
        or not message.from_user
        or message.from_user.is_bot
    ):
        return

    print(
        f"📩 رسالة واردة من "
        f"chat_id={message.chat_id} "
        f"message_id={message.message_id}"
    )

    user_text = message.text
    user_name = message.from_user.first_name

    try:

        reply = await asyncio.to_thread(
            _sync_fiqh_reply,
            user_name,
            user_text
        )

        # حماية إضافية: لا ننشر الرد إذا كان ناقصاً.
        if not reply or not (650 <= len(reply) <= 800):
            print(
                f"⚠️ تم رفض الرد قبل الإرسال: "
                f"{len(reply) if reply else 0} حرف"
            )
            return

        try:

            await message.reply_text(
                text=reply,
                parse_mode="Markdown"
            )

        except BadRequest as error:

            print(
                "⚠️ تعذر تنسيق الرد بـ Markdown، "
                f"سيتم إرساله كنص عادي: {error}"
            )

            await message.reply_text(
                text=reply
            )

        print(
            f"✅ تم نشر الموعظة كاملة "
            f"({len(reply)} حرف)"
        )

    except Exception as error:
        print(
            f"❌ خطأ في الرد على التعليق: {error}"
        )


# ============================================================
# ما بعد التهيئة
# ============================================================

async def post_init(application: Application):

    bot = application.bot
    jq = application.job_queue

    try:

        bot_info = await bot.get_me()

        channel = await bot.get_chat(
            CHANNEL_ID
        )

        channel_member = await bot.get_chat_member(
            channel.id,
            bot_info.id
        )

        print(
            f"✅ Telegram متصل: @{bot_info.username} | "
            f"القناة: {channel.title or channel.id} | "
            f"حالة البوت: {channel_member.status}"
        )

    except Exception as error:

        print(
            "⚠️ تعذر التحقق من اتصال Telegram "
            f"أو صلاحيات القناة: {error}"
        )

    # الوظائف اليومية الثابتة
    # توقيت مكة = Asia/Riyadh

    tz = LOCAL_TZ

    jq.run_daily(
        job_azkar_sabah,
        time=dtime(6, 0, tzinfo=tz),
        name="azkar_sabah"
    )

    jq.run_daily(
        job_stories_sabah,
        time=dtime(12, 0, tzinfo=tz),
        name="stories_sabah"
    )

    jq.run_daily(
        job_azkar_masa,
        time=dtime(17, 0, tzinfo=tz),
        name="azkar_masa"
    )

    jq.run_daily(
        job_stories_masa,
        time=dtime(22, 30, tzinfo=tz),
        name="stories_masa"
    )

    print("✅ تم تسجيل جميع الوظائف في JobQueue.")

    today = _today_key()
    state = _read_state()
    now_local = datetime.now(LOCAL_TZ)

    has_history = await _supabase_has_history()
    first_run_imaani_done = state.get("first_run_imaani_date") == today
    noon_posted_today = state.get("stories_sabah_date") == today

    # في أول تشغيل فقط: موعظة إيمانية شاعرية ودعاء لأهل الجزائر بلمسة دزيرية.
    # لا ترحيب ولا رسالة جدولة مثبتة.
    if not has_history and not first_run_imaani_done:
        print("📌 أول تشغيل: نشر الموعظة الإيمانية والدعاء لأهل الجزائر...")
        posted = await send_daily_post(bot, "imaani_mouthza", "الموعظة الإيمانية الأولى")
        if posted:
            state["first_run_imaani_date"] = today
            state["first_run_done"] = True
            _write_state(state)

    if now_local.time() >= dtime(12, 0) and not noon_posted_today:
        print("📌 استدراك قصة الظهر لليوم...")
        posted = await send_daily_post(bot, "stories_sabah", "قصة الظهر المستدركة")
        if posted:
            state["stories_sabah_date"] = today
            _write_state(state)

    print("✅ البوت جاهز ويعمل دون إرسال رسالة ترحيبية.")


# ============================================================
# نقطة الانطلاق
# ============================================================

def main():

    print("🚀 بدء تشغيل البوت...")

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    group_filter = filters.ChatType.GROUPS
    if DISCUSSION_GROUP_ID:
        group_filter &= filters.Chat(chat_id=int(DISCUSSION_GROUP_ID))

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & group_filter,
            reply_to_member,
        )
    )

    print("✅ البوت يستمع للرسائل...")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
