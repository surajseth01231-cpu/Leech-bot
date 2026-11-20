import os
import re
import time
import math
import asyncio
import aiohttp
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import FloodWait

# ================= CONFIG =================
API_ID = int(os.environ.get("API_ID", "3167980"))
API_HASH = os.environ.get("API_HASH", "7b7838599c637648e6d164054208d2eb")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "Boss@2011123")
PORT = int(os.environ.get("PORT", "8080"))

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "sanskari_txt_extractor",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
)

# ================= STATE =================
AUTHORIZED_USERS = set()
USER_STATE = {}


def get_state(uid: int) -> dict:
    if uid not in USER_STATE:
        USER_STATE[uid] = {
            "channel_id": None,
            "group_id": None,
            "mode": "channel",  # "channel" or "group_topic"
            "items": [],
            "batch_name": "Course Batch",
            "running": False,
            "stop_flag": False,
            "awaiting_target": None,  # "channel" or "group"
        }
    return USER_STATE[uid]


def is_auth(uid: int) -> bool:
    return uid in AUTHORIZED_USERS


# ================= HELPERS =================
def human_bytes(size: float) -> str:
    if not size or size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = min(int(math.log(size, 1024)), len(units) - 1)
    return f"{size / (1024 ** i):.2f} {units[i]}"


def format_eta(seconds: float) -> str:
    if seconds is None or seconds <= 0 or seconds > 86400:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def progress_bar(pct: float, length: int = 20) -> str:
    clamped = max(0.0, min(100.0, float(pct)))
    filled = int(length * clamped / 100.0)
    return "█" * filled + "░" * (length - filled)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\n\r]+', " ", name).strip()
    return (name[:110] or "lecture_file").strip()


def parse_txt_content(text: str, fallback_batch: str):
    items = []
    current_topic = fallback_batch
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("=====") and line.endswith("====="):
            header = line.strip("= ").strip()
            parts = [p.strip() for p in header.split("|") if p.strip()]
            current_topic = parts[-1][:100] if parts else fallback_batch
            continue

        match = re.search(r"(https?://\S+)", line)
        if not match:
            continue

        url = match.group(1).strip()
        title_part = line[: match.start()].rstrip(" :|-").strip()

        tags = re.findall(r"\(([^()]+)\)", title_part)
        topic = current_topic
        if tags:
            for t in reversed(tags):
                if t.lower() not in {
                    "live class",
                    "all study material - 2027",
                    "all study materials - 2027",
                    "home",
                }:
                    topic = t.strip()[:100]
                    break

        clean_title = re.sub(r"^\([^)]*\)\s*", "", title_part).strip()
        if not clean_title:
            clean_title = title_part or f"Lecture_{len(items) + 1}"

        lower_url = url.lower()
        if ".pdf" in lower_url or "pdf" in clean_title.lower().split():
            ftype = "pdf"
        elif any(ext in lower_url for ext in [".png", ".jpg", ".jpeg", ".webp"]):
            ftype = "image"
        else:
            ftype = "video"

        items.append({
            "title": clean_title,
            "url": url,
            "topic": topic,
            "type": ftype,
        })
    return items


# ================= DOWNLOADERS =================
async def download_direct_file(url: str, dest_path: str, status_cb, stop_check):
    timeout = aiohttp.ClientTimeout(total=1800, connect=30)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            start = time.time()
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    if stop_check():
                        raise RuntimeError("STOPPED_BY_USER")
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(0.1, time.time() - start)
                    speed = downloaded / elapsed
                    pct = (downloaded * 100 / total) if total > 0 else 0.0
                    eta = ((total - downloaded) / speed) if (total > 0 and speed > 0) else 0
                    await status_cb("📥 Downloading (HLS/Video)", pct, downloaded, total, speed, eta)


async def download_video_ytdlp(url: str, out_template: str, status_cb, stop_check) -> str:
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--newline",
        "--concurrent-fragments", "12",
        "--retries", "5",
        "--fragment-retries", "5",
        "--merge-output-format", "mp4",
        "-o", out_template,
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    prog_re = re.compile(
        r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*([\d\.]+[KMGTP]?i?B)\s+at\s+([\d\.]+[KMGTP]?i?B/s)\s+ETA\s+([\d:]+)"
    )

    while True:
        if stop_check():
            proc.kill()
            raise RuntimeError("STOPPED_BY_USER")
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="ignore").strip()
        m = prog_re.search(line)
        if m:
            pct = float(m.group(1))
            await status_cb(
                "📥 Downloading (HLS/Video)",
                pct,
                0,
                0,
                0,
                0,
                raw_size=m.group(2),
                raw_speed=m.group(3),
                raw_eta=m.group(4),
            )

    code = await proc.wait()
    if code != 0:
        raise RuntimeError(f"yt-dlp exited with code {code}")

    base_dir = os.path.dirname(out_template)
    prefix = os.path.basename(out_template).split(".")[0]
    for fn in os.listdir(base_dir):
        if fn.startswith(prefix) and not fn.endswith(".part"):
            return os.path.join(base_dir, fn)
    raise RuntimeError("Output video file not found")


# ================= CORE WORKER =================
async def run_batch_worker(uid: int, chat_id: int):
    st = get_state(uid)
    if st["running"]:
        await app.send_message(chat_id, "⚠️ Ek batch pehle se chal raha hai! Rokne ke liye `/stop` bhejein.")
        return

    items = st["items"]
    if not items:
        await app.send_message(chat_id, "📂 Koi TXT file load nahi hai. Pehle `.txt` file bhejein.")
        return

    mode = st["mode"]
    target_id = st["channel_id"] if mode == "channel" else st["group_id"]
    if not target_id:
        if mode == "channel":
            await app.send_message(chat_id, "❌ Target Channel set nahi hai! Pehle `/setchannel -100xxxxxxxxxx` bhejein.")
        else:
            await app.send_message(chat_id, "❌ Group ID set nahi hai! Group mein `/id` bhej kar ID lein aur `/grouptopictransfer -100xxxxxxxxxx` bhejein.")
        return

    st["running"] = True
    st["stop_flag"] = False

    status_msg = await app.send_message(
        chat_id,
        "⚡ EXTRACTOR LIVE STATUS\n━━━━━━━━━━━━━━━━━━━━\n🔄 Starting batch...",
    )
    last_edit = 0.0
    done_count = 0
    fail_count = 0
    total_items = len(items)
    topic_cache = {}

    async def safe_edit(text: str, force: bool = False):
        nonlocal last_edit
        now = time.time()
        if not force and (now - last_edit) < 3.0:
            return
        last_edit = now
        try:
            await status_msg.edit_text(text)
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
        except Exception:
            pass

    try:
        for idx, item in enumerate(items, start=1):
            if st["stop_flag"]:
                break

            title = item["title"]
            url = item["url"]
            ftype = item["type"]
            topic_name = item["topic"]
            left_count = total_items - idx + 1
            ext_label = ".pdf" if ftype == "pdf" else (".jpg" if ftype == "image" else ".mp4")
            display_name = f"{title}{ext_label}"

            async def update_ui(
                stage: str,
                pct: float,
                cur: int,
                tot: int,
                spd: float,
                eta: float,
                raw_size: str = None,
                raw_speed: str = None,
                raw_eta: str = None,
            ):
                size_str = (
                    raw_size
                    if raw_size
                    else (f"{human_bytes(cur)} / {human_bytes(tot)}" if tot > 0 else human_bytes(cur))
                )
                speed_str = raw_speed if raw_speed else f"{human_bytes(spd)}/s"
                eta_str = raw_eta if raw_eta else format_eta(eta)
                mode_label = "Channel Direct" if mode == "channel" else f"Group Topic ({topic_name[:22]})"

                txt = (
                    f"⚡ EXTRACTOR LIVE STATUS\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 Mode: {mode_label} | #{idx:03d} / {total_items:03d}\n"
                    f"📄 File: {display_name[:48]}\n"
                    f"🔄 Stage: {stage}\n\n"
                    f"[{progress_bar(pct)}] {pct:.1f}%\n"
                    f"💾 Size: {size_str}\n"
                    f"🚀 Speed: {speed_str} | ⏱ ETA: {eta_str}\n\n"
                    f"📊 Batch Summary:\n"
                    f"✅ Done: {done_count} | ❌ Failed: {fail_count} | ⏳ Left: {left_count}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛑 /stop dabakar turant rokein"
                )
                await safe_edit(txt)

            local_path = None
            try:
                await update_ui("📥 Downloading (HLS/Video)", 0.0, 0, 0, 0.0, 0.0)
                safe_name = sanitize_filename(title)

                if ftype == "pdf":
                    local_path = os.path.join(DOWNLOAD_DIR, f"{idx}_{safe_name}.pdf")
                    await download_direct_file(url, local_path, update_ui, lambda: st["stop_flag"])
                elif ftype == "image":
                    local_path = os.path.join(DOWNLOAD_DIR, f"{idx}_{safe_name}.jpg")
                    await download_direct_file(url, local_path, update_ui, lambda: st["stop_flag"])
                else:
                    out_tpl = os.path.join(DOWNLOAD_DIR, f"{idx}_{safe_name}.%(ext)s")
                    local_path = await download_video_ytdlp(url, out_tpl, update_ui, lambda: st["stop_flag"])

                if st["stop_flag"]:
                    break

                thread_id = None
                if mode == "group_topic":
                    if topic_name in topic_cache:
                        thread_id = topic_cache[topic_name]
                    else:
                        try:
                            created = await app.create_forum_topic(chat_id=target_id, title=topic_name[:120])
                            thread_id = created.id
                            topic_cache[topic_name] = thread_id
                        except Exception:
                            thread_id = None

                file_size = os.path.getsize(local_path) if local_path and os.path.exists(local_path) else 0
                up_start = time.time()

                async def upload_prog(current, total):
                    if st["stop_flag"]:
                        raise RuntimeError("STOPPED_BY_USER")
                    eff_total = total if total > 0 else file_size
                    elapsed = max(0.1, time.time() - up_start)
                    speed = current / elapsed
                    pct = (current * 100 / eff_total) if eff_total > 0 else 0.0
                    eta = ((eff_total - current) / speed) if (eff_total > 0 and speed > 0) else 0
                    await update_ui("📤 Uploading to Telegram", pct, current, eff_total, speed, eta)

                tag_label = "DOC" if ftype == "pdf" else "VIDEO"
                caption = (
                    f"┏━━━━━━━━━━━━━━━━━━┓\n"
                    f"  **{tag_label} #{idx:03d}**\n"
                    f"┗━━━━━━━━━━━━━━━━━━┛\n\n"
                    f"📝 **File:** `{title}`\n\n"
                    f"📦 **Topic:** `{topic_name}`\n"
                    f"🛠 **By:** @isurajsethh"
                )

                send_kwargs = {"chat_id": target_id, "caption": caption, "progress": upload_prog}
                if thread_id:
                    send_kwargs["message_thread_id"] = thread_id

                if ftype == "pdf":
                    await app.send_document(document=local_path, **send_kwargs)
                elif ftype == "image":
                    await app.send_photo(photo=local_path, **send_kwargs)
                else:
                    await app.send_video(
                        video=local_path,
                        supports_streaming=True,
                        **send_kwargs,
                    )

                done_count += 1
                await asyncio.sleep(0.8)

            except FloodWait as fw:
                await asyncio.sleep(fw.value + 2)
                fail_count += 1
            except Exception as e:
                if "STOPPED_BY_USER" in str(e):
                    break
                fail_count += 1
            finally:
                if local_path and os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass

        final_header = "🛑 Process Rok Diya Gaya (/stop)" if st["stop_flag"] else "✅ Batch Complete Ho Gaya!"
        await safe_edit(
            f"{final_header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Total Items: {total_items}\n"
            f"✅ Done: {done_count} | ❌ Failed: {fail_count} | ⏳ Left: {max(0, total_items - done_count - fail_count)}\n"
            f"━━━━━━━━━━━━━━━━━━━━",
            force=True,
        )
    finally:
        st["running"] = False
        st["stop_flag"] = False


# ================= COMMANDS & HANDLERS =================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 TXT Upload Karein", callback_data="ask_txt")],
        [
            InlineKeyboardButton("📢 Channel Mein Bhejen", callback_data="run_channel"),
            InlineKeyboardButton("🗂 Group Topic Ke Saath", callback_data="run_group"),
        ],
        [
            InlineKeyboardButton("🛑 Stop", callback_data="cmd_stop"),
            InlineKeyboardButton("♻️ Reset", callback_data="cmd_reset"),
        ],
    ])


@app.on_message(filters.command("id"))
async def id_cmd(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_auth(uid):
        return
    thread = getattr(m, "message_thread_id", None)
    await m.reply_text(
        f"🆔 **Chat ID:** `{m.chat.id}`\n"
        f"👤 **Your ID:** `{uid}`\n"
        f"📌 **Topic ID:** `{thread if thread else 'N/A'}`"
    )


@app.on_message(filters.private & filters.command("start"))
async def start_cmd(_, m: Message):
    uid = m.from_user.id
    st = get_state(uid)
    if not is_auth(uid):
        await m.reply_text("🔐 **Private Bot Locked**\n\nAccess ke liye password bhejein:")
        return

    await m.reply_text(
        f"👋 **Swagat hai!** Bot unlocked hai.\n\n"
        f"📢 **Channel ID:** `{st['channel_id'] or 'Not Set'}`\n"
        f"🗂 **Group ID:** `{st['group_id'] or 'Not Set'}`\n"
        f"📂 **Loaded Links:** `{len(st['items'])}`\n\n"
        f"• `/setchannel -100xxxxxxxxxx`\n"
        f"• `/channel` — Turant channel mein bhejna shuru\n"
        f"• `/grouptopictransfer -100xxxxxxxxxx` — Turant group topic mein bhejna shuru\n"
        f"• `/txt` — TXT file upload karein\n"
        f"• `/stop` — Turant rokein | `/reset` — Sab clear karein",
        reply_markup=main_menu_kb(),
    )


@app.on_message(filters.private & filters.command("setchannel"))
async def setchannel_cmd(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    st = get_state(uid)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        st["awaiting_target"] = "channel"
        await m.reply_text("📢 Apna **Channel ID** bhejein (jaise `-1001234567890`):")
        return
    try:
        st["channel_id"] = int(parts[1].strip())
        st["mode"] = "channel"
        if st["items"] and not st["running"]:
            asyncio.create_task(run_batch_worker(uid, m.chat.id))
        else:
            await m.reply_text(
                f"✅ **Channel Set Ho Gaya:** `{st['channel_id']}`\nAb `/txt` bhejein ya `/channel` dabakar turant transfer shuru karein.",
                reply_markup=main_menu_kb(),
            )
    except ValueError:
        await m.reply_text("❌ Galat Channel ID. `-100...` format mein bhejein.")


@app.on_message(filters.private & filters.command("channel"))
async def channel_mode_cmd(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    st = get_state(uid)
    st["mode"] = "channel"
    if not st["channel_id"]:
        st["awaiting_target"] = "channel"
        await m.reply_text("📢 Pehle Target **Channel ID** bhejein (`-100...`):")
        return
    if not st["items"]:
        await m.reply_text(f"✅ **Channel Mode Active** (`{st['channel_id']}`).\nAb `.txt` file bhejein, bhejte hi upload shuru ho jayega!")
        return
    asyncio.create_task(run_batch_worker(uid, m.chat.id))


@app.on_message(filters.private & filters.command("grouptopictransfer"))
async def group_topic_cmd(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    st = get_state(uid)
    st["mode"] = "group_topic"
    parts = m.text.split(maxsplit=1)
    if len(parts) >= 2:
        try:
            st["group_id"] = int(parts[1].strip())
        except ValueError:
            await m.reply_text("❌ Galat Group ID. `-100...` format mein bhejein.")
            return

    if not st["group_id"]:
        st["awaiting_target"] = "group"
        await m.reply_text("🗂 Apna **Group ID** bhejein (`-100...`) jismein **Topics ON** hon aur bot **Admin (Manage Topics)** ho:")
        return

    if not st["items"]:
        await m.reply_text(f"✅ **Group Topic Mode Active** (`{st['group_id']}`).\nAb `.txt` file bhejein, bhejte hi topics banakar upload shuru ho jayega!")
        return

    asyncio.create_task(run_batch_worker(uid, m.chat.id))


@app.on_message(filters.private & filters.command("txt"))
async def txt_cmd(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    await m.reply_text("📄 Apni course `.txt` file upload karein:")


@app.on_message(filters.private & filters.command("stop"))
async def stop_cmd(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    st = get_state(uid)
    if st["running"]:
        st["stop_flag"] = True
        await m.reply_text("🛑 Stop signal bhej diya gaya hai — turant ruk raha hai.")
    else:
        await m.reply_text("ℹ️ Abhi koi process nahi chal raha hai.")


@app.on_message(filters.private & filters.command("reset"))
async def reset_cmd(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    st = get_state(uid)
    st["stop_flag"] = True
    st["channel_id"] = None
    st["group_id"] = None
    st["items"] = []
    st["awaiting_target"] = None
    await m.reply_text("♻️ **Reset Complete!** Set channel, group ID aur loaded TXT file clear kar di gayi hai.", reply_markup=main_menu_kb())


@app.on_message(filters.private & filters.document)
async def handle_txt_file(_, m: Message):
    uid = m.from_user.id
    if not is_auth(uid):
        return
    doc = m.document
    if not doc.file_name.lower().endswith(".txt"):
        await m.reply_text("❌ Kripya sirf `.txt` file bhejein.")
        return

    st = get_state(uid)
    if st["running"]:
        await m.reply_text("⚠️ Pehle se ek batch chal raha hai. Nayi file ke liye pehle `/stop` karein.")
        return

    tmp_txt = os.path.join(DOWNLOAD_DIR, f"{uid}_{doc.file_name}")
    await m.download(file_name=tmp_txt)
    try:
        with open(tmp_txt, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    finally:
        if os.path.exists(tmp_txt):
            os.remove(tmp_txt)

    batch_title = os.path.splitext(doc.file_name)[0]
    items = parse_txt_content(content, batch_title)
    if not items:
        await m.reply_text("❌ Is `.txt` file mein koi valid video ya PDF link nahi mila.")
        return

    st["items"] = items
    st["batch_name"] = batch_title

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Channel Mein Bhejen", callback_data="run_channel"),
            InlineKeyboardButton("🗂 Group Topic Ke Saath Bhejen", callback_data="run_group"),
        ]
    ])
    await m.reply_text(
        f"✅ **TXT File Detect Ho Gayi!**\n"
        f"📂 **File:** `{doc.file_name}`\n"
        f"🔢 **Total Items:** `{len(items)}`\n\n"
        f"Kahan bhejna shuru karein?",
        reply_markup=kb,
    )


@app.on_callback_query()
async def callbacks(_, cq: CallbackQuery):
    uid = cq.from_user.id
    if not is_auth(uid):
        await cq.answer("🔐 Pehle /start karke password dalein!", show_alert=True)
        return

    st = get_state(uid)
    data = cq.data
    await cq.answer()

    if data == "ask_txt":
        await cq.message.reply_text("📄 Apni course `.txt` file bhejein:")
    elif data == "run_channel":
        st["mode"] = "channel"
        asyncio.create_task(run_batch_worker(uid, cq.message.chat.id))
    elif data == "run_group":
        st["mode"] = "group_topic"
        asyncio.create_task(run_batch_worker(uid, cq.message.chat.id))
    elif data == "cmd_stop":
        st["stop_flag"] = True
        await cq.message.reply_text("🛑 Stop signal bhej diya gaya hai.")
    elif data == "cmd_reset":
        st["stop_flag"] = True
        st["channel_id"] = None
        st["group_id"] = None
        st["items"] = []
        await cq.message.reply_text("♻️ Sab reset ho gaya hai.")


@app.on_message(
    filters.private
    & filters.text
    & ~filters.command(["start", "id", "setchannel", "channel", "grouptopictransfer", "txt", "stop", "reset"])
)
async def text_router(_, m: Message):
    uid = m.from_user.id
    st = get_state(uid)
    txt = m.text.strip()

    if not is_auth(uid):
        if txt == BOT_PASSWORD:
            AUTHORIZED_USERS.add(uid)
            await m.reply_text(
                "🔓 **Password Verified! Bot Unlocked.**\nAb aapke alawa koi aur is bot ko use nahi kar payega.",
                reply_markup=main_menu_kb(),
            )
        else:
            await m.reply_text("❌ Galat password! Sahi password dalein:")
        return

    if st["awaiting_target"] == "channel":
        try:
            st["channel_id"] = int(txt)
            st["mode"] = "channel"
            st["awaiting_target"] = None
            if st["items"] and not st["running"]:
                asyncio.create_task(run_batch_worker(uid, m.chat.id))
            else:
                await m.reply_text(f"✅ **Channel ID Set:** `{st['channel_id']}`\nAb `/txt` bhejein ya `/channel` dabakar shuru karein.", reply_markup=main_menu_kb())
        except ValueError:
            await m.reply_text("❌ Sahi numeric Channel ID (`-100...`) bhejein:")
    elif st["awaiting_target"] == "group":
        try:
            st["group_id"] = int(txt)
            st["mode"] = "group_topic"
            st["awaiting_target"] = None
            if st["items"] and not st["running"]:
                asyncio.create_task(run_batch_worker(uid, m.chat.id))
            else:
                await m.reply_text(f"✅ **Group ID Set:** `{st['group_id']}`\nAb `/txt` bhejein ya `/grouptopictransfer` dabakar shuru karein.", reply_markup=main_menu_kb())
        except ValueError:
            await m.reply_text("❌ Sahi numeric Group ID (`-100...`) bhejein:")


# ================= RENDER HEALTH SERVER =================
async def health_handler(_request):
    return web.Response(text="OK - Extractor Bot Running")


async def start_bot():
    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await app.start()
    print("Bot started successfully!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(start_bot())
