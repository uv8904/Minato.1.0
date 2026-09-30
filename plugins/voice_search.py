"""
Voice Search for MinatoVerse - Option C

User sends voice note in PM or group: "Jawan 2023" -> bot transcribes via Groq Whisper -> searches instantly.

Setup:
  GROQ_API_KEY env must be set (you already have it in info.py)
  Model: whisper-large-v3 via Groq OpenAI-compatible API

Flow:
  voice message -> download -> POST to https://api.groq.com/openai/v1/audio/transcriptions
  -> text -> auto_filter

Supports: voice, audio, video_note
"""

import os
import asyncio
import logging
import tempfile
import re
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from info import GROQ_API_KEY, GROQ_MODEL
import requests

logger = logging.getLogger(__name__)

# Supported languages for transcription - auto but we hint
VOICE_LANGUAGES = ["en", "hi"]  # English + Hindi

def is_enabled():
    return bool(GROQ_API_KEY and GROQ_API_KEY.strip() and "regsk" not in GROQ_API_KEY.lower() and len(GROQ_API_KEY) > 20)

async def transcribe_with_groq(file_path: str) -> str:
    """
    Transcribe audio file using Groq Whisper API
    Returns transcribed text or empty string
    """
    if not is_enabled():
        # Try with whatever key is set - might still work
        pass
    
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}"
    }
    
    # Groq supports whisper-large-v3, whisper-large-v3-turbo, distil-whisper-large-v3-en
    model = "whisper-large-v3"
    
    try:
        # Use synchronous requests in thread to avoid blocking
        def _do_request():
            with open(file_path, "rb") as f:
                files = {
                    "file": (os.path.basename(file_path), f, "audio/ogg")
                }
                data = {
                    "model": model,
                    "language": "en",  # auto-detects but hint English; for Hindi movies English transcription works best
                    "response_format": "json",
                    "temperature": "0.0"
                }
                # Try English first
                resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
                if resp.status_code != 200:
                    # Retry without language hint (auto-detect Hindi etc)
                    f.seek(0)
                    data_no_lang = {
                        "model": model,
                        "response_format": "json",
                        "temperature": "0.0"
                    }
                    resp = requests.post(url, headers=headers, files=files, data=data_no_lang, timeout=30)
                return resp
        
        resp = await asyncio.to_thread(_do_request)
        
        if resp.status_code == 200:
            result = resp.json()
            text = result.get("text", "").strip()
            return text
        else:
            logger.warning(f"Groq transcription failed {resp.status_code}: {resp.text[:500]}")
            # Try turbo model as fallback
            def _do_turbo():
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f, "audio/ogg")}
                    data = {"model": "whisper-large-v3-turbo", "response_format": "json"}
                    return requests.post(url, headers=headers, files=files, data=data, timeout=30)
            try:
                resp2 = await asyncio.to_thread(_do_turbo)
                if resp2.status_code == 200:
                    return resp2.json().get("text", "").strip()
            except Exception as e2:
                logger.warning(f"Turbo fallback failed: {e2}")
            return ""
    except Exception as e:
        logger.exception(f"Transcription error: {e}")
        return ""


def clean_transcription(text: str) -> str:
    """Clean up transcription to be a good movie search query"""
    if not text:
        return ""
    # Remove common filler words that Whisper adds
    text = text.strip()
    # Remove punctuation at ends
    text = re.sub(r'^[^\w]+|[^\w]+$', '', text)
    # Remove "please search" etc
    text = re.sub(r'\b(please|search|find|movie|download|send me|i want|movie name)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    # Keep reasonable length
    if len(text) > 60:
        text = text[:60].strip()
    # Must be at least 2 chars
    if len(text) < 2:
        return ""
    return text


@Client.on_message(filters.private & (filters.voice | filters.audio | filters.video_note) & filters.incoming)
async def voice_search_pm(client, message: Message):
    """Voice search in PM - transcribe and search"""
    user_id = message.from_user.id if message.from_user else 0
    logger.info(f"Voice search PM from {user_id}")
    
    # Check if user is banned
    from utils import temp
    if user_id in temp.BANNED_USERS:
        return
    
    # Show typing
    try:
        await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
    except Exception:
        pass
    
    status_msg = None
    try:
        status_msg = await message.reply_text(
            "<b>🎙️ Voice received... transcribing with AI...</b>",
            parse_mode=enums.ParseMode.HTML,
            quote=True
        )
    except Exception:
        pass
    
    file_path = None
    try:
        # Download voice file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            file_path = tmp.name
        
        # Download using pyrogram
        downloaded = await client.download_media(message, file_name=file_path)
        if not downloaded:
            raise ValueError("Download failed")
        
        # Transcribe
        if status_msg:
            try:
                await status_msg.edit_text("<b>🧠 AI transcribing your voice... (Groq Whisper)</b>", parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        
        transcribed = await transcribe_with_groq(file_path)
        
        if not transcribed:
            if status_msg:
                await status_msg.edit_text(
                    "<b>😕 Couldn't understand your voice, please try again.</b>\n\n"
                    "<i>Tip: Say movie name clearly like 'Jawan 2023' or 'Animal Hindi'</i>",
                    parse_mode=enums.ParseMode.HTML
                )
            return
        
        cleaned = clean_transcription(transcribed)
        if not cleaned:
            cleaned = transcribed.strip()[:60]
        
        if status_msg:
            try:
                await status_msg.edit_text(
                    f"<b>🎙️ You said:</b> <code>{transcribed}</code>\n"
                    f"<b>🔍 Searching for:</b> <code>{cleaned}</code>",
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass
        
        # Now trigger auto_filter with the cleaned query
        # Create a fake text message
        message.text = cleaned
        
        # Import auto_filter here to avoid circular
        from plugins.pmfilter import auto_filter
        await auto_filter(client, message)
        
        # Delete status after a bit
        if status_msg:
            try:
                await asyncio.sleep(3)
                await status_msg.delete()
            except Exception:
                pass
                
    except Exception as e:
        logger.exception(f"Voice search PM failed: {e}")
        if status_msg:
            try:
                await status_msg.edit_text(
                    f"<b>❌ Voice search failed:</b> <code>{str(e)[:100]}</code>\n"
                    "Please type the movie name instead.",
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass
    finally:
        # Cleanup temp file
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception:
                pass


@Client.on_message(filters.group & (filters.voice | filters.audio) & filters.incoming, group=2)
async def voice_search_group(client, message: Message):
    """Voice search in groups - same but respects group settings"""
    # Only process if group has auto filter enabled and message is short voice (<30s)
    try:
        # Check duration - ignore long audios
        voice = message.voice or message.audio
        if voice and hasattr(voice, 'duration') and voice.duration and voice.duration > 30:
            return  # Ignore long voice notes in groups
        
        from utils import get_settings
        settings = await get_settings(message.chat.id)
        if not settings.get('auto_ffilter', True):
            return
        
        # Same flow as PM but reply in group
        file_path = None
        status_msg = None
        try:
            await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
        except Exception:
            pass
        
        try:
            status_msg = await message.reply_text(
                f"<b>🎙️ {message.from_user.mention} - transcribing voice...</b>",
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass
        
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
                file_path = tmp.name
            
            downloaded = await client.download_media(message, file_name=file_path)
            if not downloaded:
                raise ValueError("Download failed")
            
            transcribed = await transcribe_with_groq(file_path)
            if not transcribed:
                if status_msg:
                    await status_msg.edit_text("<b>😕 Couldn't understand, type movie name please</b>", parse_mode=enums.ParseMode.HTML)
                return
            
            cleaned = clean_transcription(transcribed) or transcribed[:60]
            
            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"<b>🎙️ {message.from_user.first_name} said:</b> <code>{transcribed}</code>\n"
                        f"<b>🔍 Searching:</b> <code>{cleaned}</code>",
                        parse_mode=enums.ParseMode.HTML
                    )
                except Exception:
                    pass
            
            message.text = cleaned
            from plugins.pmfilter import auto_filter
            await auto_filter(client, message)
            
            if status_msg:
                try:
                    await asyncio.sleep(3)
                    await status_msg.delete()
                except Exception:
                    pass
                    
        except Exception as e:
            logger.exception(f"Voice group failed: {e}")
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"voice_search_group outer failed: {e}")


@Client.on_message(filters.command("voice") & filters.private)
async def voice_help(client, message):
    """Help for voice search"""
    await message.reply_text(
        "<b>🎙️ Voice Search - MinatoVerse</b>\n\n"
        "<b>How to use:</b>\n"
        "1. Send a voice note saying movie name\n"
        "   Example: <code>Jawan 2023</code> or <code>Animal Hindi</code>\n"
        "2. AI will transcribe and search instantly\n\n"
        "<b>Tips:</b>\n"
        "• Say clearly, 2-5 seconds\n"
        "• Include year if possible: <code>Kalki 2024</code>\n"
        "• Works in Hindi too: <code>Stree 2 Hindi</code>\n\n"
        "<b>Tech:</b> Groq Whisper-large-v3 (superfast)\n"
        f"<b>Status:</b> {'✅ Enabled' if is_enabled() else '⚠️ Check GROQ_API_KEY'}\n\n"
        "Just send a voice note now to try!",
        parse_mode=enums.ParseMode.HTML
    )
