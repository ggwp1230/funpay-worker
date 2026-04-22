"""
FunPay Pulse — Telegram Bot
Выдаёт OTP коды для установки воркера на VPS
"""
import os
import random
import string
import time
import json
import hashlib
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── Конфиг ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS   = set(int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip())
OTP_DIR     = Path("/opt/funpay-server/otps")
OTP_DIR.mkdir(parents=True, exist_ok=True)
OTP_TTL     = 300  # секунд (5 минут)

# ── OTP helpers ───────────────────────────────────────────────────────────────
def generate_otp() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def save_otp(otp: str, user_id: int):
    data = {"otp": otp, "user_id": user_id, "created_at": time.time()}
    (OTP_DIR / f"{otp}.json").write_text(json.dumps(data))

def validate_otp(otp: str) -> tuple[bool, str]:
    f = OTP_DIR / f"{otp}.json"
    if not f.exists():
        return False, "invalid_otp"
    data = json.loads(f.read_text())
    if time.time() - data["created_at"] > OTP_TTL:
        f.unlink()
        return False, "expired_otp"
    f.unlink()  # одноразовый
    return True, "ok"

def cleanup_otps():
    for f in OTP_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if time.time() - data["created_at"] > OTP_TTL:
                f.unlink()
        except Exception:
            pass

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот FunPay Pulse.\n\n"
        "Команды:\n"
        "/vps — получить код для установки воркера\n"
        "/help — помощь"
    )

async def cmd_vps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Только админы могут получать коды
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас нет доступа.")
        return

    cleanup_otps()
    otp = generate_otp()
    save_otp(otp, user_id)

    await update.message.reply_text(
        f"🔑 Ваш код для установки воркера:\n\n"
        f"`{otp}`\n\n"
        f"⏱ Действителен 5 минут\n\n"
        f"Запустите на VPS:\n"
        f"`curl -sSL https://raw.githubusercontent.com/ggwp1230/funpay-worker/main/vps_server/install.sh | sudo bash`",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Помощь:\n\n"
        "/vps — получить OTP код для установки воркера на VPS\n\n"
        "После получения кода запустите скрипт установки на VPS и введите код."
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("vps",   cmd_vps))
    app.add_handler(CommandHandler("help",  cmd_help))
    print(f"[Bot] Starting... Admin IDs: {ADMIN_IDS}")
    app.run_polling()

if __name__ == "__main__":
    main()
