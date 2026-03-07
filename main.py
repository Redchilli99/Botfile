import os
import logging
import asyncio
import time
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from motor.motor_asyncio import AsyncIOMotorClient

# --- Flask Server for Render ---
web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    # Render provides a PORT environment variable automatically
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# --- Configuration ---
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    MONGO_URI = os.getenv("MONGO_URI")
    DATABASE_CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
    AUTO_DELETE_HOURS = 6

# --- Database ---
class MongoManager:
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client.filesbhejo_db
        self.files = self.db.files
        self.shares = self.db.shared_files

db = MongoManager(Config.MONGO_URI)

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📁 Browse Files", callback_data="browse")]]
    await update.message.reply_text("👋 Bot is active!", reply_markup=InlineKeyboardMarkup(kb))

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != Config.DATABASE_CHANNEL_ID: return
    msg = update.effective_message
    file_obj = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)
    if file_obj:
        await db.files.update_one(
            {"file_id": file_obj.file_id},
            {"$set": {"file_name": getattr(file_obj, 'file_name', 'File'), "message_id": msg.message_id}},
            upsert=True
        )
        await msg.reply_text("✅ Indexed!")

async def browse_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = []
    async for f in db.files.find().limit(20):
        keyboard.append([InlineKeyboardButton(f"📥 {f['file_name']}", callback_data=f"dl_{f['file_id']}")])
    await query.edit_message_text("Files:", reply_markup=InlineKeyboardMarkup(keyboard))

async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file_id = query.data[3:]
    file_data = await db.files.find_one({"file_id": file_id})
    if file_data:
        sent = await context.bot.copy_message(update.effective_user.id, Config.DATABASE_CHANNEL_ID, file_data['message_id'])
        # Simplified share record for brevity
        await db.shares.insert_one({"chat_id": update.effective_user.id, "msg_id": sent.message_id, "time": datetime.now().isoformat()})

async def delete_worker(app: Application):
    while True:
        cutoff = (datetime.now() - timedelta(hours=Config.AUTO_DELETE_HOURS)).isoformat()
        async for s in db.shares.find({"time": {"$lt": cutoff}}):
            try:
                await app.bot.delete_message(s['chat_id'], s['msg_id'])
            except: pass
            await db.shares.delete_one({"_id": s["_id"]})
        await asyncio.sleep(60)

def main():
    # Start Web Server in background thread
    threading.Thread(target=run_web, daemon=True).start()

    # Start Bot
    app = Application.builder().token(Config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(browse_files, pattern="^browse$"))
    app.add_handler(CallbackQueryHandler(send_file_to_user, pattern="^dl_"))
    app.add_handler(MessageHandler(filters.Chat(Config.DATABASE_CHANNEL_ID), handle_channel_post))
    
    asyncio.get_event_loop().create_task(delete_worker(app))
    app.run_polling()

if __name__ == "__main__":
    main()
