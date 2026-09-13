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

# Session-expired error types that mean the session is permanently dead
SESSION_DEAD_ERRORS = (
    "AuthKeyUnregistered", "AuthKeyDuplicated", "SessionRevoked",
    "SessionExpired", "UserDeactivated", "UserDeactivatedBan"
)

# Error strings in the message that indicate corrupt/broken session data
SESSION_CORRUPT_KEYWORDS = (
    "unpack requires a buffer",
    "unpack_from requires a buffer",
    "session string is invalid",
    "invalid session",
    "not enough values to unpack",
)

def _is_session_dead(error: Exception) -> bool:
    """Check if an error indicates the session is permanently invalid (revoked/expired by Telegram)."""
    error_name = type(error).__name__
    return error_name in SESSION_DEAD_ERRORS

def _is_session_corrupt(error: Exception) -> bool:
    """Check if an error indicates the session string data is corrupt/unparseable."""
    error_name = type(error).__name__
    error_str = str(error).lower()
    # struct.error from corrupt session data
    if error_name == "error" and "unpack" in error_str:
        return True
    if error_name == "struct_error":
        return True
    # Check for known corrupt session keywords
    for keyword in SESSION_CORRUPT_KEYWORDS:
        if keyword in error_str:
            return True
    return False

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
            # If session is permanently dead or corrupt, auto-delete from DB
            if _is_session_dead(e) or _is_session_corrupt(e):
                print(f"[INFO] Auto-clearing dead/corrupt session for {user_id}")
                await delete_session(user_id)
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
        # If session is permanently dead or corrupt, auto-delete from DB
        if _is_session_dead(e) or _is_session_corrupt(e):
            print(f"[INFO] Auto-clearing dead/corrupt session for {user_id}")
            await delete_session(user_id)
        return None

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
        targets.append((custom_chat_id, thread_id))
        if send_pm == "On":
            targets.append((target_chat_id, None))
    else:
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
                        copy_kwargs = {"caption": final_caption}
                        if topic_id:
                            copy_kwargs["message_thread_id"] = topic_id
                        await bot.copy_message(dest_chat, uploaded_msg.chat.id, uploaded_msg.id, **copy_kwargs)
                        continue

                    if source_msg.photo:
                        uploaded_msg = await bot.send_photo(dest_chat, photo=file_path, **kwargs)
                    elif source_msg.video:
                        uploaded_msg = await bot.send_video(dest_chat, video=file_path, **kwargs)
                    elif source_msg.audio:
                        uploaded_msg = await bot.send_audio(dest_chat, audio=file_path, **kwargs)
                    elif source_msg.document:
                        uploaded_msg = await bot.send_document(dest_chat, document=file_path, **kwargs)
                    else:
                        copy_kwargs = {"caption": final_caption}
                        if topic_id:
                            copy_kwargs["message_thread_id"] = topic_id
                        uploaded_msg = await bot.copy_message(dest_chat, source_msg.chat.id, source_msg.id, **copy_kwargs)
                except Exception as e:
                    if "PEER_ID_INVALID" in str(e) and str(dest_chat).startswith("-") and not str(dest_chat).startswith("-100"):
                        new_dest = int(f"-100{str(dest_chat)[1:]}")
                        try:
                            if uploaded_msg:
                                copy_kwargs = {"caption": final_caption}
                                if topic_id:
                                    copy_kwargs["message_thread_id"] = topic_id
                                await bot.copy_message(new_dest, uploaded_msg.chat.id, uploaded_msg.id, **copy_kwargs)
                                continue

                            if source_msg.photo:
                                uploaded_msg = await bot.send_photo(new_dest, photo=file_path, **kwargs)
                            elif source_msg.video:
                                uploaded_msg = await bot.send_video(new_dest, video=file_path, **kwargs)
                            elif source_msg.audio:
                                uploaded_msg = await bot.send_audio(new_dest, audio=file_path, **kwargs)
                            elif source_msg.document:
                                uploaded_msg = await bot.send_document(new_dest, document=file_path, **kwargs)
                            else:
                                copy_kwargs = {"caption": final_caption}
                                if topic_id:
                                    copy_kwargs["message_thread_id"] = topic_id
                                uploaded_msg = await bot.copy_message(new_dest, source_msg.chat.id, source_msg.id, **copy_kwargs)
                        except Exception as e2:
                            print(f"Failed uploading media to fallback ID {new_dest}: {e2}")
                    else:
                        print(f"Failed uploading media to {dest_chat}: {e}")

        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
    else:
        # Text message
        for dest_chat, topic_id in targets:
            try:
                kwargs = {}
                if topic_id:
                    kwargs["message_thread_id"] = topic_id
                await bot.send_message(dest_chat, text=final_caption or source_msg.text, **kwargs)
            except Exception as e:
                if "PEER_ID_INVALID" in str(e) and str(dest_chat).startswith("-") and not str(dest_chat).startswith("-100"):
                    new_dest = int(f"-100{str(dest_chat)[1:]}")
                    try:
                        await bot.send_message(new_dest, text=final_caption or source_msg.text, **kwargs)
                    except Exception as e2:
                        print(f"Failed sending text to fallback ID {new_dest}: {e2}")
                else:
                    print(f"Failed sending text to {dest_chat}: {e}")

