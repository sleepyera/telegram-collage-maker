import asyncio
import io
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from PIL import Image, ImageOps, ImageColor, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "collage.sqlite3"
MAX_PHOTOS_PER_USER = 100
MAX_OUTPUT_SIDE = 2400

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing. Add it in Render → Environment.")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Runtime work is per Telegram user, never shared between users.
SESSIONS: Dict[int, dict] = {}
ALBUM_TASKS: Dict[Tuple[int, str], asyncio.Task] = {}
WEB_RUNNER = None

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS layouts (
        user_id INTEGER NOT NULL, name TEXT NOT NULL, settings TEXT NOT NULL,
        PRIMARY KEY(user_id, name))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY, settings TEXT NOT NULL)""")
    return conn

DEFAULT_SETTINGS = {
    "fit": "contain", "gap": 12, "border": 12, "background": "#20232a",
    "format": "JPG", "quality": 92, "tile_width": 640, "tile_height": 360,
    "columns": 2
}

def session(uid: int) -> dict:
    if uid not in SESSIONS:
        SESSIONS[uid] = {
            "photos": [], "outputs": [], "mode": 2, "settings": load_settings(uid),
            "reference": None, "last_output_ids": [], "waiting": None
        }
    return SESSIONS[uid]

def load_settings(uid):
    conn = db()
    row = conn.execute("SELECT settings FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if row:
        try:
            s = DEFAULT_SETTINGS.copy()
            s.update(json.loads(row[0]))
            return s
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(uid):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO user_settings(user_id, settings) VALUES(?,?)",
                 (uid, json.dumps(session(uid)["settings"])))
    conn.commit()
    conn.close()

def keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🧩 2×1 Collage"), KeyboardButton(text="🧩 6×1 Collage")],
        [KeyboardButton(text="🧩 8×1 Collage"), KeyboardButton(text="⚙️ Settings")],
        [KeyboardButton(text="🖼 My Collages"), KeyboardButton(text="🧹 Clear All")],
        [KeyboardButton(text="❓ Help")]
    ], resize_keyboard=True)

def inline_buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])

def help_text():
    return (
        "<b>Collage Maker Bot — Help</b>\\n\\n"
        "1) Choose 2×1, 6×1, or 8×1 mode.\\n"
        "2) Send photos one by one or as Telegram albums. When enough photos arrive, the bot creates collages automatically.\\n"
        "3) Extra photos stay pending for the next collage.\\n\\n"
        "<b>Commands</b>\\n"
        "/mode 2 | /mode 6 | /mode 8 — collage batch size\\n"
        "/pending — pending photo count\\n"
        "/make — create a collage from the pending photos (needs a full batch)\\n"
        "/drop_pending — discard pending photos\\n"
        "/convert 4x1 — join the latest 4 collages horizontally\\n"
        "/convert 1x4 — join vertically; /convert 2x2 — grid\\n"
        "/settings — image settings\\n"
        "/fit contain|cover — preserve proportions or crop to fill\\n"
        "/gap 12 — spacing in pixels (0–80)\\n"
        "/border 12 — outer border in pixels (0–100)\\n"
        "/bg #20232a — background colour\\n"
        "/format jpg|png — output format\\n"
        "/quality 92 — JPG quality (50–100)\\n"
        "/save_layout name — save current settings\\n"
        "/use_layout name — load saved settings\\n"
        "/my_layouts — list saved layouts\\n"
        "/delete_layout name — delete a saved layout\\n"
        "/reference — then send a reference collage image (approximate layout matching)\\n"
        "/clear all — clear this user's current photos and generated collages\\n\\n"
        "Note: temporary photos/collages are kept in bot memory only and may reset after a restart. Saved layouts/settings are stored in SQLite."
    )

@dp.message(CommandStart())
async def start(m: Message):
    session(m.from_user.id)
    await m.answer(
        "👋 <b>Welcome to Collage Maker!</b>\\n\\n"
        "Choose a collage mode below, then send your game screenshots. "
        "The bot keeps each user's photos separate.",
        reply_markup=keyboard()
    )

@dp.message(Command("help"))
@dp.message(F.text == "❓ Help")
async def help_cmd(m: Message):
    await m.answer(help_text(), reply_markup=keyboard())

@dp.message(F.text.in_({"🧩 2×1 Collage", "🧩 6×1 Collage", "🧩 8×1 Collage"}))
async def mode_button(m: Message):
    n = 2 if "2×1" in m.text else (6 if "6×1" in m.text else 8)
    session(m.from_user.id)["mode"] = n
    await m.answer(f"✅ Mode set to <b>{n} photos per collage</b>. Send your photos now.")

@dp.message(Command("mode"))
async def mode_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2 or parts[1] not in {"2", "6", "8"}:
        return await m.answer("Use: /mode 2, /mode 6, or /mode 8")
    session(m.from_user.id)["mode"] = int(parts[1])
    await m.answer(f"✅ Mode set to {parts[1]} photos per collage.")

@dp.message(Command("pending"))
async def pending(m: Message):
    s = session(m.from_user.id)
    await m.answer(f"📥 Pending photos: <b>{len(s['photos'])}</b> / {s['mode']}")

@dp.message(Command("drop_pending"))
async def drop_pending(m: Message):
    s = session(m.from_user.id)
    count = len(s["photos"])
    s["photos"].clear()
    await m.answer(f"🗑 Discarded {count} pending photo(s).")

def decode_color(value: str):
    try:
        return ImageColor.getrgb(value)
    except Exception:
        return (32, 35, 42)def fit_image(img: Image.Image, size: Tuple[int, int], fit: str, bg):
    img = ImageOps.exif_transpose(img).convert("RGB")
    if fit == "cover":
        return ImageOps.fit(img, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    img.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(img, ((size[0] - img.width)//2, (size[1] - img.height)//2))
    return canvas

def make_collage(images: List[Image.Image], settings: dict, columns: Optional[int] = None):
    n = len(images)
    cols = columns or (2 if n > 1 else 1)
    rows = (n + cols - 1) // cols
    tw = max(160, min(int(settings.get("tile_width", 640)), MAX_OUTPUT_SIDE))
    th = max(120, min(int(settings.get("tile_height", 360)), MAX_OUTPUT_SIDE))
    gap = max(0, min(int(settings.get("gap", 12)), 80))
    border = max(0, min(int(settings.get("border", 12)), 100))
    bg = decode_color(settings.get("background", "#20232a"))
    canvas = Image.new("RGB", (cols*tw + max(0, cols-1)*gap + 2*border,
                               rows*th + max(0, rows-1)*gap + 2*border), bg)
    for i, img in enumerate(images):
        tile = fit_image(img, (tw, th), settings.get("fit", "contain"), bg)
        x = border + (i % cols)*(tw+gap)
        y = border + (i // cols)*(th+gap)
        canvas.paste(tile, (x, y))
    canvas.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)
    return canvas

def encode_image(img: Image.Image, settings: dict):
    buf = io.BytesIO()
    fmt = settings.get("format", "JPG").upper()
    if fmt == "PNG":
        img.save(buf, format="PNG", optimize=True)
        ext = "png"
    else:
        img.save(buf, format="JPEG", quality=max(50, min(100, int(settings.get("quality", 92)))), optimize=True)
        ext = "jpg"
    return buf.getvalue(), ext

async def download_photo(m: Message):
    ph = m.photo[-1]
    f = await bot.get_file(ph.file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, destination=buf)
    buf.seek(0)
    try:
        return Image.open(buf).convert("RGB")
    except Exception:
        return None

async def process_ready(uid: int, chat_id: int):
    s = session(uid)
    mode = s["mode"]
    made = 0
    while len(s["photos"]) >= mode:
        batch = s["photos"][:mode]
        del s["photos"][:mode]
        try:
            collage = make_collage(batch, s["settings"])
            data, ext = encode_image(collage, s["settings"])
            name = f"collage_{int(time.time())}_{uuid.uuid4().hex[:5]}.{ext}"
            s["outputs"].append({"name": name, "data": data, "created": time.time()})
            if len(s["outputs"]) > 30:
                s["outputs"] = s["outputs"][-30:]
            await bot.send_document(chat_id, BufferedInputFile(data, filename=name),
                                    caption=f"✅ Collage ready ({mode} photos).")
            made += 1
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Could not create collage: {e}")
    if made:
        await bot.send_message(chat_id, f"Done: <b>{made}</b> collage(s). Pending photos: <b>{len(s['photos'])}</b>.")
    elif s["photos"]:
        await bot.send_message(chat_id, f"📥 Received. Pending: <b>{len(s['photos'])}/{mode}</b>. Send more photos to complete it, or use /drop_pending.")

async def collect_album(m: Message):
    uid = m.from_user.id
    key = (uid, m.media_group_id or str(m.message_id))
    task = ALBUM_TASKS.get(key)
    if task:
        task.cancel()
    async def delayed():
        await asyncio.sleep(1.2)
        ALBUM_TASKS.pop(key, None)
        s = session(uid)
        if len(s["photos"]) > MAX_PHOTOS_PER_USER:
            s["photos"] = s["photos"][-MAX_PHOTOS_PER_USER:]
        await process_ready(uid, m.chat.id)
    ALBUM_TASKS[key] = asyncio.create_task(delayed())

@dp.message(Command("make"))
async def make_cmd(m: Message):
    s = session(m.from_user.id)
    if len(s["photos"]) < s["mode"]:
        return await m.answer(f"Need {s['mode']} photos for the next collage; currently {len(s['photos'])} pending.")
    await process_ready(m.from_user.id, m.chat.id)

@dp.message(Command("convert"))
async def convert_cmd(m: Message):
    s = session(m.from_user.id)
    parts = (m.text or "").split()
    if len(parts) != 2 or not re.fullmatch(r"\d{1,2}x\d{1,2}", parts[1].lower()):
        return await m.answer("Use /convert 4x1, /convert 1x4, /convert 2x2, or /convert 3x2")
    cols, rows = map(int, parts[1].lower().split("x"))
    if cols < 1 or rows < 1 or cols*rows > 20:
        return await m.answer("Grid must be between 1 and 20 images.")
    n = cols*rows
    outputs = s["outputs"][-n:]
    if len(outputs) < n:
        return await m.answer(f"Need {n} generated collages; I have {len(outputs)} in this session.")
    ims = []
    for item in outputs:
        try:
            ims.append(Image.open(io.BytesIO(item["data"])).convert("RGB"))
        except Exception:
            pass
    if len(ims) != n:
        return await m.answer("Some recent collage files are unavailable. Create them again first.")
    # Each collage is treated as one tile; fit to the largest recent tile dimensions.
    tw = min(MAX_OUTPUT_SIDE, max(i.width for i in ims))
    th = min(MAX_OUTPUT_SIDE, max(i.height for i in ims))
    bg = decode_color(s["settings"].get("background", "#20232a"))
    tiles = [fit_image(i, (tw, th), "contain", bg) for i in ims]
    gap = max(0, min(int(s["settings"].get("gap", 12)), 80))
    canvas = Image.new("RGB", (cols*tw+(cols-1)*gap, rows*th+(rows-1)*gap), bg)
    for idx, tile in enumerate(tiles):
        canvas.paste(tile, ((idx % cols)*(tw+gap), (idx//cols)*(th+gap)))
    canvas.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)
    data, ext = encode_image(canvas, s["settings"])
    name = f"converted_{cols}x{rows}_{uuid.uuid4().hex[:5]}.{ext}"
    s["outputs"].append({"name": name, "data": data, "created": time.time()})
    await m.answer_document(BufferedInputFile(data, filename=name), caption=f"🔗 Combined latest {n} collages into {cols}×{rows}.")

@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Settings")
async def settings_cmd(m: Message):
    s = session(m.from_user.id)["settings"]
    await m.answer(
        f"<b>Current settings</b>\\nFit: {s['fit']} | Gap: {s['gap']} | Border: {s['border']}\\n"
        f"Background: {s['background']} | Format: {s['format']} | Quality: {s['quality']}\\n"
        f"Tile: {s['tile_width']}×{s['tile_height']}\\n\\n"
        "Change with /fit contain|cover, /gap 12, /border 12, /bg #20232a, "
        "/format jpg|png, /quality 92, /tile 640x360",
        reply_markup=inline_buttons([[("Fit: Contain", "fit:contain"), ("Fit: Cover", "fit:cover")],
                                     [("JPG", "format:JPG"), ("PNG", "format:PNG")],
                                     [("Reset settings", "settings:reset")]])
    )

@dp.callback_query(F.data.startswith("fit:"))
async def cb_fit(c: CallbackQuery):
    session(c.from_user.id)["settings"]["fit"] = c.data.split(":")[1]
    save_settings(c.from_user.id)
    await c.answer("Fit mode updated")
    await c.message.edit_text(f"Fit mode set to {session(c.from_user.id)['settings']['fit']}.")

@dp.callback_query(F.data.startswith("format:"))
async def cb_format(c: CallbackQuery):
    session(c.from_user.id)["settings"]["format"] = c.data.split(":")[1]
    save_settings(c.from_user.id)
    await c.answer("Format updated")
    await c.message.edit_text(f"Output format set to {session(c.from_user.id)['settings']['format']}.")

@dp.callback_query(F.data == "settings:reset")
async def cb_reset_settings(c: CallbackQuery):
    session(c.from_user.id)["settings"] = DEFAULT_SETTINGS.copy()
    save_settings(c.from_user.id)
    await c.answer("Settings reset")
    await c.message.edit_text("Settings reset to defaults.")

@dp.message(Command("fit"))
async def fit_cmd(m: Message):
    p = (m.text or "").split()
    if len(p) != 2 or p[1].lower() not in {"contain", "cover"}:
        return await m.answer("Use /fit contain or /fit cover. Contain avoids cropping; cover fills the tile by cropping edges.")
    session(m.from_user.id)["settings"]["fit"] = p[1].lower()
    save_settings(m.from_user.id)
    await m.answer(f"Fit set to {p[1].lower()}.")

@dp.message(Command("gap"))
async def gap_cmd(m: Message):
    p = (m.text or "").split()
    try: n = int(p[1])
    except Exception: return await m.answer("Use /gap 12 (0–80).")
    if not 0 <= n <= 80: return await m.answer("Gap must be 0–80.")
    session(m.from_user.id)["settings"]["gap"] = n
    save_settings(m.from_user.id)
    await m.answer(f"Gap set to {n}px.")

@dp.message(Command("border"))
async def border_cmd(m: Message):
    p = (m.text or "").split()
    try: n = int(p[1])
    except Exception: return await m.answer("Use /border 12 (0–100).")
    if not 0 <= n <= 100: return await m.answer("Border must be 0–100.")
    session(m.from_user.id)["settings"]["border"] = n
    save_settings(m.from_user.id)
    await m.answer(f"Border set to {n}px.")

@dp.message(Command("bg"))
async def bg_cmd(m: Message):
    p = (m.text or "").split(maxsplit=1)
    if len(p) != 2: return await m.answer("Use /bg #20232a or /bg white.")
    try: ImageColor.getrgb(p[1])
    except Exception: return await m.answer("Invalid colour. Try #20232a, black, white, or grey.")
    session(m.from_user.id)["settings"]["background"] = p[1]
    save_settings(m.from_user.id)
    await m.answer(f"Background set to {p[1]}.")

@dp.message(Command("format"))
async def format_cmd(m: Message):
    p = (m.text or "").split()
    if len(p) != 2 or p[1].lower() not in {"jpg", "jpeg", "png"}:
        return await m.answer("Use /format jpg or /format png.")
    session(m.from_user.id)["settings"]["format"] = "PNG" if p[1].lower() == "png" else "JPG"
    save_settings(m.from_user.id)
    await m.answer(f"Format set to {session(m.from_user.id)['settings']['format']}.")

@dp.message(Command("quality"))
async def quality_cmd(m: Message):
    p = (m.text or "").split()
    try: n = int(p[1])
    except Exception: return await m.answer("Use /quality 92 (50–100).")
    if not 50 <= n <= 100: return await m.answer("Quality must be 50–100.")
    session(m.from_user.id)["settings"]["quality"] = n
    save_settings(m.from_user.id)
    await m.answer(f"JPG quality set to {n}.")

@dp.message(Command("tile"))
async def tile_cmd(m: Message):
    p = (m.text or "").split()
    if len(p) != 2 or not re.fullmatch(r"\d{3,4}x\d{3,4}", p[1].lower()):
        return await m.answer("Use /tile 640x360. Tile width 300–1600, height 200–1200.")
    w, h = map(int, p[1].lower().split("x"))
    if not (300 <= w <= 1600 and 200 <= h <= 1200):
        return await m.answer("Tile width must be 300–1600 and height 200–1200.")
    session(m.from_user.id)["settings"].update(tile_width=w, tile_height=h)
    save_settings(m.from_user.id)
    await m.answer(f"Tile size set to {w}×{h}.")

@dp.message(Command("save_layout"))
async def save_layout(m: Message):
    p = (m.text or "").split(maxsplit=1)
    if len(p) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", p[1]):
        return await m.answer("Use /save_layout brawl (name: 1–24 letters, numbers, _ or -).")
    uid, name = m.from_user.id, p[1].lower()
    conn = db()
    conn.execute("INSERT OR REPLACE INTO layouts(user_id,name,settings) VALUES(?,?,?)",
                 (uid, name, json.dumps(session(uid)["settings"])))
    conn.commit(); conn.close()
    await m.answer(f"💾 Saved layout <b>{name}</b>.")

@dp.message(Command("use_layout"))
async def use_layout(m: Message):
    p = (m.text or "").split(maxsplit=1)
    if len(p) != 2: return await m.answer("Use /use_layout brawl.")
    conn = db()
    row = conn.execute("SELECT settings FROM layouts WHERE user_id=? AND name=?", (m.from_user.id, p[1].lower())).fetchone()
    conn.close()
    if not row: return await m.answer("Layout not found. Use /my_layouts.")
    s = DEFAULT_SETTINGS.copy(); s.update(json.loads(row[0]))
    session(m.from_user.id)["settings"] = s
    save_settings(m.from_user.id)
    await m.answer(f"✅ Loaded layout <b>{p[1].lower()}</b>.")@dp.message(Command("my_layouts"))
async def my_layouts(m: Message):
    conn = db()
    rows = conn.execute("SELECT name FROM layouts WHERE user_id=? ORDER BY name", (m.from_user.id,)).fetchall()
    conn.close()
    await m.answer("Saved layouts: " + (", ".join(r[0] for r in rows) if rows else "none yet") + "\\nSave with /save_layout name.")

@dp.message(Command("delete_layout"))
async def delete_layout(m: Message):
    p = (m.text or "").split(maxsplit=1)
    if len(p) != 2: return await m.answer("Use /delete_layout brawl.")
    conn = db()
    cur = conn.execute("DELETE FROM layouts WHERE user_id=? AND name=?", (m.from_user.id, p[1].lower()))
    conn.commit(); conn.close()
    await m.answer("Deleted." if cur.rowcount else "Layout not found.")

@dp.message(Command("reference"))
async def reference_cmd(m: Message):
    session(m.from_user.id)["waiting"] = "reference"
    await m.answer("Send one reference collage image now. I'll estimate its tile proportions and spacing; this is an approximation, not perfect AI layout detection.")

@dp.message(Command("my_collages"))
@dp.message(F.text == "🖼 My Collages")
async def my_collages(m: Message):
    outs = session(m.from_user.id)["outputs"]
    if not outs:
        return await m.answer("No generated collages in this session yet.")
    rows = []
    for i, item in enumerate(outs[-10:]):
        rows.append([ (f"📄 {len(outs)-min(10,len(outs))+i+1}. {item['name'][:22]}", f"out:{len(outs)-min(10,len(outs))+i}") ])
    rows.append([("🗑 Delete all outputs", "outputs:clear")])
    await m.answer("Your latest generated collages:", reply_markup=inline_buttons(rows))

@dp.callback_query(F.data.startswith("out:"))
async def output_action(c: CallbackQuery):
    s = session(c.from_user.id)
    try: idx = int(c.data.split(":")[1])
    except Exception: return await c.answer("Invalid item", show_alert=True)
    if idx >= len(s["outputs"]): return await c.answer("This item is no longer available.", show_alert=True)
    item = s["outputs"][idx]
    await c.message.answer_document(BufferedInputFile(item["data"], filename=item["name"]))
    await c.answer()

@dp.callback_query(F.data == "outputs:clear")
async def clear_outputs(c: CallbackQuery):
    await c.message.answer("Delete all current generated collages?", reply_markup=inline_buttons([[("Yes, delete", "clear:outputs:yes"), ("Cancel", "clear:no")]]))
    await c.answer()

@dp.callback_query(F.data == "clear:outputs:yes")
async def clear_outputs_yes(c: CallbackQuery):
    session(c.from_user.id)["outputs"].clear()
    await c.message.answer("🗑 Your current generated collages have been cleared.")
    await c.answer()

@dp.message(Command("clear"))
@dp.message(F.text == "🧹 Clear All")
async def clear_cmd(m: Message):
    await m.answer("This clears only your current pending photos and generated collages. Saved layouts/settings stay. Continue?",
                   reply_markup=inline_buttons([[("🗑 Confirm clear", "clear:all:yes"), ("Cancel", "clear:no")]]))

@dp.callback_query(F.data == "clear:all:yes")
async def clear_all_yes(c: CallbackQuery):
    s = session(c.from_user.id)
    s["photos"].clear()
    s["outputs"].clear()
    s["reference"] = None
    s["waiting"] = None
    await c.message.answer("✅ Cleared your current photos and generated collages. Saved layouts/settings were kept.")
    await c.answer()

@dp.callback_query(F.data == "clear:no")
async def clear_no(c: CallbackQuery):
    await c.message.answer("Cancelled.")
    await c.answer()

@dp.message(F.text)
async def unknown_text(m: Message):
    if (m.text or "").startswith("/"):
        return
    # Keep friendly response for arbitrary text without interrupting commands.
    await m.answer("Choose a button or send photos. Type /help to see all commands.", reply_markup=keyboard())

# Replace photo handler with a state-aware implementation at startup.
async def photo_state_handler(m: Message):
    uid = m.from_user.id
    s = session(uid)
    if s.get("waiting") == "reference":
        s["waiting"] = None
        img = await download_photo(m)
        if img is None:
            return await m.answer("Could not read reference image. Send it again using /reference.")
        w, h = img.size
        # Estimate rough orientation. Reference affects the default tile size and fit mode only.
        if w >= h:
            s["settings"].update(tile_width=640, tile_height=360, fit="contain")
        else:
            s["settings"].update(tile_width=360, tile_height=640, fit="contain")
        s["reference"] = {"width": w, "height": h}
        save_settings(uid)
        return await m.answer(f"🧭 Reference noted ({w}×{h}). I set an approximate tile ratio. Adjust with /tile and /fit, then save using /save_layout name.")
    if len(s["photos"]) >= MAX_PHOTOS_PER_USER:
        return await m.answer("You have too many pending photos. Choose a mode or use /drop_pending.")
    img = await download_photo(m)
    if img is None:
        return await m.answer("❌ I couldn't read that image. Please send it again.")
    s["photos"].append(img)
    await collect_album(m)

dp.message.register(photo_state_handler, F.photo)

async def health(request):
    return web.json_response({"status": "ok", "service": "telegram-collage-maker"})

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    return runner

async def main():
    global WEB_RUNNER
    WEB_RUNNER = await start_health_server()
    print(f"Health server listening on port {PORT}")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())

