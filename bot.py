import asyncio
import io
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup,
    KeyboardButton, BufferedInputFile
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

MAX_PHOTOS = 100
MAX_OUTPUT_SIDE = 2400

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add it in Render Environment."
    )

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

SESSIONS = {}
ALBUM_TASKS = {}


DEFAULT_SETTINGS = {
    "fit": "contain",
    "gap": 12,
    "border": 12,
    "background": "#20232a",
    "format": "JPG",
    "quality": 92,
    "tile_width": 640,
    "tile_height": 360,
}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS layouts (
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            settings TEXT NOT NULL,
            PRIMARY KEY(user_id, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            settings TEXT NOT NULL
        )
    """)
    return conn


def load_settings(uid):
    conn = db()
    row = conn.execute(
        "SELECT settings FROM user_settings WHERE user_id=?",
        (uid,)
    ).fetchone()
    conn.close()

    settings = DEFAULT_SETTINGS.copy()

    if row:
        try:
            settings.update(json.loads(row[0]))
        except Exception:
            pass

    return settings


def session(uid):
    if uid not in SESSIONS:
        SESSIONS[uid] = {
            "photos": [],
            "outputs": [],
            "mode": 2,
            "settings": load_settings(uid),
            "waiting": None,
        }
    return SESSIONS[uid]


def save_settings(uid):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO user_settings(user_id, settings) VALUES(?, ?)",
        (uid, json.dumps(session(uid)["settings"]))
    )
    conn.commit()
    conn.close()


def keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🧩 2×1 Collage"),
                KeyboardButton(text="🧩 6×1 Collage")
            ],
            [
                KeyboardButton(text="🧩 8×1 Collage"),
                KeyboardButton(text="⚙️ Settings")
            ],
            [
                KeyboardButton(text="🖼 My Collages"),
                KeyboardButton(text="🧹 Clear All")
            ],
            [KeyboardButton(text="❓ Help")]
        ],
        resize_keyboard=True
    )


def buttons(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=data)
                for label, data in
                row
            ]
            for row in rows
        ]
    )


def decode_color(value):
    try:
        return ImageColor.getrgb(value)
    except Exception:
        return (32, 35, 42)


def fit_image(img, size, fit, bg):
    img = ImageOps.exif_transpose(img).convert("RGB")

    if fit == "cover":
        return ImageOps.fit(
            img, size,
            method=Image.Resampling.LANCZOS
        )

    img.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(
        img,
        ((size[0] - img.width) // 2,
         (size[1] - img.height) // 2)
    )
    return canvas


def make_collage(images, settings, columns=None):
    count = len(images)
    cols = columns or (2 if count > 1 else 1)
    rows = (count + cols - 1) // cols

    tw = max(160, min(int(settings["tile_width"]), MAX_OUTPUT_SIDE))
    th = max(120, min(int(settings["tile_height"]), MAX_OUTPUT_SIDE))
    gap = max(0, min(int(settings["gap"]), 80))
    border = max(0, min(int(settings["border"]), 100))
    bg = decode_color(settings["background"])

    width = cols * tw + (cols - 1) * gap + 2 * border
    height = rows * th + (rows - 1) * gap + 2 * border

    canvas = Image.new("RGB", (width, height), bg)

    for index, image in enumerate(images):
        tile = fit_image(
            image, (tw, th), settings["fit"], bg
        )
        x = border + (index % cols) * (tw + gap)
        y = border + (index // cols) * (th + gap)
        canvas.paste(tile, (x, y))

    canvas.thumbnail(
        (MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE),
        Image.Resampling.LANCZOS
    )
    return canvas


def encode_image(image, settings):
    output = io.BytesIO()
    fmt = settings["format"].upper()

    if fmt == "PNG":
        image.save(output, format="PNG", optimize=True)
        extension = "png"
    else:
        quality = max(50, min(100, int(settings["quality"])))
        image.save(
            output, format="JPEG",
            quality=quality, optimize=True
        )
        extension = "jpg"

    return output.getvalue(), extension


async def download_photo(message):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    buffer = io.BytesIO()

    await bot.download_file(
        file_info.file_path,
        destination=buffer
    )

    buffer.seek(0)

    try:
        with Image.open(buffer) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    except Exception:
        return None


async def send_collage(chat_id, images, settings, caption):
    image = make_collage(images, settings)
    data, extension = encode_image(image, settings)
    filename = f"collage_{uuid.uuid4().hex[:8]}.{extension}"

    await bot.send_document(
        chat_id,
        BufferedInputFile(data, filename=filename),
        caption=caption
    )

    return {
        "name": filename,
        "data": data,
        "created": time.time()
    }


async def process_ready(uid, chat_id):
    s = session(uid)
    mode = s["mode"]
    made = 0

    while len(s["photos"]) >= mode:
        batch = s["photos"][:mode]
        del s["photos"][:mode]

        try:
            output = await send_collage(
                chat_id,
                batch,
                s["settings"],
                f"✅ Collage ready ({mode} photos)."
            )

            s["outputs"].append(output)
            s["outputs"] = s["outputs"][-30:]
            made += 1

        except Exception:
            await bot.send_message(
                chat_id,
                "❌ Collage create nahi ho paya. Dobara try karo."
            )
            break

    if made:
        await bot.send_message(
            chat_id,
            f"Done: <b>{made}</b> collage(s). "
            f"Pending photos: <b>{len(s['photos'])}</b>."
        )
    elif s["photos"]:
        await bot.send_message(
            chat_id,
            f"📥 Pending: {len(s['photos'])}/{mode}. "
            "Collage complete karne ke liye aur photos bhejo."
        )


async def collect_album(message):
    uid = message.from_user.id
    key = (uid, message.media_group_id or str(message.message_id))

    previous = ALBUM_TASKS.get(key)
    if previous:
        previous.cancel()

    async def delayed():
        try:
            await asyncio.sleep(1.2)
            await process_ready(uid, message.chat.id)
        except asyncio.CancelledError:
            pass
        finally:
            ALBUM_TASKS.pop(key, None)

    ALBUM_TASKS[key] = asyncio.create_task(delayed())


@dp.message(CommandStart())
async def start(message: Message):
    session(message.from_user.id)
    await message.answer(
        "👋 <b>Welcome to Collage Maker!</b>\n\n"
        "Choose 2×1, 6×1, or 8×1 mode, then send your photos.",
        reply_markup=keyboard()
    )


@dp.message(Command("help"))
@dp.message(F.text == "❓ Help")
async def help_cmd(message: Message):
    await message.answer(
        "<b>Collage Maker Help</b>\n\n"
        "/mode 2, /mode 6, /mode 8 — select batch size\n"
        "/pending — show pending photo count\n"
        "/make — create collage if enough photos are pending\n"
        "/drop_pending — remove pending photos\n"
        "/convert 2x2 — combine recent collages into a grid\n"
        "/settings — show image settings\n"
        "/fit contain or /fit cover\n"
        "/gap 12 — spacing from 0 to 80\n"
        "/border 12 — border from 0 to 100\n"
        "/bg #20232a — background colour\n"
        "/format jpg or /format png\n"
        "/quality 92 — JPG quality from 50 to 100\n"
        "/tile 640x360 — tile size\n"
        "/save_layout name — save current settings\n"
        "/use_layout name — load saved settings\n"
        "/my_layouts — list saved layouts\n"
        "/delete_layout name — delete a layout\n"
        "/reference — send a reference image\n"
        "/my_collages — view recent collages\n"
        "/clear — clear current photos and collages",
        reply_markup=keyboard()
    )


@dp.message(Command("mode"))
async def mode_cmd(message: Message):
    parts = (message.text or "").split()

    if len(parts) != 2 or parts[1] not in {"2", "6", "8"}:
        return await message.answer("Use /mode 2, /mode 6, or /mode 8.")

    session(message.from_user.id)["mode"] = int(parts[1])
    await message.answer(f"✅ Mode set to {parts[1]} photos per collage.")


@dp.message(F.text.in_({
    "🧩 2×1 Collage",
    "🧩 6×1 Collage",
    "🧩 8×1 Collage"
}))
async def mode_button(message: Message):
    text = message.text
    mode = 2 if "2×1" in text else 6 if "6×1" in text else 8
    session(message.from_user.id)["mode"] = mode
    await message.answer(f"✅ Mode set to {mode} photos per collage.")


@dp.message(Command("pending"))
async def pending_cmd(message: Message):
    s = session(message.from_user.id)
    await message.answer(
        f"📥 Pending photos: {len(s['photos'])}/{s['mode']}"
    )


@dp.message(Command("drop_pending"))
async def drop_pending(message: Message):
    s = session(message.from_user.id)
    count = len(s["photos"])
    s["photos"].clear()
    await message.answer(f"🗑 Removed {count} pending photos.")


@dp.message(Command("make"))
async def make_cmd(message: Message):
    s = session(message.from_user.id)

    if len(s["photos"]) < s["mode"]:
        return await message.answer(
            f"Need {s['mode']} photos; currently {len(s['photos'])} pending."
        )

    await process_ready(message.from_user.id, message.chat.id)


@dp.message(Command("convert"))
async def convert_cmd(message: Message):
    s = session(message.from_user.id)
    parts = (message.text or "").split()

    if len(parts) != 2 or not re.fullmatch(
        r"\d{1,2}x\d{1,2}", parts[1].lower()
    ):
        return await message.answer("Use /convert 4x1, /convert 1x4, or /convert 2x2.")

    cols, rows = map(int, parts[1].lower().split("x"))

    if cols < 1 or rows < 1 or cols * rows > 20:
        return await message.answer("Grid must contain 1–20 collages.")

    count = cols * rows
    outputs = s["outputs"][-count:]

    if len(outputs) < count:
        return await message.answer(
            f"Need {count} generated collages; only {len(outputs)} available."
        )

    images = []
    for item in outputs:
        try:
            images.append(Image.open(io.BytesIO(item["data"])).convert("RGB"))
        except Exception:
            pass

    if len(images) != count:
        return await message.answer("Some collage files are unavailable.")

    tw = min(MAX_OUTPUT_SIDE, max(image.width for image in images))
    th = min(MAX_OUTPUT_SIDE, max(image.height for image in images))
    bg = decode_color(s["settings"]["background"])
    gap = max(0, min(int(s["settings"]["gap"]), 80))

    canvas = Image.new(
        "RGB",
        (cols * tw + (cols - 1) * gap,
         rows * th + (rows - 1) * gap),
        bg
    )

    for index, image in enumerate(images):
        tile = fit_image(image, (tw, th), "contain", bg)
        canvas.paste(
            tile,
            ((index % cols) * (tw + gap),
             (index // cols) * (th + gap))
        )

    canvas.thumbnail(
        (MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE),
        Image.Resampling.LANCZOS
    )
    data, extension = encode_image(canvas, s["settings"])
    filename = f"converted_{uuid.uuid4().hex[:8]}.{extension}"

    s["outputs"].append({
        "name": filename,
        "data": data,
        "created": time.time()
    })
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"🔗 Combined {count} collages into a {cols}×{rows} grid."
    )


@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Settings")
async def settings_cmd(message: Message):
    s = session(message.from_user.id)["settings"]

    await message.answer(
        f"<b>Current settings</b>\n"
        f"Fit: {s['fit']} | Gap: {s['gap']} | Border: {s['border']}\n"
        f"Background: {s['background']} | Format: {s['format']}\n"
        f"Quality: {s['quality']} | Tile: {s['tile_width']}×{s['tile_height']}\n\n"
        "Use /fit contain, /fit cover, /gap 12, /border 12, "
        "/bg white, /format jpg, /quality 92, or /tile 640x360.",
        reply_markup=buttons([
            [("Fit: Contain", "fit:contain"), ("Fit: Cover", "fit:cover")],
            [("JPG", "format:JPG"), ("PNG", "format:PNG")],
            [("Reset settings", "settings:reset")]
        ])
    )


@dp.callback_query(F.data.startswith("fit:"))
async def cb_fit(callback: CallbackQuery):
    session(callback.from_user.id)["settings"]["fit"] = callback.data.split(":")[1]
    save_settings(callback.from_user.id)
    await callback.answer("Fit updated")
    await callback.message.edit_text(
        f"Fit set to {session(callback.from_user.id)['settings']['fit']}."
    )


@dp.callback_query(F.data.startswith("format:"))
async def cb_format(callback: CallbackQuery):
    session(callback.from_user.id)["settings"]["format"] = callback.data.split(":")[1]
    save_settings(callback.from_user.id)
    await callback.answer("Format updated")
    await callback.message.edit_text(
        f"Format set to {session(callback.from_user.id)['settings']['format']}."
    )


@dp.callback_query(F.data == "settings:reset")
async def cb_reset(callback: CallbackQuery):
    session(callback.from_user.id)["settings"] = DEFAULT_SETTINGS.copy()
    save_settings(callback.from_user.id)
    await callback.answer("Settings reset")
    await callback.message.edit_text("Settings reset to defaults.")


@dp.message(Command("fit"))
async def fit_cmd(message: Message):
    p = (message.text or "").split()

    if len(p) != 2 or p[1].lower() not in {"contain", "cover"}:
        return await message.answer("Use /fit contain or /fit cover.")

    session(message.from_user.id)["settings"]["fit"] = p[1].lower()
    save_settings(message.from_user.id)
    await message.answer(f"Fit set to {p[1].lower()}.")


@dp.message(Command("gap"))
async def gap_cmd(message: Message):
    p = (message.text or "").split()

    try:
        value = int(p[1])
    except Exception:
        return await message.answer("Use /gap 12 (0–80).")

    if not 0 <= value <= 80:
        return await message.answer("Gap must be 0–80.")

    session(message.from_user.id)["settings"]["gap"] = value
    save_settings(message.from_user.id)
    await message.answer(f"Gap set to {value}px.")


@dp.message(Command("border"))
async def border_cmd(message: Message):
    p = (message.text or "").split()

    try:
        value = int(p[1])
    except Exception:
        return await message.answer("Use /border 12 (0–100).")

    if not 0 <= value <= 100:
        return await message.answer("Border must be 0–100.")

    session(message.from_user.id)["settings"]["border"] = value
    save_settings(message.from_user.id)
    await message.answer(f"Border set to {value}px.")


@dp.message(Command("bg"))
async def bg_cmd(message: Message):
    p = (message.text or "").split(maxsplit=1)

    if len(p) != 2:
        return await message.answer("Use /bg #20232a or /bg white.")

    try:
        ImageColor.getrgb(p[1])
    except Exception:
        return await message.answer("Invalid colour. Try #20232a, black, or white.")

    session(message.from_user.id)["settings"]["background"] = p[1]
    save_settings(message.from_user.id)
    await message.answer(f"Background set to {p[1]}.")


@dp.message(Command("format"))
async def format_cmd(message: Message):
    p = (message.text or "").split()

    if len(p) != 2 or p[1].lower() not in {"jpg", "jpeg", "png"}:
        return await message.answer("Use /format jpg or /format png.")

    session(message.from_user.id)["settings"]["format"] = (
        "PNG" if p[1].lower() == "png" else "JPG"
    )
    save_settings(message.from_user.id)
    await message.answer("Output format updated.")


@dp.message(Command("quality"))
async def quality_cmd(message: Message):
    p = (message.text or "").split()

    try:
        value = int(p[1])
    except Exception:
        return await message.answer("Use /quality 92 (50–100).")

    if not 50 <= value <= 100:
        return await message.answer("Quality must be 50–100.")

    session(message.from_user.id)["settings"]["quality"] = value
    save_settings(message.from_user.id)
    await message.answer(f"JPG quality set to {value}.")


@dp.message(Command("tile"))
async def tile_cmd(message: Message):
    p = (message.text or "").split()

    if len(p) != 2 or not re.fullmatch(r"\d{3,4}x\d{3,4}", p[1].lower()):
        return await message.answer("Use /tile 640x360.")

    width, height = map(int, p[1].lower().split("x"))

    if not (300 <= width <= 1600 and 200 <= height <= 1200):
        return await message.answer("Width must be 300–1600 and height 200–1200.")

    session(message.from_user.id)["settings"].update(
        tile_width=width, tile_height=height
    )
    save_settings(message.from_user.id)
    await message.answer(f"Tile size set to {width}×{height}.")


@dp.message(Command("save_layout"))
async def save_layout(message: Message):
    p = (message.text or "").split(maxsplit=1)

    if len(p) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", p[1]):
        return await message.answer("Use /save_layout brawl.")

    uid, name = message.from_user.id, p[1].lower()
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO layouts(user_id,name,settings) VALUES(?,?,?)",
        (uid, name, json.dumps(session(uid)["settings"]))
    )
    conn.commit()
    conn.close()

    await message.answer(f"💾 Saved layout <b>{name}</b>.")


@dp.message(Command("use_layout"))
async def use_layout(message: Message):
    p = (message.text or "").split(maxsplit=1)

    if len(p) != 2:
        return await message.answer("Use /use_layout brawl.")

    uid = message.from_user.id
    conn = db()
    row = conn.execute(
        "SELECT settings FROM layouts WHERE user_id=? AND name=?",
        (uid, p[1].lower())
    ).fetchone()
    conn.close()

    if not row:
        return await message.answer("Layout not found. Use /my_layouts.")

    settings = DEFAULT_SETTINGS.copy()
    settings.update(json.loads(row[0]))
    session(uid)["settings"] = settings
    save_settings(uid)

    await message.answer(f"✅ Loaded layout <b>{p[1].lower()}</b>.")


@dp.message(Command("my_layouts"))
async def my_layouts(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT name FROM layouts WHERE user_id=? ORDER BY name",
        (message.from_user.id,)
    ).fetchall()
    conn.close()

    names = ", ".join(row[0] for row in rows) if rows else "none yet"
    await message.answer(f"Saved layouts: {names}\nSave using /save_layout name.")


@dp.message(Command("delete_layout"))
async def delete_layout(message: Message):
    p = (message.text or "").split(maxsplit=1)

    if len(p) != 2:
        return await message.answer("Use /delete_layout brawl.")

    conn = db()
    cur = conn.execute(
        "DELETE FROM layouts WHERE user_id=? AND name=?",
        (message.from_user.id, p[1].lower())
    )
    conn.commit()
    deleted = cur.rowcount
    conn.close()

    await message.answer("Deleted." if deleted else "Layout not found.")


@dp.message(Command("reference"))
async def reference_cmd(message: Message):
    session(message.from_user.id)["waiting"] = "reference"
    await message.answer(
        "Send a reference collage image. "
        "I will estimate its orientation; exact layout detection is not supported."
    )


@dp.message(Command("my_collages"))
@dp.message(F.text == "🖼 My Collages")
async def my_collages(message: Message):
    outputs = session(message.from_user.id)["outputs"]

    if not outputs:
        return await message.answer("No collages generated in this session yet.")

    rows = []
    start = max(0, len(outputs) - 10)

    for index in range(start, len(outputs)):
        rows.append([
            (
                f"📄 {index + 1}. {outputs[index]['name'][:22]}",
                f"out:{index}"
            )
        ])

    rows.append([("🗑 Delete all outputs", "outputs:clear")])
    await message.answer(
        "Your latest collages:",
        reply_markup=buttons(rows)
    )


@dp.callback_query(F.data.startswith("out:"))
async def output_action(callback: CallbackQuery):
    outputs = session(callback.from_user.id)["outputs"]

    try:
        index = int(callback.data.split(":")[1])
    except Exception:
        return await callback.answer("Invalid item.", show_alert=True)

    if not 0 <= index < len(outputs):
        return await callback.answer("This item is unavailable.", show_alert=True)

    item = outputs[index]
    await callback.message.answer_document(
        BufferedInputFile(item["data"], filename=item["name"])
    )
    await callback.answer()


@dp.callback_query(F.data == "outputs:clear")
async def clear_outputs(callback: CallbackQuery):
    await callback.message.answer(
        "Delete all current generated collages?",
        reply_markup=buttons([
            [("Yes, delete", "clear:outputs:yes"), ("Cancel", "clear:no")]
        ])
    )
    await callback.answer()


@dp.callback_query(F.data == "clear:outputs:yes")
async def clear_outputs_yes(callback: CallbackQuery):
    session(callback.from_user.id)["outputs"].clear()
    await callback.message.answer("🗑 Generated collages cleared.")
    await callback.answer()


@dp.message(Command("clear"))
@dp.message(F.text == "🧹 Clear All")
async def clear_cmd(message: Message):
    await message.answer(
        "Clear current pending photos and generated collages? Saved layouts remain.",
        reply_markup=buttons([
            [("🗑 Confirm clear", "clear:all:yes"), ("Cancel", "clear:no")]
        ])
    )


@dp.callback_query(F.data == "clear:all:yes")
async def clear_all_yes(callback: CallbackQuery):
    s = session(callback.from_user.id)
    s["photos"].clear()
    s["outputs"].clear()
    s["waiting"] = None

    await callback.message.answer("✅ Current photos and collages cleared.")
    await callback.answer()


@dp.callback_query(F.data == "clear:no")
async def clear_no(callback: CallbackQuery):
    await callback.message.answer("Cancelled.")
    await callback.answer()


@dp.message(F.photo)
async def photo_handler(message: Message):
    uid = message.from_user.id
    s = session(uid)

    image = await download_photo(message)

    if image is None:
        return await message.answer("❌ Could not read this image. Please try again.")

    if s["waiting"] == "reference":
        s["waiting"] = None
        width, height = image.size

        if width >= height:
            s["settings"].update(tile_width=640, tile_height=360, fit="contain")
        else:
            s["settings"].update(tile_width=360, tile_height=640, fit="contain")

        save_settings(uid)
        return await message.answer(
            f"🧭 Reference noted ({width}×{height}). "
            "Adjust using /tile and /fit, then save with /save_layout name."
        )

    if len(s["photos"]) >= MAX_PHOTOS:
        return await message.answer(
            "Too many pending photos. Use /make or /drop_pending."
        )

    s["photos"].append(image)
    await collect_album(message)


@dp.message(F.text)
async def unknown_text(message: Message):
    if (message.text or "").startswith("/"):
        return

    await message.answer(
        "Choose a button or send photos. Type /help for commands.",
        reply_markup=keyboard()
    )


async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "telegram-collage-maker"
    })


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
    runner = await start_health_server()
    print(f"Health server listening on port {PORT}")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
    
                
