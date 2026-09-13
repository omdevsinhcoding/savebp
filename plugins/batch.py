from pyrogram import Client, filters
from pyrogram.types import Message
import asyncio
import re
from helpers.downloader import parse_tg_link, get_user_client, process_and_send_message
from helpers.tasks import generate_task_id, register_task, unregister_task, cancel_task, cancel_user_batch
from config import API_ID, API_HASH

from helpers.cleaner import auto_clean_chat

BATCH_CANCEL_FLAGS = {}

@Client.on_message(filters.command("batch") & filters.private & ~filters.reply)
async def batch_info_handler(client: Client, message: Message):
    await auto_clean_chat(client, message)
    await message.reply_text(
        "📌 **Batch Download Format:**\n\n"
        "Send start link and end link separated by space:\n"
        "Example: `/batch https://t.me/c/123/10 https://t.me/c/123/50`\n\n"
        "Use `/cancel` anytime to stop the batch process!"
    )

@Client.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client: Client, message: Message):
    await auto_clean_chat(client, message)
    user_id = message.from_user.id
    BATCH_CANCEL_FLAGS[user_id] = True
    cancel_user_batch(user_id)
    await message.reply_text("🛑 **Batch process cancellation requested! Processes killed.**")

@Client.on_message(filters.regex(r"^/cancel_([a-zA-Z0-9]{6})$") & filters.private)
async def cancel_specific_task_handler(client: Client, message: Message):
    await auto_clean_chat(client, message)
    task_id = message.matches[0].group(1)
    if cancel_task(task_id):
        await message.reply_text(f"🛑 **Task `{task_id}` Cancelled successfully!**")
    else:
        await message.reply_text(f"⚠️ **Task `{task_id}` not found or already completed.**")

@Client.on_message(filters.command("batch") & filters.private)
async def batch_range_command(client: Client, message: Message):
    await auto_clean_chat(client, message)
    args = message.text.split()
    if len(args) < 3:
        return

    user_id = message.from_user.id
    start_link = args[1]
    end_link = args[2]

    chat_id1, start_id, _, is_priv1, expected_topic1 = parse_tg_link(start_link)
    chat_id2, _, end_id, is_priv2, expected_topic2 = parse_tg_link(end_link)

    if not chat_id1 or not start_id or not end_id:
        return await message.reply_text("❌ **Invalid links or range!**")

    # Auto-fix reversed range
    if start_id > end_id:
        start_id, end_id = end_id, start_id

    expected_topic = expected_topic1 if expected_topic1 else None

    total_posts = (end_id - start_id) + 1
    max_batch = 500
    if total_posts > max_batch:
        return await message.reply_text(f"⚠️ **Batch Limit Exceeded!** Maximum allowed per batch is `{max_batch}` posts.")

    status = await message.reply_text(f"🚀 **Starting Batch Extraction ({total_posts} posts)...**\nUse `/cancel` to stop anytime.")
    BATCH_CANCEL_FLAGS[user_id] = False

    def is_cancelled():
        return BATCH_CANCEL_FLAGS.get(user_id, False)

    user_client = None
    try:
        user_client = await get_user_client(user_id, API_ID, API_HASH)
        if is_priv1 and not user_client:
            return await status.edit_text("🔐 **Private Channel!** Please login using `/login` first.")

        processed_count = 0
        for current_id in range(start_id, end_id + 1):
            if is_cancelled():
                await status.edit_text("🛑 **Batch Process Cancelled!**")
                break

            fetch_client = user_client if user_client else client
            try:
                msg = await fetch_client.get_messages(chat_id1, current_id)
                if getattr(msg, "empty", False) or (not msg.text and not msg.media):
                    continue
                
                # Topic filtering — only skip if we KNOW it's in a different topic
                if expected_topic:
                    msg_thread = getattr(msg, 'message_thread_id', None)
                    if msg_thread is not None and msg_thread != expected_topic:
                        continue

                if msg and not msg.empty:
                    sub_status = await message.reply_text(f"🔄 **Processing Post {current_id}...**")
                    task_id = generate_task_id()
                    
                    async def run_task(m=msg, ss=sub_status, tid=task_id):
                        await process_and_send_message(client, user_id, m, message.chat.id, ss, is_cancelled=is_cancelled, task_id=tid)
                    
                    task = asyncio.create_task(run_task())
                    register_task(task_id, task, user_id)
                    
                    try:
                        await task
                        processed_count += 1
                    except asyncio.CancelledError:
                        await sub_status.edit_text(f"🛑 **File Skipped by User!**")
                        await asyncio.sleep(1)
                    finally:
                        unregister_task(task_id, user_id)
                        try:
                            await sub_status.delete()
                        except:
                            pass
            except Exception as e:
                print(f"[WARN] Batch skip post {current_id} in chat {chat_id1} (topic={expected_topic}): {e}")

        if not is_cancelled():
            if processed_count > 0:
                await status.edit_text(f"✅ **Batch Completed!** Processed {processed_count} posts.")
            else:
                await status.edit_text(
                    f"❌ **No valid posts found in range!**\n\n"
                    f"Make sure the message IDs exist in this topic."
                )

    except Exception as e:
        await status.edit_text(f"❌ **Batch Error:** `{e}`")
