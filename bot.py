import asyncio
import logging
import os
import zipfile
import tempfile
from pathlib import Path
from io import BytesIO


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram import BotCommand
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

_raw_ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = (
    {int(uid) for uid in _raw_ids.split(",") if uid.strip()}
    if _raw_ids
    else set()
)

MAX_FILE_SIZE_MB = 40
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

# Only one archive processed at a time to protect RAM
processing_semaphore = asyncio.Semaphore(1)

CHOOSING_RESOLUTION = 0


def _build_auth_filter():
    if ALLOWED_USER_IDS:
        logger.info("Auth enabled for user IDs: %s", ALLOWED_USER_IDS)
        return filters.User(user_id=ALLOWED_USER_IDS)
    logger.warning("ALLOWED_USER_IDS not set — bot is open to everyone")
    return filters.ALL


auth_filter = _build_auth_filter()


def resize_image_bytes(data: bytes, suffix: str, target_w: int, target_h: int) -> bytes:
    img = Image.open(BytesIO(data))

    # Scale to cover the target (fill), then center-crop to exact dimensions
    ratio = max(target_w / img.width, target_h / img.height)
    scaled_w = max(int(img.width * ratio), target_w)
    scaled_h = max(int(img.height * ratio), target_h)
    img = img.resize((scaled_w, scaled_h), Image.LANCZOS)
    left = (scaled_w - target_w) // 2
    top = (scaled_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))

    fmt = suffix.lstrip(".")
    if fmt in ("jpg", "jpeg"):
        fmt = "JPEG"
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
    elif fmt == "png":
        fmt = "PNG"
    elif fmt == "webp":
        fmt = "WEBP"
    elif fmt in ("tiff", "tif"):
        fmt = "TIFF"
    else:
        fmt = "BMP"

    buf = BytesIO()
    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs = {"quality": 92, "optimize": True}
    elif fmt == "PNG":
        save_kwargs = {"optimize": True}
    elif fmt == "WEBP":
        save_kwargs = {"quality": 92}

    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


CONTAINER_NAME = "telegram-bot-api"


async def _container_file_ready(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", CONTAINER_NAME, "test", "-f", str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _copy_from_container(container_path: Path, dest: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "cp", f"{CONTAINER_NAME}:{container_path}", str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"docker cp failed: {stderr.decode().strip()}")


async def unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.warning("Unauthorized access attempt from user_id=%s username=%s", user.id, user.username)
    await update.effective_message.reply_text("You are not authorized to use this bot.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hello! Send me a .zip archive with photos and I will resize all of them.\n\n"
        "Maximum archive size: 40 MB.\n"
        "Supported formats: JPG, PNG, WebP, BMP, TIFF."
    )


async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document

    if not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("Please send a .zip archive.")
        return ConversationHandler.END

    if doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"Archive is too large. Maximum size is {MAX_FILE_SIZE_MB} MB.")
        return ConversationHandler.END

    context.user_data["file_id"] = doc.file_id
    context.user_data["waiting_custom"] = False

    keyboard = [
        [
            InlineKeyboardButton("1920×1080", callback_data="1920x1080"),
            InlineKeyboardButton("1080×1920", callback_data="1080x1920"),
        ],
        [
            InlineKeyboardButton("1280×720", callback_data="1280x720"),
            InlineKeyboardButton("Custom...", callback_data="custom"),
        ],
    ]
    await update.message.reply_text(
        "Archive received! Choose the output resolution:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_RESOLUTION


async def handle_resolution_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "custom":
        context.user_data["waiting_custom"] = True
        await query.edit_message_text("Enter the resolution as WIDTHxHEIGHT, e.g. 2560x1440:")
        return CHOOSING_RESOLUTION

    target_w, target_h = map(int, query.data.split("x"))
    await query.edit_message_text(f"Processing… resizing to fit {target_w}×{target_h}")
    await _run_processing(context, query.message.chat_id, target_w, target_h)
    return ConversationHandler.END


async def handle_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("waiting_custom"):
        return ConversationHandler.END

    text = update.message.text.strip().lower().replace(" ", "")
    try:
        w_str, h_str = text.split("x")
        target_w, target_h = int(w_str), int(h_str)
        if not (1 <= target_w <= 7680 and 1 <= target_h <= 4320):
            raise ValueError
    except (ValueError, AttributeError):
        await update.message.reply_text("Invalid format. Use WIDTHxHEIGHT, e.g. 2560x1440 (max 7680x4320):")
        return CHOOSING_RESOLUTION

    context.user_data["waiting_custom"] = False
    await update.message.reply_text(f"Processing… resizing to fit {target_w}×{target_h}")
    await _run_processing(context, update.message.chat_id, target_w, target_h)
    return ConversationHandler.END


async def _run_processing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    target_w: int,
    target_h: int,
) -> None:
    async with processing_semaphore:
        file_id = context.user_data["file_id"]
        try:
            await _process_and_send(context, chat_id, file_id, target_w, target_h)
        except Exception:
            logger.exception("Processing failed for chat %s", chat_id)
            await context.bot.send_message(chat_id, "Something went wrong while processing the archive. Please try again.")


async def _process_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_id: str,
    target_w: int,
    target_h: int,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        tg_file = await context.bot.get_file(file_id)
        zip_in = tmp / "input.zip"

        # getFile triggers an async download inside the local server container.
        # Extract the container-side absolute path from PTB's URL, then poll
        # via docker exec until the file is ready, and copy it out with docker cp.
        fp = tg_file.file_path
        idx = fp.find("/var/lib/telegram-bot-api")
        if idx >= 0:
            container_path = Path(fp[idx:])
        else:
            parts = fp.split(f"/{BOT_TOKEN}/")
            rel = parts[-1] if len(parts) > 1 else fp.lstrip("/")
            container_path = Path(f"/var/lib/telegram-bot-api/{BOT_TOKEN}/{rel}")

        logger.info("Waiting for container file: %s", container_path)
        for _ in range(300):
            if await _container_file_ready(container_path):
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError(f"File not ready after 5 minutes: {container_path}")

        await _copy_from_container(container_path, zip_in)

        # Remove the file from inside the container to prevent disk accumulation
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", CONTAINER_NAME, "rm", "-f", str(container_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        zip_out = tmp / "output.zip"
        processed = 0
        skipped = 0

        with (
            zipfile.ZipFile(zip_in, "r") as zin,
            zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zout,
        ):
            for item in zin.infolist():
                if item.is_dir():
                    continue

                suffix = Path(item.filename).suffix.lower()

                if suffix not in IMAGE_EXTENSIONS:
                    zout.writestr(item, zin.read(item.filename))
                    continue

                try:
                    resized = await asyncio.get_event_loop().run_in_executor(
                        None,
                        resize_image_bytes,
                        zin.read(item.filename),
                        suffix,
                        target_w,
                        target_h,
                    )
                    zout.writestr(item.filename, resized)
                    processed += 1
                except Exception:
                    logger.exception("Could not resize %s", item.filename)
                    zout.writestr(item, zin.read(item.filename))
                    skipped += 1

        caption = f"Done! {processed} image(s) resized to fit {target_w}×{target_h}."
        if skipped:
            caption += f"\n{skipped} file(s) could not be resized and are included unchanged."

        with open(zip_out, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=f"resized_{target_w}x{target_h}.zip",
                caption=caption,
            )

        await context.bot.send_message(
            chat_id=chat_id,
            text="Send another .zip archive whenever you're ready.",
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url("http://127.0.0.1:8081/bot")
        .base_file_url("http://127.0.0.1:8081/file/bot")
        .local_mode(True)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(auth_filter & filters.Document.FileExtension("zip"), handle_zip)
        ],
        states={
            CHOOSING_RESOLUTION: [
                CallbackQueryHandler(handle_resolution_button),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start, filters=auth_filter))
    app.add_handler(conv)

    # Catch-all for anyone not in the allowlist
    if ALLOWED_USER_IDS:
        app.add_handler(MessageHandler(~auth_filter, unauthorized))

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Show welcome message"),
            BotCommand("cancel", "Cancel current operation"),
        ])

    app.post_init = post_init

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
