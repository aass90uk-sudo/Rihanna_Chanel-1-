import os
import asyncio
import json
import hashlib
import re
import unicodedata
from datetime import datetime, time as dtime
from pathlib import Path

import httpx
from pytz import timezone
from hijri_converter import Gregorian
from telegram import Bot
from telegram.ext import Application, ContextTypes


# ============================================================
# الإعدادات البيئية
# ============================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or os.getenv("VITE_SUPABASE_URL")
    or ""
).rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

LOCAL_TZ = timezone("Asia/Riyadh")
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
    return "\n\n🖤 صدقة جارية لأمـي جنتي «الأندلسية» غ ـفر الله لها."


def _clean_post_text(text: str) -> str:
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = re.sub(r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"([\u064B-\u065F\u0670])\1+", r"\1", cleaned)
    cleaned = re.sub(r"(?<![\u0621-\u064A])[\u064B-\u065F\u0670]+(?![\u0621-\u064A])", "", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def content_hash(text: str) -> str:
    normalized = " ".join(_clean_post_text(text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ============================================================
# الذاكرة الدائمة (Supabase)
# ============================================================

async def _supabase_claim_post(h_hash: str, prompt_type: str, content: str) -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("ذاكرة Supabase الدائمة غير مضبوطة في Railway.")

    url = f"{SUPABASE_URL}/rest/v1/rpc/claim_post_content"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "p_content_hash": h_hash,
        "p_prompt_type": prompt_type,
        "p_content": content,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code != 200:
                raise RuntimeError(f"فشل حجز المنشور في الذاكرة: {response.status_code}")
            result = response.json()
            return result is True
    except (httpx.HTTPError, ValueError) as error:
        raise RuntimeError(f"تعذر حجز المنشور في الذاكرة: {error}") from error


async def _supabase_mark_posted(h_hash: str):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("ذاكرة Supabase الدائمة غير مضبوطة في Railway.")

    url = f"{SUPABASE_URL}/rest/v1/posted_history"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.patch(
                url,
                headers=headers,
                params={"content_hash": f"eq.{h_hash}"},
                json={"status": "posted", "posted_at": datetime.utcnow().isoformat()},
            )
            if response.status_code not in (200, 204):
                raise RuntimeError(f"فشل تثبيت حالة المنشور: {response.status_code}")
    except httpx.HTTPError as error:
        raise RuntimeError(f"تعذر تثبيت حالة المنشور: {error}") from error


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
                    "status": "eq.posted",
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
        "اكتب النص plain text فقط دون نجوم أو علامات Markdown أو عناوين مزخرفة. "
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
    return _clean_post_text(value) if isinstance(value, str) and value.strip() else None


# ============================================================
# Grok
# ============================================================

def _grok_output_text(response_data: dict) -> str:
    direct_text = response_data.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    choices = response_data.get("choices", [])
    if not isinstance(choices, list):
        return ""

    text_parts = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {})
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            text_parts.append(message["content"])

    return "".join(text_parts).strip()


def _grok_chat(
    prompt_text: str,
    system_instruction: str | None = None,
    max_output_tokens: int = 2048,
) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("المتغير GROQ_API_KEY غير مضبوط في Railway.")

    input_messages = []
    if system_instruction:
        input_messages.append({"role": "system", "content": system_instruction})
    input_messages.append({"role": "user", "content": prompt_text})

    payload = {
        "model": GROQ_MODEL,
        "messages": input_messages,
        "temperature": 0.9,
        "max_tokens": max_output_tokens,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=120) as client:
            response = client.post(GROQ_API_URL, headers=headers, json=payload)

            if response.status_code in (400, 404):
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = {}

                error_details = error_body.get("error") if isinstance(error_body, dict) else None
                if isinstance(error_details, dict) and error_details.get("code") == "model_not_found":
                    available_response = client.get(GROQ_MODELS_URL, headers=headers)
                    if available_response.status_code == 200:
                        available_data = available_response.json()
                        available_items = available_data.get("data", []) if isinstance(available_data, dict) else []
                        available_ids = {
                            item.get("id")
                            for item in available_items
                            if isinstance(item, dict) and isinstance(item.get("id"), str)
                        }
                        preferred_ids = [
                            "llama-3.1-8b-instant",
                            "llama-3.3-70b-versatile",
                            "openai/gpt-oss-20b",
                            "openai/gpt-oss-120b",
                        ]
                        fallback_model = next(
                            (model_id for model_id in preferred_ids if model_id in available_ids),
                            None,
                        )
                        if fallback_model and fallback_model != payload["model"]:
                            payload["model"] = fallback_model
                            response = client.post(GROQ_API_URL, headers=headers, json=payload)
    except httpx.HTTPError as error:
        raise RuntimeError(f"تعذر الاتصال بخدمة Groq: {error}") from error

    if response.status_code >= 400:
        try:
            error_body = response.json()
        except ValueError:
            error_body = response.text[:300]
        raise RuntimeError(f"فشل Groq ({response.status_code}): {error_body}")

    try:
        response_data = response.json()
    except ValueError as error:
        raise RuntimeError("أعاد Groq استجابة غير صالحة.") from error

    text = _grok_output_text(response_data)
    if not text:
        raise RuntimeError("أعاد Groq نصاً فارغاً.")

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

        text = _clean_post_text(text)
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
                print(f"⚠️ تعذر Groq ({error})، سيتم استخدام المحتوى الجاهز لـ {prompt_type}.")
                return fallback
        raise


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

        text = _clean_post_text(await generate_content(prompt_type))

        post_text = (
            f"{get_hijri_date()}\n\n"
            f"{text}"
            f"{footer()}"
        )

        post_hash = content_hash(text)
        claimed = await _supabase_claim_post(post_hash, prompt_type, text)
        if not claimed:
            print(f"⚠️ تم منع منشور مكرر قبل الإرسال: {label}")
            return False

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
        )

        await _supabase_mark_posted(post_hash)

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

    print("✅ البوت جاهز ويعمل بجدولة المنشورات فقط دون نشر فوري عند الإقلاع.")


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

    print("✅ البوت مخصص للنشر المجدول فقط، ولن يرد على رسائل المجموعة.")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
