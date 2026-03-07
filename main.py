import os
import logging
import asyncio
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from motor.motor_asyncio import AsyncIOMotorClient

# --- Configuration (Pulled from Render Dashboard) ---
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    MONGO_URI = os.getenv("MONGO_URI")
    DATABASE_CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
    AUTO_DELETE_HOURS = 6

# --- Data Models ---
@dataclass
class FileRecord:
    file_id: str
    file_name: str
    message_id: int
    added_date: str

@dataclass
class SharedFile:
    share_id: str
    user_id: int
    bot_message_id: int
    chat_id: int
    shared_at: str

# --- Database Manager ---
class MongoManager:
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client.filesbhejo_db
        self.files = self.db.files
        self.shares = self.db.shared_files

    async def save_file(self, record: FileRecord):
        await self.files.update_one({"file_id": record.file_id}, {"$set": asdict(record)}, upsert=True)

# Initialize DB
db = MongoManager(Config.MONGO_URI)

# --- Bot Functions ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = "👋 Welcome to @filesbhejo_bot!\n\nUse the button below to see available files."
    kb = [[InlineKeyboardButton("📁 Browse Files", callback_data="browse")]]
    await update.message.reply_text(welcome, reply_markup=InlineKeyboardMarkup(kb))

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically indexes files sent to the private channel"""
    if update.effective_chat.id != Config.DATABASE_CHANNEL_ID: return
    msg = update.effective_message
    file_obj = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)
    
    if file_obj:
        rec = FileRecord(
            file_id=file_obj.file_id,
            file_name=getattr(file_obj, 'file_name', 'Unnamed File'),
            message_id=msg.message_id,
            added_date=datetime.now().isoformat()
        )
        await db.save_file(rec)
        await msg.reply_text(f"✅ File Indexed: {rec.file_name}")

async def browse_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    cursor = db.files.find().limit(20)
    keyboard = []
    async for f in cursor:
        keyboard.append([InlineKeyboardButton(f"📥 {f['file_name']}", callback_data=f"dl_{f['file_id']}")])
    
    if not keyboard:
        await query.edit_message_text("📭 The database is currently empty.")
    else:
        await query.edit_message_text("Choose a file to download:", reply_markup=InlineKeyboardMarkup(keyboard))

async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file_id = query.data[3:]
    user_id = update.effective_user.id
    
    file_data = await db.files.find_one({"file_id": file_id})
    if not file_data: return

    sent_msg = await context.bot.copy_message(
        chat_id=user_id,
        from_chat_id=Config.DATABASE_CHANNEL_ID,
        message_id=file_data['message_id'],
        caption=f"📁 {file_data['file_name']}\n\n⏰ This file will auto-delete in {Config.AUTO_DELETE_HOURS} hours."
    )
    
    # Save info for auto-delete
    share = SharedFile(
        share_id=f"{user_id}_{int(time.time())}",
        user_id=user_id,
        bot_message_id=sent_msg.message_id,
        chat_id=user_id,
        shared_at=datetime.now().isoformat()
    )
    await db.shares.insert_one(asdict(share))

async def delete_worker(app: Application):
    """Checks every minute for files that need to be deleted"""
    while True:
        cutoff = (datetime.now() - timedelta(hours=Config.AUTO_DELETE_HOURS)).isoformat()
        cursor = db.shares.find({"shared_at": {"$lt": cutoff}})
        
        async for share in cursor:
            try:
                await app.bot.delete_message(share['chat_id'], share['bot_message_id'])
            except: pass
            await db.shares.delete_one({"_id": share["_id"]})
            
        await asyncio.sleep(60)

def main():
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(browse_files, pattern="^browse$"))
    app.add_handler(CallbackQueryHandler(send_file_to_user, pattern="^dl_"))
    app.add_handler(MessageHandler(filters.Chat(Config.DATABASE_CHANNEL_ID), handle_channel_post))
    
    # Start the auto-delete background task
    asyncio.get_event_loop().create_task(delete_worker(app))
    
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
