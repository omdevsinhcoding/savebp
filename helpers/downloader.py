import os
import re
import asyncio
from pyrogram import Client
from pyrogram.types import Message
from helpers.progress import ProgressTracker
from database.db import get_session, delete_session, get_thumbnail, get_caption, get_replacements, get_user_settings

def parse_tg_link(link: str):
    """
    Parses Telegram message link.
    Supports:
    - https://t.me/c/1234567890/190/123-125 -> private topic range (-1001234567890, 123, 125, True, 190)
    - https://t.me/c/1234567890/123 -> private chat single (-1001234567890, 123, 123, True, None)
    """
    link = link.strip()
    
    # Private Topic Range
    match = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)-(\d+)", link)
    if match:
        return int(f"-100{match.group(1)}"), int(match.group(3)), int(match.group(4)), True, int(match.group(2))

    # Private Topic Single
    match = re.search(r"t\.me/c/(\d+)/(\d+)/(\d+)", link)
    if match:
        return int(f"-100{match.group(1)}"), int(match.group(3)), int(match.group(3)), True, int(match.group(2))

    # Private Range
    match = re.search(r"t\.me/c/(\d+)/(\d+)-(\d+)", link)
    if match:
        return int(f"-100{match.group(1)}"), int(match.group(2)), int(match.group(3)), True, None

    # Private Single
    match = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if match:
        return int(f"-100{match.group(1)}"), int(match.group(2)), int(match.group(2)), True, None

    # Public Topic Range
    match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)/(\d+)-(\d+)", link)
    if match:
        return match.group(1), int(match.group(3)), int(match.group(4)), False, int(match.group(2))

    # Public Topic Single
    match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)/(\d+)", link)
    if match:
        return match.group(1), int(match.group(3)), int(match.group(3)), False, int(match.group(2))

    # Public Range
    match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)-(\d+)", link)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3)), False, None

    # Public Single
    match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", link)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(2)), False, None

    return None, None, None, False, None

ACTIVE_CLIENTS = {}

# Session-expired error types that mean the session is permanently dead ON TELEGRAM'S SIDE.
# ONLY these should trigger auto-deletion from DB.
# struct/unpack errors are NOT included — those are Pyrogram version mismatches, not real corruption.
SESSION_DEAD_ERRORS = (
    "AuthKeyUnregistered", "AuthKeyDuplicated", "SessionRevoked",
    "SessionExpired", "UserDeactivated", "UserDeactivatedBan"
)

def _is_session_dead(error: Exception) -> bool:
    """Check if an error indicates the session is permanently invalid (revoked/expired by Telegram)."""
    error_name = type(error).__name__
    return error_name in SESSION_DEAD_ERRORS

def _is_version_mismatch(error: Exception) -> bool:
    """Check if error is due to Pyrogram version mismatch (NOT a real session problem)."""
    error_str = str(error).lower()
    return ("unpack" in error_str and "buffer" in error_str) or \
           "not enough values to unpack" in error_str

async def get_user_client(user_id: int, api_id: int, api_hash: str):
    # Try cached client first
    if user_id in ACTIVE_CLIENTS:
        uc = ACTIVE_CLIENTS[user_id]
        try:
            if not uc.is_connected:
                await uc.start()
            # Validate the client is actually working with a lightweight call
            await uc.get_me()
            return uc
        except Exception as e:
            print(f"[WARN] Cached user client for {user_id} is stale/broken: {e}")
            # Remove stale client from cache
            try:
                await uc.stop()
            except Exception:
                pass
            ACTIVE_CLIENTS.pop(user_id, None)
            # ONLY delete from DB if Telegram confirmed the session is dead
            if _is_session_dead(e):
                print(f"[INFO] Auto-clearing expired session for {user_id} (Telegram confirmed)")
                await delete_session(user_id)
                return None
            if _is_version_mismatch(e):
                print(f"[WARN] Pyrogram version mismatch for {user_id} — session NOT deleted from DB")
                return None

    # Create fresh client from saved session
    session_str = await get_session(user_id)
    if not session_str:
        return None
    try:
        user_client = Client(
            f"user_{user_id}",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_str,
            in_memory=True
        )
        await user_client.start()
        # Validate new client works
        await user_client.get_me()
        ACTIVE_CLIENTS[user_id] = user_client
        return user_client
    except Exception as e:
        print(f"[ERROR] Failed to start user client for {user_id}: {e}")
        # ONLY delete from DB if Telegram confirmed the session is dead
        if _is_session_dead(e):
            print(f"[INFO] Auto-clearing expired session for {user_id} (Telegram confirmed)")
            await delete_session(user_id)
        elif _is_version_mismatch(e):
            print(f"[WARN] Pyrogram version mismatch for {user_id} — session NOT deleted from DB")
        return None


async def _resolve_peer_safe(bot: Client, chat_id):
    """Try to resolve a chat peer so the bot knows about it before sending."""
    try:
        await bot.get_chat(chat_id)
        return True
    except Exception as e:
        print(f"[WARN] Could not resolve peer {chat_id}: {e}")
        return False

async def _send_media(bot: Client, dest_chat, source_msg, file_path, kwargs, topic_id, final_caption):
    """Send media with fallback for Pyrogram versions that don't support message_thread_id."""
    send_kwargs = dict(kwargs)  # copy so we don't mutate
    
    async def _try_send(chat, kw):
        if source_msg.photo:
            return await bot.send_photo(chat, photo=file_path, **kw)
        elif source_msg.video:
            return await bot.send_video(chat, video=file_path, **kw)
        elif source_msg.audio:
            return await bot.send_audio(chat, audio=file_path, **kw)
        elif source_msg.document:
            return await bot.send_document(chat, document=file_path, **kw)
        else:
            copy_kw = {"caption": final_caption}
            if "message_thread_id" in kw:
                copy_kw["message_thread_id"] = kw["message_thread_id"]
            return await bot.copy_message(chat, source_msg.chat.id, source_msg.id, **copy_kw)

    try:
        return await _try_send(dest_chat, send_kwargs)
    except TypeError as e:
        # Pyrogram version doesn't support message_thread_id — retry without it
        if "message_thread_id" in str(e) and "message_thread_id" in send_kwargs:
            print(f"[WARN] message_thread_id not supported, retrying without it")
            send_kwargs.pop("message_thread_id", None)
            return await _try_send(dest_chat, send_kwargs)
        raise

async def _copy_media(bot: Client, dest_chat, uploaded_msg, topic_id, final_caption):
    """Copy an already-uploaded message to another destination."""
    copy_kwargs = {"caption": final_caption}
    if topic_id:
        copy_kwargs["message_thread_id"] = topic_id
    try:
        await bot.copy_message(dest_chat, uploaded_msg.chat.id, uploaded_msg.id, **copy_kwargs)
    except TypeError as e:
        if "message_thread_id" in str(e):
            copy_kwargs.pop("message_thread_id", None)
            await bot.copy_message(dest_chat, uploaded_msg.chat.id, uploaded_msg.id, **copy_kwargs)
        else:
            raise

async def process_and_send_message(bot: Client, user_id: int, source_msg: Message, target_chat_id: int, status_msg: Message, is_cancelled=None, task_id=None):
    if getattr(source_msg, "empty", False) or (not source_msg.text and not source_msg.media):
        # Safely skip deleted or service messages
        return

    tracker = ProgressTracker(status_msg, action_text="📥 Downloading Media", user_id=user_id, is_cancelled=is_cancelled, task_id=task_id)
    
    settings = await get_user_settings(user_id)
    custom_caption_template = settings.get("custom_caption")
    replacements = settings.get("replacements", {})
    send_pm = settings.get("send_pm", "On")
    upload_data = settings.get("set_upload_data")

    # Determine destination targets [(chat_id, message_thread_id)]
    targets = []
    custom_chat_id = None
    thread_id = None

    if upload_data and isinstance(upload_data, dict):
        raw_chat = upload_data.get("chat_id")
        if raw_chat:
            try:
                custom_chat_id = int(raw_chat) if str(raw_chat).replace('-', '').isdigit() else raw_chat
                topic = upload_data.get("topic")
                if topic and str(topic).strip() not in ["None", "", "0"]:
                    thread_id = int(topic)
            except Exception as e:
                print(f"Error parsing set_upload_data: {e}")

    if custom_chat_id:
        # Resolve the custom upload chat so the bot knows about it
        resolved = await _resolve_peer_safe(bot, custom_chat_id)
        if resolved:
            targets.append((custom_chat_id, thread_id))
        else:
            print(f"[WARN] Skipping custom upload to {custom_chat_id} — peer not resolved (bot may not be a member)")
        if send_pm == "On":
            targets.append((target_chat_id, None))
    else:
        targets.append((target_chat_id, None))

    # If no targets resolved, at least send to user PM
    if not targets:
        targets.append((target_chat_id, None))

    # Calculate caption
    original_caption = source_msg.caption or source_msg.text or ""
    for old_word, new_word in replacements.items():
        original_caption = original_caption.replace(old_word, new_word)

    final_caption = custom_caption_template.replace("{caption}", original_caption) if custom_caption_template else original_caption

    # Check for media
    if source_msg.media:
        file_path = None
        try:
            os.makedirs("downloads", exist_ok=True)
            file_path = await source_msg.download(
                file_name="downloads/",
                progress=tracker.progress_callback
            )

            # Check download succeeded
            if not file_path or not os.path.exists(file_path):
                print(f"[ERROR] Download failed or file not found for user {user_id}")
                return

            upload_tracker = ProgressTracker(status_msg, action_text="📤 Uploading Media", user_id=user_id, is_cancelled=is_cancelled)
            user_thumb = settings.get("thumbnail_id")
            if user_thumb and not os.path.exists(user_thumb):
                user_thumb = None

            uploaded_msg = None
            for dest_chat, topic_id in targets:
                kwargs = {"caption": final_caption, "progress": upload_tracker.progress_callback}
                if user_thumb:
                    kwargs["thumb"] = user_thumb
                if topic_id:
                    kwargs["message_thread_id"] = topic_id

                try:
                    if uploaded_msg:
                        await _copy_media(bot, dest_chat, uploaded_msg, topic_id, final_caption)
                        continue

                    uploaded_msg = await _send_media(bot, dest_chat, source_msg, file_path, kwargs, topic_id, final_caption)
                except Exception as e:
                    error_str = str(e)
                    # Try with -100 prefix fix for supergroups
                    if "PEER_ID_INVALID" in error_str or "Peer id invalid" in error_str:
                        dest_str = str(dest_chat)
                        if dest_str.startswith("-") and not dest_str.startswith("-100"):
                            new_dest = int(f"-100{dest_str[1:]}")
                        elif not dest_str.startswith("-"):
                            new_dest = int(f"-100{dest_str}")
                        else:
                            print(f"[ERROR] Cannot resolve peer {dest_chat}: {e}")
                            continue
                        
                        try:
                            await _resolve_peer_safe(bot, new_dest)
                            if uploaded_msg:
                                await _copy_media(bot, new_dest, uploaded_msg, topic_id, final_caption)
                            else:
                                uploaded_msg = await _send_media(bot, new_dest, source_msg, file_path, kwargs, topic_id, final_caption)
                        except Exception as e2:
                            print(f"[ERROR] Failed uploading media to fallback {new_dest}: {e2}")
                    else:
                        print(f"[ERROR] Failed uploading media to {dest_chat}: {e}")

        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
    else:
        # Text message
        for dest_chat, topic_id in targets:
            try:
                # Resolve peer for custom upload chats
                if dest_chat != target_chat_id:
                    await _resolve_peer_safe(bot, dest_chat)
                
                kwargs = {}
                if topic_id:
                    kwargs["message_thread_id"] = topic_id
                try:
                    await bot.send_message(dest_chat, text=final_caption or source_msg.text, **kwargs)
                except TypeError as e:
                    if "message_thread_id" in str(e):
                        kwargs.pop("message_thread_id", None)
                        await bot.send_message(dest_chat, text=final_caption or source_msg.text, **kwargs)
                    else:
                        raise
            except Exception as e:
                error_str = str(e)
                if "PEER_ID_INVALID" in error_str or "Peer id invalid" in error_str:
                    dest_str = str(dest_chat)
                    if dest_str.startswith("-") and not dest_str.startswith("-100"):
                        new_dest = int(f"-100{dest_str[1:]}")
                    elif not dest_str.startswith("-"):
                        new_dest = int(f"-100{dest_str}")
                    else:
                        print(f"[ERROR] Cannot resolve text peer {dest_chat}: {e}")
                        continue
                    try:
                        await _resolve_peer_safe(bot, new_dest)
                        send_kwargs = {}
                        if topic_id:
                            send_kwargs["message_thread_id"] = topic_id
                        try:
                            await bot.send_message(new_dest, text=final_caption or source_msg.text, **send_kwargs)
                        except TypeError as te:
                            if "message_thread_id" in str(te):
                                send_kwargs.pop("message_thread_id", None)
                                await bot.send_message(new_dest, text=final_caption or source_msg.text, **send_kwargs)
                            else:
                                raise
                    except Exception as e2:
                        print(f"[ERROR] Failed sending text to fallback {new_dest}: {e2}")
                else:
                    print(f"[ERROR] Failed sending text to {dest_chat}: {e}")


