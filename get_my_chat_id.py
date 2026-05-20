"""Script auxiliar: arranca el bot en modo descubrir-chat-id.
Manda /start al bot @soykiimo_bot y veras tu chat_id aca.
Apretar Ctrl+C cuando lo tengas."""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from skiimo.config import TELEGRAM_BOT_TOKEN


async def show(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    print(f"\nchat_id: {chat_id}")
    print(f"user_id: {user.id if user else '?'}")
    print(f"nombre:  {user.full_name if user else '?'}")
    print(f"username: @{user.username if user and user.username else '?'}")
    await update.message.reply_text(
        f"Tu chat_id es:\n\n{chat_id}\n\n"
        f"Pasaselo al admin para que te de de alta."
    )


def main() -> None:
    print("Esperando mensajes. Manda algo al bot @soykiimo_bot y veras tu chat_id aca.")
    print("Ctrl+C para detener.\n")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", show))
    app.add_handler(MessageHandler(filters.ALL, show))
    app.run_polling()


if __name__ == "__main__":
    main()
