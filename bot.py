import os
import asyncio
import json
import hashlib
import re
import tempfile
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

LOCAL_TZ = timezone("Asia/Riyadh")
ALGERIA_TZ = timezone("Africa/Algiers")

CONTENT_FILE = Path("content.json")
VOLUME_MOUNT_PATH = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/data"))
MEMORY_FILE = VOLUME_MOUNT_PATH / "posted_history.json"
MEMORY_LOCK = asyncio.Lock()

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
    cleaned = re.sub(r"(?m)^#{1,6}\s*", "", cleaned)
    return cleaned.strip()


def content_hash(text: str) -> str:
    normalized = " ".join(_clean_post_text(text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ============================================================
# الذاكرة الدائمة على Railway Volume
# ============================================================
def _empty_memory():
    return {"version": 1, "posts": []}


def _load_memory():
    try:
        with MEMORY_FILE.open("r", encoding="utf-8") as memory_file:
            memory = json.load(memory_file)
    except FileNotFoundError:
        return _empty_memory()
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"تعذر قراءة ملف الذاكرة الدائمة: {error}") from error

    if not isinstance(memory, dict) or not isinstance(memory.get("posts"), list):
        raise RuntimeError("ملف الذاكرة الدائمة غير صالح.")
    return memory


def _save_memory(memory):
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix="posted_history-",
            suffix=".tmp",
            dir=MEMORY_FILE.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as memory_file:
            json.dump(memory, memory_file, ensure_ascii=False, indent=2)
            memory_file.flush()
            os.fsync(memory_file.fileno())
        os.replace(temporary_path, MEMORY_FILE)
    except OSError as error:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        raise RuntimeError(f"تعذر حفظ الذاكرة الدائمة داخل Volume: {error}") from error


async def _memory_has_posted_today(prompt_type: str) -> bool:
    today_key = f"{prompt_type}:{datetime.now(LOCAL_TZ).date().isoformat()}"
    async with MEMORY_LOCK:
        memory = _load_memory()
        return any(
            isinstance(record, dict)
            and record.get("claim_key") == today_key
            and record.get("status") == "posted"
            for record in memory["posts"]
        )


async def _memory_known_hashes(prompt_type: str) -> set[str]:
    async with MEMORY_LOCK:
        memory = _load_memory()
        return {
            record["content_hash"]
            for record in memory["posts"]
            if isinstance(record, dict)
            and record.get("prompt_type") == prompt_type
            and record.get("status") == "posted"
            and record.get("content_hash")
        }


async def _memory_claim_post(
    h_hash: str,
    prompt_type: str,
    content: str,
    claim_key: str,
) -> bool:
    async with MEMORY_LOCK:
        memory = _load_memory()

        already_claimed = any(
            isinstance(record, dict)
            and record.get("claim_key") == claim_key
            and record.get("status") in {"claimed", "posted"}
            for record in memory["posts"]
        )
        duplicate_content = any(
            isinstance(record, dict)
            and record.get("prompt_type") == prompt_type
            and record.get("content_hash") == h_hash
            and record.get("status") in {"claimed", "posted"}
            for record in memory["posts"]
        )
        if already_claimed or duplicate_content:
            return False

        memory["posts"].append(
            {
                "claim_key": claim_key,
                "content_hash": h_hash,
                "prompt_type": prompt_type,
                "content": content,
                "status": "claimed",
                "claimed_at": datetime.now(LOCAL_TZ).isoformat(),
            }
        )
        _save_memory(memory)
        return True


async def _memory_mark_posted(h_hash: str, prompt_type: str):
    async with MEMORY_LOCK:
        memory = _load_memory()
        for record in reversed(memory["posts"]):
            if (
                isinstance(record, dict)
                and record.get("content_hash") == h_hash
                and record.get("prompt_type") == prompt_type
                and record.get("status") == "claimed"
            ):
                record["status"] = "posted"
                record["posted_at"] = datetime.now(LOCAL_TZ).isoformat()
                _save_memory(memory)
                return
        raise RuntimeError("تعذر تثبيت المنشور في الذاكرة الدائمة.")


async def _memory_release_claim(h_hash: str, prompt_type: str):
    async with MEMORY_LOCK:
        memory = _load_memory()
        memory["posts"] = [
            record
            for record in memory["posts"]
            if not (
                isinstance(record, dict)
                and record.get("content_hash") == h_hash
                and record.get("prompt_type") == prompt_type
                and record.get("status") == "claimed"
            )
        ]
        _save_memory(memory)


# ============================================================
# أوامر المحتوى
# ============================================================
def _post_system_instruction(prompt_type: str) -> str:
    style_rule = ""
    if prompt_type == "stories_masa":
        style_rule = (
            "قصة من التراث الأندلسي الصالح، "
            "ولا تذكر نسبها إلى الجزائر أو تسميها قصة المساء أو التراث الجزائري، "
        )
    elif prompt_type == "qiyam_layl":
        style_rule = (
            "اكتب باللهجة الجزائرية الدارجة المفهومة والمحترمة، مع الحفاظ على روح إيمانية صادقة. "
            "لا تنسب حديثاً أو فضلاً محدداً إلى النبي صلى الله عليه وسلم دون يقين. "
        )
    elif prompt_type == "mawiza_20":
        style_rule = (
            "اكتب موعظة إيمانية عميقة وهادئة ومطمئنة بأسلوب أدبي قريب من منشورات القناة، "
            "واجعلها مناسبة للنشر المسائي وتترك في القلب أملاً وسكينة. "
        )
    elif prompt_type == "mawiza_08":
        style_rule = (
            "اكتب موعظة إيمانية عميقة وهادئة ومطمئنة بأسلوب أدبي قريب من منشورات القناة، "
            "واجعلها مناسبة لبداية النهار وتملأ القلب أملاً ورضاً وعزيمة هادئة. "
        )

    return (
        "أنت كاتب إيماني بليغ وذو أسلوب شعري هادئ وعميق. "
        "اكتب منشوراً متكاملاً غير مبتور، بحيث تكون آخر جملة مكتملة المعنى دون حشو أو إطناب. "
        "ابتعد عن التكرار والقوالب الجاهزة المستهلكة، "
        "وغيّر المقدمات والأساليب والصور التعبيرية في كل مرة، "
        "ولا تُلحق المنشور بأي تاريخ أو هجري أو خاتمة خارجية. "
        "اكتب النص plain text فقط دون نجوم أو علامات Markdown أو عناوين مزخرفة. "
        + style_rule
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
    "qiyam_layl":
        "اكتب تذكيراً يومياً بقيام الليل باللهجة الجزائرية الدارجة، يشجع بلطف على ركعتين أو أكثر والدعاء في هدوء الليل، دون تخويف أو مبالغة.",
    "mawiza_20":
        "اكتب موعظة إيمانية عميقة وهادئة ومطمئنة عن موضوع متجدد كل يوم، مثل الرجاء بالله أو جبر القلوب أو الصبر أو التوبة أو أثر الدعاء، واجعلها قريبة من الناس.",
    "mawiza_08":
        "اكتب موعظة إيمانية صباحية عميقة وهادئة ومطمئنة عن موضوع متجدد كل يوم، مثل بداية جديدة أو حسن الظن بالله أو الشكر أو السعي الطيب، واجعلها قريبة من الناس.",
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


def _sync_generate_post(prompt_text, prompt_type, known_hashes):
    for attempt in range(1, 5):
        text = _grok_chat(
            prompt_text,
            system_instruction=_post_system_instruction(prompt_type),
        )
        text = _clean_post_text(text)

        if content_hash(text) in known_hashes:
            print(f"⚠️ منشور مكرر (محاولة {attempt}/4)، سيتم طلب نص جديد.")
            continue

        print(f"✅ منشور جديد | محاولة {attempt}")
        return text

    raise RuntimeError("تعذر إنشاء منشور جديد غير مكرر بعد عدة محاولات.")


async def generate_content(prompt_type):
    known_hashes = await _memory_known_hashes(prompt_type)
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
async def _send_claimed_post(
    bot: Bot,
    prompt_type: str,
    label: str,
    text: str,
    claim_key: str,
) -> bool:
    post_hash = content_hash(text)
    claimed = await _memory_claim_post(post_hash, prompt_type, text, claim_key)
    if not claimed:
        print(f"⚠️ تم منع منشور مكرر قبل الإرسال: {label}")
        return False

    post_text = f"{get_hijri_date()}\n\n{text}{footer()}"
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=post_text)
    except Exception:
        await _memory_release_claim(post_hash, prompt_type)
        raise

    await _memory_mark_posted(post_hash, prompt_type)
    print(f"✅ تم النشر: {label}")
    return True


async def send_daily_post(
    bot: Bot,
    prompt_type: str,
    label: str,
) -> bool:
    try:
        print(f"📤 {label}...")
        text = _clean_post_text(await generate_content(prompt_type))
        claim_key = f"{prompt_type}:{datetime.now(LOCAL_TZ).date().isoformat()}"
        return await _send_claimed_post(bot, prompt_type, label, text, claim_key)
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


async def job_qiyam_layl(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "qiyam_layl",
        "تذكير قيام الليل"
    )


async def job_mawiza_20(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "mawiza_20",
        "الموعظة الإيمانية المسائية"
    )


async def job_mawiza_08(context: ContextTypes.DEFAULT_TYPE):
    await send_daily_post(
        context.bot,
        "mawiza_08",
        "الموعظة الإيمانية الصباحية"
    )


async def job_qiyam_layl_startup_once(context: ContextTypes.DEFAULT_TYPE):
    # نشر لمرة واحدة عند التشغيل، لأن موعد التذكير المجدول قد يكون قد فات.
    if await _memory_has_posted_today("qiyam_layl"):
        print("✅ تذكير قيام الليل منشور اليوم، لا حاجة لنشره الآن.")
        return
    await send_daily_post(context.bot, "qiyam_layl", "تذكير قيام الليل (عند التشغيل)")


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
    # عند إعادة التشغيل لا يُنشر أي شيء فوراً، بل ينتظر البوت موعده المجدول التالي.
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
    jq.run_daily(
        job_qiyam_layl,
        time=dtime(2, 0, tzinfo=ALGERIA_TZ),
        name="qiyam_layl_algeria"
    )
    jq.run_daily(
        job_mawiza_20,
        time=dtime(20, 0, tzinfo=ALGERIA_TZ),
        name="mawiza_20_algeria"
    )
    jq.run_daily(
        job_mawiza_08,
        time=dtime(8, 0, tzinfo=ALGERIA_TZ),
        name="mawiza_08_algeria"
    )

    jq.run_once(
        job_qiyam_layl_startup_once,
        when=10,
        name="qiyam_layl_startup_once",
    )

    print(f"✅ الذاكرة الدائمة تعمل على: {MEMORY_FILE}")
    print("✅ تم تسجيل الوظائف اليومية + تذكير قيام الليل لمرة واحدة عند التشغيل.")


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
