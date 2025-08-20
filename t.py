# tts
#!/usr/bin/env python3
"""
Simple consent-first /ans Telegram TTS bot (single file).

Usage:
1. export TELEGRAM_TOKEN="your_token"
2. pip install python-telegram-bot==20.5 TTS
3. python3 tts_ans_bot.py

Flow:
- /start -> instructions
- /consent -> record consent; next voice note uploaded by user is saved as speaker sample
- Send a voice note after /consent (10-30s) -> saved as speaker.wav for that user
- /ans <your text> -> bot synthesizes text into audio in saved speaker style and sends back
"""

import os
import asyncio
import tempfile
import logging
from functools import partial
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Optional heavy imports - require pip install TTS
try:
    from TTS.api import TTS
    TTS_AVAILABLE = True
except Exception as e:
    TTS_AVAILABLE = False
    TTS_ERR = str(e)

# Basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("7876057412:AAFjt0m9tPOzNM1WoI0Bua1S2DeGfz3kxQQ")
if not TELEGRAM_TOKEN:
    logger.error("Set TELEGRAM_TOKEN env var and restart.")
    raise SystemExit("Missing TELEGRAM_TOKEN")

DATA_DIR = Path("user_data")
DATA_DIR.mkdir(exist_ok=True)

def user_dir(uid: int) -> Path:
    p = DATA_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p

# Load a Coqui TTS model (adjust model name if you prefer another)
TTS_MODEL = "tts_models/multilingual/multi-dataset/your_tts"  # change if you know a better model
tts = None
if TTS_AVAILABLE:
    try:
        # If you have GPU, pass gpu=True for faster generation
        tts = TTS(model_name=TTS_MODEL, progress_bar=False, gpu=False)
        logger.info("TTS model loaded.")
    except Exception as e:
        logger.exception("Failed to init TTS model: %s", e)
        tts = None
        TTS_AVAILABLE = False

# Helpers to run blocking work in executor
async def run_in_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Salam — Simple /ans TTS bot.\n\n"
        "Steps:\n"
        "1) Send /consent to confirm you OWN the voice sample or have explicit written consent.\n"
        "2) After /consent, upload a voice note (10-30s) — this will be saved as your speaker sample.\n"
        "3) Use /ans <text> and I'll reply in a similar voice style.\n\n"
        "Important: Do NOT upload someone else's voice without permission. Misuse is prohibited."
    )
    await update.message.reply_text(txt)

async def consent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ud = user_dir(uid)
    (ud / "consent.txt").write_text("consent_given")
    await update.message.reply_text("Consent recorded. Ab ek voice note bhejiye (10-30s) jo main speaker sample ke roop me save karunga.")

async def voice_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the voice note as speaker sample if consent exists and sample not yet present.
       Otherwise tell user how to use /ans."""
    msg = update.message
    uid = update.effective_user.id
    ud = user_dir(uid)
    consent_file = ud / "consent.txt"
    speaker_path = ud / "speaker.wav"

    voice = msg.voice or msg.audio
    if not voice:
        await msg.reply_text("Koi valid voice file nahi mili.")
        return

    # download to temp
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        ogg_path = tmp.name
    await voice.get_file().download_to_drive(ogg_path)

    # convert ogg to wav using ffmpeg
    wav_tmp = ud / f"sample_{voice.file_unique_id}.wav"
    ffmpeg_cmd = f'ffmpeg -y -i "{ogg_path}" -ar 22050 -ac 1 "{wav_tmp}" -loglevel error'
    ret = os.system(ffmpeg_cmd)
    if ret != 0 or not wav_tmp.exists():
        await msg.reply_text("Conversion failed. Ensure ffmpeg is installed on the server.")
        return

    # If consent given and no speaker sample yet -> save
    if consent_file.exists() and not speaker_path.exists():
        os.replace(str(wav_tmp), str(speaker_path))
        await msg.reply_text("Speaker sample saved successfully. Ab /ans <text> chalakar text ko is voice mein paayein.")
        return

    # If sample already exists -> optionally tell user they can replace
    if speaker_path.exists():
        await msg.reply_text("Aapka speaker sample pehle se maujood hai. Agar aap isse replace karna chahte hain, pehle delete kar dijiye (user_data/<id>/speaker.wav) ya mujhe bataiye.")
        # remove wav_tmp
        try:
            wav_tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # If no consent -> ask to run /consent
    await msg.reply_text("Aapne /consent nahin diya. Pehle /consent chalayen, fir voice sample bhejein.")
    try:
        wav_tmp.unlink(missing_ok=True)
    except Exception:
        pass

async def ans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Synthesize provided text using saved speaker sample."""
    msg = update.message
    uid = update.effective_user.id
    ud = user_dir(uid)
    speaker_path = ud / "speaker.wav"

    # get text after command
    if context.args:
        text = " ".join(context.args).strip()
    else:
        await msg.reply_text("Use: /ans <text> — example: /ans Hello, kaise ho?")
        return

    if not text:
        await msg.reply_text("Kuch text dalen jisey aap voice me chahte hain.")
        return

    # Safety check: refuse if user explicitly asks to clone a named third party
    lowered = text.lower()
    if "clone" in lowered or "make it like" in lowered or "impostor" in lowered:
        await msg.reply_text("Main kisi specific aadmi/celebrity ki awaaz bina likhit consent ke clone nahi karunga. Agar yeh aapki khud ki sample se chalana chahte hain, pehle /consent aur voice sample bhejein.")
        return

    if not speaker_path.exists():
        await msg.reply_text("Koi speaker sample nahi mila. Pehle /consent chalayen aur apni voice sample bhejein.")
        return

    if not TTS_AVAILABLE or tts is None:
        await msg.reply_text("TTS engine available nahi hai on server. Install 'TTS' python package and set TTS_MODEL appropriately.")
        return

    await msg.reply_text("Synthesis shuru ho rahi hai — thoda time lag sakta hai (model download/CPU pe depend karega).")

    out_wav = ud / "ans_reply.wav"
    try:
        # Use tts.tts_to_file or tts.tts depending on TTS version
        # Many TTS versions support tts.tts_to_file(text=..., speaker_wav=..., file_path=...)
        def synth():
            try:
                # prefer tts_to_file if present
                if hasattr(tts, "tts_to_file"):
                    tts.tts_to_file(text=text, speaker_wav=str(speaker_path), file_path=str(out_wav))
                else:
                    # older signature: tts.tts(text, speaker_wav=..., file_path=...)
                    tts.tts(text, speaker_wav=str(speaker_path), file_path=str(out_wav))
                return True, ""
            except Exception as e:
                return False, str(e)
        ok, err = await run_in_thread(synth)
        if not ok or not out_wav.exists():
            logger.error("TTS failed: %s", err)
            await msg.reply_text("TTS failed on server. Check model and resources. Error: " + (err or "unknown"))
            return

        # Send the result as audio (Telegram accepts mp3/wav as audio). Use send_voice if you convert to ogg/opus.
        # We'll send as audio for simplicity.
        await msg.reply_audio(audio=open(out_wav, "rb"), title="TTS Reply")
    except Exception as e:
        logger.exception("Synthesis/send error: %s", e)
        await msg.reply_text("Kuch galat ho gaya TTS ke dauran. Dekhkar bataunga.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start - instructions\n/consent - give consent and upload a voice note\n/ans <text> - get text in your saved voice")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("consent", consent))
    app.add_handler(CommandHandler("ans", ans_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # voice handler for receiving speaker sample
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_receiver))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
