import asyncio
import os
import random
import sqlite3
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import BusinessConnection, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- CONFIGURATION & RENDER PERSISTENT PATHS ---
DATA_DIR = os.getenv("RENDER_DISK_PATH", ".")
DB_FILE = os.path.join(DATA_DIR, "business_campaign.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "your_telegram_bot_token_here")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "your_openrouter_api_key_here")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))  # Main Owner User ID

MODEL = "google/gemini-3.6-flash"
TARGET_BOT_USERNAME = "folkapp_bot"
MAX_QUESTIONS = 50

TOPICS = [
    "space", "cooking", "movies", "history", "tech", "travel",
    "music", "fitness", "animals", "weather", "hobbies", "food"
]

# --- AIOGRAM SETUP ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PENDING_ADMIN_INPUT = set()

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS business_accounts (
            business_connection_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            count INTEGER DEFAULT 0,
            waiting INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            messaging_enabled INTEGER DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_admins (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row)

def get_account_state(conn, business_connection_id: str):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT count, waiting, completed, messaging_enabled, user_name 
        FROM business_accounts 
        WHERE business_connection_id = ?
    """, (business_connection_id,))
    row = cursor.fetchone()
    if row:
        return {
            "count": row[0], 
            "waiting": bool(row[1]), 
            "completed": bool(row[2]),
            "messaging_enabled": bool(row[3]),
            "user_name": row[4]
        }
    return None

def update_account_state(business_connection_id: str, count: int, waiting: bool, completed: bool):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE business_accounts 
        SET count = ?, waiting = ?, completed = ? 
        WHERE business_connection_id = ?
    """, (count, int(waiting), int(completed), business_connection_id))
    conn.commit()
    conn.close()

# --- OPENROUTER AI GENERATOR ---
async def generate_question(last_bot_reply: str) -> str:
    topic = random.choice(TOPICS)
    prompt = (
        f"You are a normal human texting casually on Telegram. The bot just said:\n\"{last_bot_reply}\"\n\n"
        f"Ask a simple, everyday casual question about {topic}. "
        "CRITICAL: Keep it like normal human small talk. NEVER ask deep research, academic, or complex philosophical questions. "
        "Keep it under 12 words and output ONLY the question."
    )
    async with httpx.AsyncClient(timeout=20) as http:
        res = await http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
                "max_tokens": 40,
            },
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()

# --- BUSINESS CONNECTION HANDLER ---
@dp.business_connection()
async def on_business_connection(business_connection: BusinessConnection):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    bc_id = business_connection.id
    user_id = business_connection.user.id
    user_name = business_connection.user.first_name or "User"
    is_enabled = business_connection.is_enabled

    if is_enabled:
        cursor.execute("""
            INSERT INTO business_accounts (business_connection_id, user_id, user_name, count, waiting, completed, is_active, messaging_enabled)
            VALUES (?, ?, ?, 0, 0, 0, 1, 1)
            ON CONFLICT(business_connection_id) DO UPDATE SET is_active = 1
        """, (bc_id, user_id, user_name))
        conn.commit()
        print(f"🔗 Business account connected: {user_name} ({user_id}) [ID: {bc_id}]")
    else:
        cursor.execute("UPDATE business_accounts SET is_active = 0 WHERE business_connection_id = ?", (bc_id,))
        conn.commit()
        print(f"🔌 Business account disconnected: [ID: {bc_id}]")

    conn.close()

# --- BUSINESS MESSAGE HANDLER ---
@dp.business_message()
async def on_business_message(message: Message):
    bc_id = message.business_connection_id
    if not bc_id:
        return

    if not message.from_user or message.from_user.username != TARGET_BOT_USERNAME:
        return

    conn = sqlite3.connect(DB_FILE)
    state = get_account_state(conn, bc_id)
    conn.close()

    if not state or not state["messaging_enabled"] or not state["waiting"] or state["completed"] or state["count"] >= MAX_QUESTIONS:
        return

    update_account_state(bc_id, state["count"], waiting=False, completed=False)

    try:
        await asyncio.sleep(3)
        bot_text = message.text or message.caption or "Hello"
        print(f"[Business ID {bc_id}] Received from @{TARGET_BOT_USERNAME}: {bot_text}")

        await asyncio.sleep(random.uniform(4, 8))
        question = await generate_question(bot_text)
        
        await bot.send_message(
            chat_id=message.chat.id,
            text=question,
            business_connection_id=bc_id
        )
        print(f"[Business ID {bc_id}] Sent question #{state['count'] + 1}: {question}")

        new_count = state["count"] + 1
        if new_count >= MAX_QUESTIONS:
            update_account_state(bc_id, new_count, waiting=False, completed=True)
            print(f"[Business ID {bc_id}] Reached 50 questions limit. Campaign finished.")
        else:
            update_account_state(bc_id, new_count, waiting=True, completed=False)

    except Exception as e:
        print(f"⚠️ Error handling business message for ID {bc_id}: {e}")
        update_account_state(bc_id, state["count"], waiting=True, completed=False)

# --- UI KEYBOARD BUILDERS ---
def get_dashboard_keyboard(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT business_connection_id, user_name, messaging_enabled, completed, count FROM business_accounts")
    rows = cursor.fetchall()
    conn.close()

    builder = InlineKeyboardBuilder()
    for bc_id, user_name, messaging_enabled, completed, count in rows:
        status_icon = "🟢" if messaging_enabled else "🔴"
        comp_str = "✅ Done" if completed else f"{count}/50"
        short_id = bc_id[:12]
        builder.row(
            types.InlineKeyboardButton(
                text=f"{status_icon} {user_name} ({comp_str})",
                callback_data=f"acc_view:{short_id}"
            )
        )
    
    builder.row(
        types.InlineKeyboardButton(text="🚀 Start All Campaigns", callback_data="global_start"),
        types.InlineKeyboardButton(text="⏹ Stop All Campaigns", callback_data="global_stop")
    )
    
    if user_id == OWNER_ID:
        builder.row(types.InlineKeyboardButton(text="👥 Manage Bot Admins", callback_data="admin_manage"))

    builder.row(types.InlineKeyboardButton(text="🔄 Refresh Dashboard", callback_data="dash_refresh"))
    return builder.as_markup()

def get_account_detail_keyboard(short_id: str, messaging_enabled: bool):
    builder = InlineKeyboardBuilder()
    toggle_text = "🔴 Turn Messaging OFF" if messaging_enabled else "🟢 Turn Messaging ON"
    
    builder.row(types.InlineKeyboardButton(text=toggle_text, callback_data=f"acc_toggle:{short_id}"))
    builder.row(types.InlineKeyboardButton(text="🚀 Start/Trigger Greeting", callback_data=f"acc_start_single:{short_id}"))
    builder.row(types.InlineKeyboardButton(text="🔄 Reset Counter (0/50)", callback_data=f"acc_reset:{short_id}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Back to Dashboard", callback_data="dash_home"))
    return builder.as_markup()

def get_admins_keyboard():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM bot_admins")
    rows = cursor.fetchall()
    conn.close()

    builder = InlineKeyboardBuilder()
    for r in rows:
        admin_id = r[0]
        builder.row(
            types.InlineKeyboardButton(text=f"❌ Remove Admin: {admin_id}", callback_data=f"admin_remove:{admin_id}")
        )
    builder.row(types.InlineKeyboardButton(text="➕ Add New Admin", callback_data="admin_add_prompt"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Back to Dashboard", callback_data="dash_home"))
    return builder.as_markup()

# --- MESSAGE ROUTING & ADMIN CHECK ---
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_inputs(message: types.Message):
    user_id = message.from_user.id
    if user_id == OWNER_ID and user_id in PENDING_ADMIN_INPUT:
        PENDING_ADMIN_INPUT.remove(user_id)
        try:
            new_admin_id = int(message.text.strip())
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (new_admin_id,))
            conn.commit()
            conn.close()

            await message.answer(f"✅ User ID `{new_admin_id}` has been granted full admin access!", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await message.answer("⚠️ Invalid User ID. Please enter a valid numeric Telegram User ID.")
        return

    if not is_admin(user_id):
        return

@dp.message(Command("start", "dashboard"))
async def cmd_dashboard(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    
    text = (
        "🤖 **Business Chat Automation UI**\n\n"
        "Welcome! Monitor linked accounts, control campaigns with `@folkapp_bot`, "
        "and manage your team access below:"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_dashboard_keyboard(user_id))

# --- CALLBACK QUERY HANDLERS ---
@dp.callback_query(F.data == "dash_refresh")
async def cb_refresh(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "🤖 **Business Chat Automation UI**\n\nDashboard refreshed live:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_dashboard_keyboard(callback.from_user.id)
    )
    await callback.answer("Refreshed!")

@dp.callback_query(F.data == "dash_home")
async def cb_home(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "🤖 **Business Chat Automation UI**\n\nSelect an account below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_dashboard_keyboard(callback.from_user.id)
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_manage")
async def cb_admin_manage(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Only the owner can manage admins.", show_alert=True)
        return
    
    await callback.message.edit_text(
        "👥 **Bot Admin Management**\n\n"
        "Admins have full access to view accounts, start/stop campaigns, and use the bot normally.\n"
        "Select an admin to remove or add a new one below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_admins_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_add_prompt")
async def cb_admin_add_prompt(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Unauthorized", show_alert=True)
        return
    
    PENDING_ADMIN_INPUT.add(callback.from_user.id)
    await callback.message.answer(
        "✍️ **Add New Admin**\n\n"
        "Please reply to this chat with the numeric Telegram User ID of the person you want to make an admin.",
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_remove:"))
async def cb_admin_remove(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Unauthorized", show_alert=True)
        return
    
    remove_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bot_admins WHERE user_id = ?", (remove_id,))
    conn.commit()
    conn.close()

    await callback.answer(f"Removed admin ID {remove_id}", show_alert=True)
    await callback.message.edit_text(
        "👥 **Bot Admin Management**\n\nUpdated admin list:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_admins_keyboard()
    )

@dp.callback_query(F.data == "global_start")
async def cb_global_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT business_connection_id, user_name, count, completed FROM business_accounts WHERE is_active = 1 AND messaging_enabled = 1")
    rows = cursor.fetchall()
    conn.close()

    success_count = 0
    for bc_id, user_name, count, completed in rows:
        if not completed and count == 0:
            try:
                update_account_state(bc_id, count=0, waiting=True, completed=False)
                await bot.send_message(
                    chat_id=f"@{TARGET_BOT_USERNAME}",
                    text="Hi",
                    business_connection_id=bc_id
                )
                success_count += 1
            except Exception as e:
                print(f"⚠️ Global start error: {e}")

    await callback.answer(f"🚀 Started campaigns for {success_count} accounts!", show_alert=True)
    await cb_refresh(callback)

@dp.callback_query(F.data == "global_stop")
async def cb_global_stop(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE business_accounts SET messaging_enabled = 0")
    conn.commit()
    conn.close()

    await callback.answer("⏹ Stopped messaging for all accounts!", show_alert=True)
    await cb_refresh(callback)

@dp.callback_query(F.data.startswith("acc_view:"))
async def cb_view_account(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    
    short_id = callback.data.split(":")[1]
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT business_connection_id, user_name, user_id, count, waiting, completed, messaging_enabled 
        FROM business_accounts 
        WHERE business_connection_id LIKE ?
    """, (f"{short_id}%",))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("⚠️ Account not found.", show_alert=True)
        return

    full_bc_id, user_name, user_id, count, waiting, completed, messaging_enabled = row
    status = "🟢 Active" if messaging_enabled else "🔴 Paused"
    comp_text = "✅ Completed (50/50)" if completed else f"⏳ In Progress ({count}/50)"

    detail_text = (
        f"👤 **Account:** `{user_name}`\n\n"
        f"• **Telegram User ID:** `{user_id}`\n"
        f"• **Messaging Status:** {status}\n"
        f"• **Campaign Progress:** {comp_text}\n"
        f"• **Waiting for Reply:** `{'Yes' if waiting else 'No'}`\n\n"
        "Choose an action:"
    )
    
    await callback.message.edit_text(
        detail_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_account_detail_keyboard(short_id, bool(messaging_enabled))
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("acc_toggle:"))
async def cb_toggle_account(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    
    short_id = callback.data.split(":")[1]
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT messaging_enabled FROM business_accounts WHERE business_connection_id LIKE ?", (f"{short_id}%",))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await callback.answer("Account not found.", show_alert=True)
        return
    
    new_state = 0 if row[0] else 1
    cursor.execute("UPDATE business_accounts SET messaging_enabled = ? WHERE business_connection_id LIKE ?", (new_state, f"{short_id}%"))
    conn.commit()
    conn.close()

    await callback.answer("Status updated!")
    callback.data = f"acc_view:{short_id}"
    await cb_view_account(callback)

@dp.callback_query(F.data.startswith("acc_start_single:"))
async def cb_start_single(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    
    short_id = callback.data.split(":")[1]
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT business_connection_id, user_name FROM business_accounts WHERE business_connection_id LIKE ?", (f"{short_id}%",))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("Account not found.", show_alert=True)
        return

    full_bc_id, user_name = row
    try:
        update_account_state(full_bc_id, count=0, waiting=True, completed=False)
        await bot.send_message(
            chat_id=f"@{TARGET_BOT_USERNAME}",
            text="Hi",
            business_connection_id=full_bc_id
        )
        await callback.answer(f"🚀 Started conversation for {user_name}!", show_alert=True)
    except Exception as e:
        await callback.answer(f"⚠️ Error: {e}", show_alert=True)

    callback.data = f"acc_view:{short_id}"
    await cb_view_account(callback)

@dp.callback_query(F.data.startswith("acc_reset:"))
async def cb_reset_account(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Unauthorized", show_alert=True)
        return
    
    short_id = callback.data.split(":")[1]
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE business_accounts 
        SET count = 0, waiting = 1, completed = 0 
        WHERE business_connection_id LIKE ?
    """, (f"{short_id}%",))
    conn.commit()
    conn.close()

    await callback.answer("🔄 Counter reset to 0/50!")
    callback.data = f"acc_view:{short_id}"
    await cb_view_account(callback)

# --- MAIN RUNNER ---
async def main():
    init_db()
    print("🤖 Aiogram Business Automation Admin Dashboard starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
