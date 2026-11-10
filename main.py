import asyncio
import os
import random
import sqlite3
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import BusinessConnection, Message

# --- CONFIGURATION & RENDER PERSISTENT PATHS ---
DATA_DIR = os.getenv("RENDER_DISK_PATH", ".")
DB_FILE = os.path.join(DATA_DIR, "business_campaign.db")

BOT_TOKEN = os.getenv("BOT_TOKEN", "your_telegram_bot_token_here")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "your_openrouter_api_key_here")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))  # <--- Put your numeric Telegram User ID here

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
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def get_account_state(conn, business_connection_id: str):
    cursor = conn.cursor()
    cursor.execute("SELECT count, waiting, completed FROM business_accounts WHERE business_connection_id = ?", (business_connection_id,))
    row = cursor.fetchone()
    if row:
        return {"count": row[0], "waiting": bool(row[1]), "completed": bool(row[2])}
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

# --- BUSINESS CONNECTION HANDLER (Chat Automation Linking) ---
@dp.business_connection()
async def on_business_connection(business_connection: BusinessConnection):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    bc_id = business_connection.id
    user_id = business_connection.user.id
    user_name = business_connection.user.first_name
    is_enabled = business_connection.is_enabled

    if is_enabled:
        cursor.execute("""
            INSERT INTO business_accounts (business_connection_id, user_id, user_name, count, waiting, completed, is_active)
            VALUES (?, ?, ?, 0, 0, 0, 1)
            ON CONFLICT(business_connection_id) DO UPDATE SET is_active = 1
        """, (bc_id, user_id, user_name))
        conn.commit()
        print(f"🔗 Business account connected: {user_name} ({user_id}) [ID: {bc_id}]")

        try:
            cursor.execute("SELECT count, completed FROM business_accounts WHERE business_connection_id = ?", (bc_id,))
            row = cursor.fetchone()
            if row and row[0] == 0 and not row[1]:
                update_account_state(bc_id, count=0, waiting=True, completed=False)
                await bot.send_message(
                    chat_id=f"@{TARGET_BOT_USERNAME}",
                    text="Hi",
                    business_connection_id=bc_id
                )
                print(f"🚀 Sent initial 'Hi' to @{TARGET_BOT_USERNAME} for account {user_name}")
        except Exception as e:
            print(f"⚠️ Failed to send initial greeting for connection {bc_id}: {e}")
    else:
        cursor.execute("UPDATE business_accounts SET is_active = 0 WHERE business_connection_id = ?", (bc_id,))
        conn.commit()
        print(f"🔌 Business account disconnected: [ID: {bc_id}]")

    conn.close()

# --- BUSINESS MESSAGE HANDLER (Chat Automation Replies) ---
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

    if not state or not state["waiting"] or state["completed"] or state["count"] >= MAX_QUESTIONS:
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

# --- OWNER SECURITY FILTER & ADMIN COMMANDS ---
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

@dp.message(F.from_user.func(lambda u: not is_owner(u.id)))
async def unauthorized_handler(message: types.Message):
    return

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_owner(message.from_user.id):
        return
    help_text = (
        "🤖 **Business Chat Automation Controller**\n\n"
        "Welcome! Configure your bot via Telegram Settings > Chat Automation.\n"
        "📊 `/stats` - View connected business accounts, active counts, and limits."
    )
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not is_owner(message.from_user.id):
        return

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM business_accounts")
        total_accounts = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM business_accounts WHERE is_active = 1")
        active_accounts = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM business_accounts WHERE completed = 1")
        completed_campaigns = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM business_accounts WHERE waiting = 1")
        waiting_accounts = cursor.fetchone()[0]
        conn.close()

        stats_text = (
            "📊 **Live Business Campaign Statistics**\n\n"
            f"👥 **Linked Business Accounts:** `{total_accounts}`\n"
            f"🟢 **Active Connections:** `{active_accounts}`\n"
            f"⏳ **Active in Chat (Waiting):** `{waiting_accounts}`\n"
            f"✅ **Completed (50/50):** `{completed_campaigns}`\n"
        )
        await message.answer(stats_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await message.answer(f"⚠️ Error reading database stats: `{e}`", parse_mode=ParseMode.MARKDOWN)

# --- MAIN RUNNER ---
async def main():
    init_db()
    print("🤖 Aiogram Business Automation Bot starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
