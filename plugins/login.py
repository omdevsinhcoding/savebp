import asyncio
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import Message
from pyrogram.errors import (
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid,
    AuthKeyUnregistered, AuthKeyDuplicated, UserDeactivated, UserDeactivatedBan, SessionRevoked, SessionExpired
)
from database.db import save_session, get_session, delete_session
from config import API_ID, API_HASH

from helpers.cleaner import auto_clean_chat, protect_message

# Active login sessions in-memory step state
LOGIN_STATES = {}


async def validate_session(user_id: int) -> tuple:
    """
    Actually connects to Telegram with the saved session to verify it works.
    Returns (is_valid: bool, error_msg: str or None, user_info: str or None)
    """
    session_str = await get_session(user_id)
    if not session_str:
        return False, "no_session", None

    temp_client = Client(
        f"validate_{user_id}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        in_memory=True,
        no_updates=True
    )
    try:
        await temp_client.start()
        me = await temp_client.get_me()
        user_info = f"{me.first_name} ({me.phone_number or 'N/A'})"
        await temp_client.stop()
        return True, None, user_info
    except (AuthKeyUnregistered, AuthKeyDuplicated, SessionRevoked, SessionExpired):
        # Session was terminated/revoked from Telegram side
        try:
            await temp_client.stop()
        except Exception:
            pass
        await delete_session(user_id)
        # Also clear from active clients cache
        try:
            from helpers.downloader import ACTIVE_CLIENTS
            if user_id in ACTIVE_CLIENTS:
                try:
                    await ACTIVE_CLIENTS[user_id].stop()
                except Exception:
                    pass
                ACTIVE_CLIENTS.pop(user_id, None)
        except Exception:
            pass
        return False, "expired", None
    except (UserDeactivated, UserDeactivatedBan):
        try:
            await temp_client.stop()
        except Exception:
            pass
        await delete_session(user_id)
        return False, "deactivated", None
    except Exception as e:
        try:
            await temp_client.stop()
        except Exception:
            pass
        error_str = str(e).lower()
        print(f"[WARN] Session validation error for {user_id}: {e}")
        # Check if session data is corrupt (struct unpack errors, etc.)
        is_corrupt = ("unpack" in error_str and "buffer" in error_str) or \
                     "invalid session" in error_str or \
                     "not enough values to unpack" in error_str
        if is_corrupt:
            print(f"[INFO] Auto-clearing corrupt session for {user_id}")
            await delete_session(user_id)
            try:
                from helpers.downloader import ACTIVE_CLIENTS
                ACTIVE_CLIENTS.pop(user_id, None)
            except Exception:
                pass
            return False, "corrupt", None
        return False, f"error: {e}", None


@Client.on_message(filters.command("login") & filters.private)
async def login_handler(client: Client, message: Message):
    protect_message(message.chat.id, message.id)
    await auto_clean_chat(client, message)
    user_id = message.from_user.id
    existing_session = await get_session(user_id)

    if existing_session:
        # Actually validate the session instead of blindly saying "active"
        status_msg = await message.reply_text("🔄 **Checking your session...**")
        is_valid, error, user_info = await validate_session(user_id)

        if is_valid:
            await status_msg.edit_text(
                f"✅ **Session Already Active!**\n\n"
                f"> **Account:** `{user_info}`\n"
                f"> **Status:** Connected & Working\n\n"
                f"You can save restricted content. Use `/logout` first if you want to re-login."
            )
            protect_message(message.chat.id, status_msg.id)
        else:
            # Session is expired/revoked — auto-clear and start fresh login
            if error == "expired":
                await status_msg.edit_text(
                    "❌ **Session Expired / Revoked!**\n\n"
                    "> Your session was terminated from Telegram side.\n"
                    "> This happens when you revoke it manually:\n"
                    "> **Telegram → Settings → Privacy & Security → Active Sessions**\n\n"
                    "Session cleared. Starting fresh login...\n\n"
                    "📱 Please send your phone number in international format:\n"
                    "Example: `+919876543210`"
                )
            elif error == "deactivated":
                await status_msg.edit_text(
                    "❌ **Account Deactivated!**\n\n"
                    "> Your Telegram account has been deactivated or banned.\n"
                    "> Session has been cleared."
                )
                return
            elif error == "corrupt":
                await status_msg.edit_text(
                    "❌ **Session Data Corrupted!**\n\n"
                    "> Your saved session string is corrupted or incompatible.\n"
                    "> Session has been cleared.\n\n"
                    "Starting fresh login...\n\n"
                    "📱 Please send your phone number in international format:\n"
                    "Example: `+919876543210`"
                )
            else:
                await status_msg.edit_text(
                    f"❌ **Session Invalid!**\n\n"
                    f"> Error: `{error}`\n\n"
                    "Session cleared. Starting fresh login...\n\n"
                    "📱 Please send your phone number in international format:\n"
                    "Example: `+919876543210`"
                )
            # Start fresh login flow
            LOGIN_STATES[user_id] = {"step": "PHONE"}
        return

    LOGIN_STATES[user_id] = {"step": "PHONE"}
    await message.reply_text(
        "📱 **Telegram Account Login**\n\n"
        "Please send your phone number registered with Telegram in international format (with country code).\n"
        "Example: `+919876543210`"
    )

@Client.on_message(filters.command("check") & filters.private)
async def check_handler(client: Client, message: Message):
    await auto_clean_chat(client, message)
    user_id = message.from_user.id
    session = await get_session(user_id)

    if not session:
        await message.reply_text("❌ **No Active Session!** Please use `/login` to connect your account.")
        return

    # Actually validate the session by connecting to Telegram
    status_msg = await message.reply_text("🔄 **Verifying session with Telegram...**")
    is_valid, error, user_info = await validate_session(user_id)

    if is_valid:
        check_msg_text = (
            f"✅ **Session Active!**\n\n"
            f"> **Account:** `{user_info}`\n"
            f"> **Status:** Connected & Working\n"
            f"> **You can save restricted content.**"
        )
        await status_msg.edit_text(check_msg_text)
        protect_message(message.chat.id, status_msg.id)
    else:
        if error == "expired":
            await status_msg.edit_text(
                "❌ **Session Expired / Revoked!**\n\n"
                "> Your session was terminated from Telegram side.\n"
                "> This happens when you revoke it manually:\n"
                "> **Telegram → Settings → Privacy & Security → Active Sessions**\n\n"
                "Session cleared. Send `/login` to reconnect."
            )
        elif error == "deactivated":
            await status_msg.edit_text(
                "❌ **Account Deactivated!**\n\n"
                "> Your Telegram account has been deactivated or banned.\n"
                "> Session has been cleared."
            )
        elif error == "corrupt":
            await status_msg.edit_text(
                "❌ **Session Data Corrupted!**\n\n"
                "> Your saved session string is corrupted or incompatible.\n"
                "> Session has been auto-cleared.\n\n"
                "Send `/login` to reconnect."
            )
        else:
            await status_msg.edit_text(
                f"❌ **Session Invalid!**\n\n"
                f"> Error: `{error}`\n\n"
                "Session cleared. Send `/login` to reconnect."
            )

@Client.on_message(filters.command("logout") & filters.private)
async def logout_handler(client: Client, message: Message):
    await auto_clean_chat(client, message)
    user_id = message.from_user.id
    # Also clear from active clients cache
    try:
        from helpers.downloader import ACTIVE_CLIENTS
        if user_id in ACTIVE_CLIENTS:
            try:
                await ACTIVE_CLIENTS[user_id].stop()
            except Exception:
                pass
            ACTIVE_CLIENTS.pop(user_id, None)
    except Exception:
        pass
    await delete_session(user_id)
    if user_id in LOGIN_STATES:
        del LOGIN_STATES[user_id]
    await message.reply_text("🚪 **Logged out successfully!** Your saved session string has been deleted.")

@Client.on_message(filters.text & filters.private & ~filters.command(["login", "logout", "check", "start", "stop", "help", "settings", "batch", "dl", "adl", "cancel", "id", "commands", "referral", "myplan", "premium"]))
async def login_step_listener(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in LOGIN_STATES:
        raise ContinuePropagation

    state = LOGIN_STATES[user_id]
    step = state.get("step")

    if step == "PHONE":
        phone_number = message.text.strip().replace(" ", "")
        temp_client = Client(f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        try:
            await temp_client.connect()
            code_hash = await temp_client.send_code(phone_number)
            LOGIN_STATES[user_id] = {
                "step": "OTP",
                "phone": phone_number,
                "code_hash": code_hash.phone_code_hash,
                "temp_client": temp_client
            }
            await message.reply_text(
                "📩 **OTP Sent!**\n\n"
                "Please enter the OTP code sent to your Telegram app.\n"
                "Format: Enter numbers separated by spaces (e.g. `1 2 3 4 5`) so Telegram doesn't auto-read it."
            )
        except Exception as e:
            await temp_client.disconnect()
            del LOGIN_STATES[user_id]
            await message.reply_text(f"❌ **Login Error:** `{e}`\nPlease start again using `/login`.")

    elif step == "OTP":
        otp_code = message.text.strip().replace(" ", "")
        temp_client = state["temp_client"]
        phone_number = state["phone"]
        code_hash = state["code_hash"]

        try:
            await temp_client.sign_in(phone_number=phone_number, phone_code_hash=code_hash, phone_code=otp_code)
            session_string = await temp_client.export_session_string()
            await save_session(user_id, session_string)
            await temp_client.disconnect()
            del LOGIN_STATES[user_id]
            
            success_text = (
                "✅ **Login Successful!**\n\n"
                "> **Your account is now connected.**\n"
                "> **You can save restricted content.**\n\n"
                "Use `/check` to verify your session anytime."
            )
            success_msg = await message.reply_text(success_text)
            protect_message(message.chat.id, success_msg.id)
        except SessionPasswordNeeded:
            LOGIN_STATES[user_id]["step"] = "2FA"
            await message.reply_text("🔐 **Two-Factor Authentication (2FA) Required!**\nPlease enter your Two-Step Verification Password:")
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            await message.reply_text(f"❌ **Invalid/Expired OTP:** `{e}`. Please try entering again:")
        except Exception as e:
            await temp_client.disconnect()
            del LOGIN_STATES[user_id]
            await message.reply_text(f"❌ **Error:** `{e}`")

    elif step == "2FA":
        password = message.text.strip()
        temp_client = state["temp_client"]
        try:
            await temp_client.check_password(password=password)
            session_string = await temp_client.export_session_string()
            await save_session(user_id, session_string)
            await temp_client.disconnect()
            del LOGIN_STATES[user_id]
            
            success_text = (
                "✅ **Login Successful!**\n\n"
                "> **Your account is now connected.**\n"
                "> **You can save restricted content.**\n\n"
                "Use `/check` to verify your session anytime."
            )
            success_msg = await message.reply_text(success_text)
            protect_message(message.chat.id, success_msg.id)
        except PasswordHashInvalid:
            await message.reply_text("❌ **Incorrect 2FA Password!** Please enter password again:")
        except Exception as e:
            await temp_client.disconnect()
            del LOGIN_STATES[user_id]
            await message.reply_text(f"❌ **Error:** `{e}`")
