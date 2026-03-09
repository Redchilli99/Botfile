import os
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

# --- Flask Server for Render ---
web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    MONGO_URI = os.getenv("MONGO_URI")
    DATABASE_CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
    AUTO_DELETE_HOURS = 6
    ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []

# --- Database Manager ---
class MongoManager:
    def __init__(self, uri: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client.filesbhejo_db
        self.files = self.db.files
        self.shares = self.db.shared_files
        self.users = self.db.users
        self.stats = self.db.stats

    async def create_indexes(self):
        await self.files.create_index([("file_name", ASCENDING)])
        await self.files.create_index([("category", ASCENDING)])
        await self.files.create_index([("created_at", DESCENDING)])
        await self.shares.create_index([("user_id", ASCENDING)])
        await self.stats.create_index([("file_id", ASCENDING)])

db = MongoManager(Config.MONGO_URI)

# --- Helper Functions ---
async def get_user_data(user_id: int):
    user = await db.users.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "first_name": "",
            "favorites": [],
            "downloads": 0,
            "joined_at": datetime.now().isoformat()
        }
        await db.users.insert_one(user)
    return user

async def log_download(user_id: int, file_id: str):
    await db.stats.update_one(
        {"file_id": file_id},
        {"$inc": {"download_count": 1}, "$set": {"last_downloaded": datetime.now().isoformat()}},
        upsert=True
    )
    await db.users.update_one(
        {"user_id": user_id},
        {"$inc": {"downloads": 1}}
    )

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user_data(update.effective_user.id)
    await db.users.update_one(
        {"user_id": update.effective_user.id},
        {"$set": {"first_name": update.effective_user.first_name}}
    )
    
    kb = [
        [InlineKeyboardButton("📁 Browse Files", callback_data="browse_0")],
        [InlineKeyboardButton("🔍 Search Files", callback_data="search")],
        [InlineKeyboardButton("❤️ Favorites", callback_data="favorites")],
        [InlineKeyboardButton("📊 My Stats", callback_data="stats")]
    ]
    
    if update.effective_user.id in Config.ADMIN_IDS:
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    
    await update.message.reply_text(
        f"👋 Welcome {update.effective_user.first_name}!
\n📂 Share and manage files easily.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != Config.DATABASE_CHANNEL_ID:
        return
    
    msg = update.effective_message
    file_obj = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)
    
    if file_obj:
        file_data = {
            "file_id": file_obj.file_id,
            "file_name": getattr(file_obj, 'file_name', f'File_{msg.message_id}'),
            "message_id": msg.message_id,
            "file_type": "document" if msg.document else ("video" if msg.video else ("audio" if msg.audio else "photo")),
            "file_size": getattr(file_obj, 'file_size', 0),
            "category": "Uncategorized",
            "created_at": datetime.now().isoformat(),
            "uploaded_by": msg.from_user.id if msg.from_user else None
        }
        
        await db.files.update_one(
            {"file_id": file_obj.file_id},
            {"$set": file_data},
            upsert=True
        )
        logger.info(f"Indexed file: {file_data['file_name']}")

async def browse_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split("_")[1])
    files = await db.files.find().sort("created_at", -1).to_list(100)
    
    keyboard = []
    files_per_page = 5
    start = page * files_per_page
    end = start + files_per_page
    total = len(files)
    
    for f in files[start:end]:
        stats = await db.stats.find_one({"file_id": f['file_id']}) or {}
        downloads = stats.get('download_count', 0)
        btn_text = f"📁 {f['file_name'][:25]} [{downloads}]"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"dl_{f['file_id']}" )])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"browse_{page-1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"browse_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🏠 Back", callback_data="back_menu")])
    
    info_text = f"📂 All Files ({total} total)\nPage {page + 1}"
    await query.edit_message_text(info_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def search_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔍 Search Files\n\nSend me the filename to search:")
    context.user_data['searching'] = True

async def handle_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('searching'):
        return
    
    search_term = update.message.text.lower()
    context.user_data['searching'] = False
    
    files = await db.files.find({
        "file_name": {"$regex": search_term, "$options": "i"}
    }).to_list(100)
    
    if not files:
        await update.message.reply_text("❌ No files found!")
        return
    
    keyboard = []
    for f in files[:5]:
        stats = await db.stats.find_one({"file_id": f['file_id']}) or {}
        downloads = stats.get('download_count', 0)
        btn_text = f"📁 {f['file_name'][:25]} [{downloads}]"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"dl_{f['file_id']}" )])
    
    keyboard.append([InlineKeyboardButton("🏠 Back", callback_data="back_menu")])
    
    await update.message.reply_text(
        f"🔍 Search Results ({len(files)} found)",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    file_id = query.data[3:]
    file_data = await db.files.find_one({"file_id": file_id})
    
    if not file_data:
        await query.answer("❌ File not found!", show_alert=True)
        return
    
    try:
        sent = await context.bot.copy_message(
            update.effective_user.id,
            Config.DATABASE_CHANNEL_ID,
            file_data['message_id']
        )
        
        await log_download(update.effective_user.id, file_id)
        stats = await db.stats.find_one({"file_id": file_id}) or {}
        downloads = stats.get('download_count', 1)
        
        await context.bot.send_message(
            update.effective_user.id,
            f"✅ Downloaded: {file_data['file_name']}\n📊 Total Downloads: {downloads}"
        )
        
        await db.shares.insert_one({
            "user_id": update.effective_user.id,
            "file_id": file_id,
            "msg_id": sent.message_id,
            "time": datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error sending file: {e}")
        await query.answer("❌ Error sending file!", show_alert=True)

async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = await get_user_data(update.effective_user.id)
    favorites = user.get('favorites', [])
    
    if not favorites:
        kb = [[InlineKeyboardButton("🏠 Back", callback_data="back_menu")]]
        await query.edit_message_text("❤️ Your Favorites\n\nNo favorites yet!", reply_markup=InlineKeyboardMarkup(kb))
        return
    
    files = await db.files.find({"file_id": {"$in": favorites}}).to_list(100)
    keyboard = []
    
    for f in files:
        stats = await db.stats.find_one({"file_id": f['file_id']}) or {}
        downloads = stats.get('download_count', 0)
        btn_text = f"📁 {f['file_name'][:25]} [{downloads}]"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"dl_{f['file_id']}" )])
    
    keyboard.append([InlineKeyboardButton("🏠 Back", callback_data="back_menu")])
    
    await query.edit_message_text(
        f"❤️ Your Favorites ({len(files)})",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = await get_user_data(update.effective_user.id)
    
    stats_text = (
        f"📊 Your Statistics\n\n"
        f"👤 Name: {user.get('first_name', 'Unknown')}\n"
        f"⬇️ Downloads: {user.get('downloads', 0)}\n"
        f"❤️ Favorites: {len(user.get('favorites', []))}\n"
        f"📅 Joined: {user.get('joined_at', 'Unknown')[:10]}"
    )
    
    kb = [[InlineKeyboardButton("🏠 Back", callback_data="back_menu")]]
    await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(kb))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id not in Config.ADMIN_IDS:
        await query.answer("❌ Unauthorized!", show_alert=True)
        return
    
    total_files = await db.files.count_documents({})
    total_users = await db.users.count_documents({})
    
    kb = [
        [InlineKeyboardButton(f"📋 Files: {total_files}", callback_data="admin_list")],
        [InlineKeyboardButton(f"👥 Users: {total_users}", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("🏠 Back", callback_data="back_menu")]
    ]
    
    await query.edit_message_text("⚙️ Admin Panel", reply_markup=InlineKeyboardMarkup(kb))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def delete_worker(app: Application):
    while True:
        try:
            cutoff = (datetime.now() - timedelta(hours=Config.AUTO_DELETE_HOURS)).isoformat()
            async for s in db.shares.find({"time": {"$lt": cutoff}}):
                try:
                    await app.bot.delete_message(s['user_id'], s['msg_id'])
                except:
                    pass
                await db.shares.delete_one({"_id": s["_id"]})
            
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Error in delete_worker: {e}")
            await asyncio.sleep(3600)

async def post_init(app: Application):
    await db.create_indexes()
    logger.info("Database indexes created!")

def main():
    threading.Thread(target=run_web, daemon=True).start()
    
    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(browse_files, pattern="^browse_"))
    app.add_handler(CallbackQueryHandler(search_files, pattern="^search$"))
    app.add_handler(CallbackQueryHandler(send_file_to_user, pattern="^dl_"))
    app.add_handler(CallbackQueryHandler(show_favorites, pattern="^favorites"))
    app.add_handler(CallbackQueryHandler(show_stats, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_menu$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_input))
    app.add_handler(MessageHandler(filters.Chat(Config.DATABASE_CHANNEL_ID), handle_channel_post))
    
    app.post_init = post_init
    asyncio.get_event_loop().create_task(delete_worker(app))
    
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()