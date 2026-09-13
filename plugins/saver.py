from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import Message, CallbackQuery
import asyncio
from helpers.downloader import parse_tg_link, get_user_client, process_and_send_message
from helpers.cleaner import auto_clean_chat
from helpers.progress import handle_refresh_callback
from helpers.tasks import generate_task_id, register_task, unregister_task
from config import API_ID, API_HASH

@Client.on_callback_query(filters.regex(r"^refresh_prog_"))
async def refresh_progress_listener(client: Client, query: CallbackQuery):
    await handle_refresh_callback(client, query)

@Client.on_message(filters.text & filters.private & ~filters.command(["start", "stop", "help", "login", "logout", "settings", "batch", "dl", "adl", "cancel", "stats", "broadcast", "id", "commands", "referral", "myplan", "premium"]))
async def single_post_saver(client: Client, message: Message):
    await auto_clean_chat(client, message)
    user_id = message.from_user.id
    link = message.text.strip()

    chat_id, start_id, end_id, is_private, expected_topic = parse_tg_link(link)
    if not chat_id or not start_id:
        raise ContinuePropagation  # Not a valid Telegram link

    status = await message.reply_text("🔎 **Fetching Message...**")

    user_client = None
    try:
        user_client = await get_user_client(user_id, API_ID, API_HASH)
        if is_private and not user_client:
            # Check if session still exists in DB (version mismatch) vs truly deleted (expired)
            from database.db import get_session as _check_session
            has_session = await _check_session(user_id)
            if has_session:
                # Session exists but can't be used by this Pyrogram version
                return await status.edit_text(
                    "⚠️ **Session Cannot Be Used Here!**\n\n"
                    "Your session is saved but this bot instance cannot use it.\n"
                    "Use `/logout` then `/login` to create a fresh session."
                )
            else:
                return await status.edit_text(
                    "🔐 **Private Channel Link Detected!**\n\n"
                    "Please login to your account using `/login` to download content from private channels."
                )

        fetch_client = user_client if user_client else client

        if start_id == end_id:
            # Single Post
            source_msg = None
            fetch_error = None
            try:
                source_msg = await fetch_client.get_messages(chat_id, start_id)
            except Exception as e:
                fetch_error = e
                print(f"[WARN] Primary fetch failed for {chat_id}/{start_id} (topic={expected_topic}): {e}")
                if not is_private and not user_client:
                    user_client = await get_user_client(user_id, API_ID, API_HASH)
                    if user_client:
                        try:
                            source_msg = await user_client.get_messages(chat_id, start_id)
                            fetch_error = None
                        except Exception as e2:
                            fetch_error = e2
                            print(f"[WARN] Fallback fetch also failed for {chat_id}/{start_id}: {e2}")

            if not source_msg or getattr(source_msg, "empty", False) or (not source_msg.text and not source_msg.media):
                err_detail = f"\n\n`Error: {fetch_error}`" if fetch_error else ""
                return await status.edit_text(f"❌ **Could not fetch message!** Make sure link is correct and bot/user has access.{err_detail}")

            from plugins.batch import BATCH_CANCEL_FLAGS
            BATCH_CANCEL_FLAGS[user_id] = False
            def is_cancelled():
                return BATCH_CANCEL_FLAGS.get(user_id, False)

            try:
                task_id = generate_task_id()
                async def run_task():
                    await process_and_send_message(client, user_id, source_msg, message.chat.id, status, is_cancelled=is_cancelled, task_id=task_id)
                task = asyncio.create_task(run_task())
                register_task(task_id, task, user_id)
                await task
                await status.edit_text("✅ **Task Complete!**")
            except asyncio.CancelledError:
                await status.edit_text("🛑 **Process Cancelled!**")
            finally:
                unregister_task(task_id, user_id)
        else:
            # Range Link (e.g. 135 to 137)
            total_posts = (end_id - start_id) + 1
            await status.edit_text(f"🚀 **Starting Range Extraction ({total_posts} posts)...**")
            
            from plugins.batch import BATCH_CANCEL_FLAGS
            BATCH_CANCEL_FLAGS[user_id] = False
            def is_cancelled():
                return BATCH_CANCEL_FLAGS.get(user_id, False)
                
            for current_id in range(start_id, end_id + 1):
                if is_cancelled():
                    await message.reply_text("🛑 **Process Cancelled!**")
                    break
                try:
                    source_msg = await fetch_client.get_messages(chat_id, current_id)
                    if getattr(source_msg, "empty", False) or (not source_msg.text and not source_msg.media):
                        continue
                    
                    if expected_topic and source_msg.message_thread_id and source_msg.message_thread_id != expected_topic:
                        continue

                    if source_msg and not source_msg.empty:
                        sub_status = await message.reply_text(f"🔄 **Processing Post {current_id}...**")
                        task_id = generate_task_id()
                        async def run_task():
                            await process_and_send_message(client, user_id, source_msg, message.chat.id, sub_status, is_cancelled=is_cancelled, task_id=task_id)
                        
                        task = asyncio.create_task(run_task())
                        register_task(task_id, task, user_id)
                        try:
                            await task
                        except asyncio.CancelledError:
                            await sub_status.edit_text("🛑 **File Skipped by User!**")
                            await asyncio.sleep(1)
                        finally:
                            unregister_task(task_id, user_id)
                            try:
                                await sub_status.delete()
                            except:
                                pass
                except Exception as e:
                    print(f"[WARN] Range skip post {current_id} in chat {chat_id} (topic={expected_topic}): {e}")

            if not is_cancelled():
                await status.edit_text("✅ **Batch Completed!**")

    except Exception as e:
        print(f"[ERROR] Saver error for user {user_id}, link={link}: {e}")
        await status.edit_text(f"❌ **Error:** `{e}`")
