import discord
from discord import app_commands, ui
from discord.ext import commands
import os
import re
import glob
import asyncio
import tempfile
import traceback
import time
import sys
import shutil
from pathlib import Path
from urllib.parse import urlsplit
import aiohttp
import librosa
import mido

# ==================== CONFIG ====================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
ALLOWED_GUILD_ID = os.environ.get("GUILD_ID", "").strip()
ALLOWED_CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
SHOWCASE_CHANNEL_ID = os.environ.get("SHOWCASE_CHANNEL_ID", "").strip()  # tùy chọn
TIKHUB_API_KEY = os.environ.get("TIKHUB_API_KEY", "").strip()  # https://user.tikhub.io
MAX_FILE_SIZE_MB = 25
MAX_DURATION_SECONDS = 540

CACHE_DIR = "/tmp/teto_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

COLOR_MAIN = 0x000000
COLOR_WORKING = COLOR_MAIN
COLOR_PROGRESS = COLOR_MAIN
COLOR_COMPLETE = COLOR_MAIN
COLOR_ERROR = 0xff4444
COLOR_QUEUE = COLOR_MAIN


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

transcribe_lock = asyncio.Lock()
queue_lock = asyncio.Lock()
job_queue = []          # (interaction, source_type, source_data, queue_msg)
midi_cache = {}
MAX_CACHE = 30


def cache_midi(message_id, midi_path, display_name, filename, note_count, bpm, duration, sustain, requester):
    if len(midi_cache) >= MAX_CACHE:
        oldest_key = next(iter(midi_cache))
        midi_cache.pop(oldest_key)
        for f in glob.glob(os.path.join(CACHE_DIR, f"{oldest_key}*.mid")):
            try: os.remove(f)
            except Exception: pass
    cached_path = os.path.join(CACHE_DIR, f"{message_id}.mid")
    shutil.copy2(midi_path, cached_path)
    entry = {
        'midi_path': cached_path, 'display_name': display_name, 'filename': filename,
        'note_count': note_count, 'bpm': bpm, 'duration': duration,
        'file_size': os.path.getsize(cached_path), 'sustain': sustain, 'requester': requester,
    }
    midi_cache[message_id] = entry
    return entry


# ==================== UTILS ====================
def get_progress_bar(percent, length=20):
    filled = int(length * percent / 100)
    return f"`[{'█' * filled}{'░' * (length - filled)}] {percent}%`"

def format_duration(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

def estimate_transcribe_time(d):
    if d < 60: return max(d * 1.0, 8.0)
    elif d < 180: return max(d * 0.8, 15.0)
    else: return max(d * 0.6, 25.0)

def count_midi_notes(p):
    try:
        mid = mido.MidiFile(p)
        return sum(1 for t in mid.tracks for m in t if m.type == "note_on" and m.velocity > 0)
    except: return 0

def midi_has_sustain(p):
    try:
        mid = mido.MidiFile(p)
        for track in mid.tracks:
            for msg in track:
                if msg.type == "control_change" and msg.control == 64 and msg.value >= 64:
                    return True
    except Exception:
        pass
    return False

def set_midi_sustain(src, dst, enable):
    mid = mido.MidiFile(src)
    for track in mid.tracks:
        new, carry = [], 0
        for msg in track:
            if msg.type == 'control_change' and msg.control == 64:
                carry += msg.time
                continue
            if carry:
                msg = msg.copy(time=msg.time + carry)
                carry = 0
            new.append(msg)
        track[:] = new
    if enable:
        tr = mid.tracks[0] if mid.tracks else mid.add_track()
        tr.insert(0, mido.Message('control_change', control=64, value=127, time=0))
    mid.save(dst)

def safe_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|#%&]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return (name or "transcribed")[:max_len]

def truncate_url(u, m=50):
    return u if len(u) <= m else "..." + u[-(m-3):]

def format_file_size(s):
    if s < 1024: return f"{s} B"
    elif s < 1048576: return f"{s/1024:.2f} KB"
    else: return f"{s/1048576:.2f} MB"


# ==================== AUDIO ====================
async def run_transkun(input_path, output_path, device="cpu", progress_callback=None):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "transkun.transcribe",
        input_path, output_path, "--device", device,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stderr_lines = []
    while True:
        line = await proc.stderr.readline()
        if not line: break
        decoded = line.decode("utf-8", errors="replace").strip()
        stderr_lines.append(decoded)
        if progress_callback and decoded:
            m = re.search(r'(\d+)\s*%', decoded)
            if m:
                try:
                    p = int(m.group(1))
                    if 0 <= p <= 100: progress_callback(p)
                except: pass
    await proc.wait()
    await proc.stdout.read()
    if proc.returncode != 0:
        raise RuntimeError(f"Transkun failed: {' '.join(stderr_lines[-3:])}")
    return output_path


# ==================== TIKHUB (YouTube / TikTok) ====================
# Resolves a public YouTube/TikTok link to a direct, downloadable media URL
# via the TikHub API (https://tikhub.io — PyPI package "tikhub").
#
# TikHub's official SDK is generated straight from their OpenAPI spec:
# resource attribute = platform surface, method name = last path segment
# (see https://docs.tikhub.io and the Swagger UI at https://api.tikhub.io).
# The exact endpoint used to resolve a share URL can move between TikHub
# spec versions, so each platform below lists a couple of likely
# (resource, method, url_kwarg) candidates and we try them in order — the
# first one that returns a usable media URL wins. If every candidate starts
# failing, check the Swagger UI for the current endpoint name/parameter and
# update this list; nothing else in the file needs to change.
TIKHUB_ENDPOINTS = {
    "tiktok": [
        ("tiktok_web", "fetch_one_video", "url"),
        ("tiktok_app_v3", "fetch_one_video", "url"),
    ],
    "youtube": [
        ("youtube_web", "fetch_one_video", "url"),
        ("youtube_web_v2", "fetch_one_video", "url"),
    ],
}

def detect_tikhub_platform(url):
    host = urlsplit(url).netloc.lower()
    if "tiktok.com" in host:
        return "tiktok"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return None

def _extract_media_url(node, trusted=False):
    """Heuristically find a direct media URL inside a TikHub JSON payload.
    A string is only trusted once we've descended through a key that looks
    like a video/download field, so we don't accidentally grab a cover or
    avatar image URL instead of the actual video."""
    if isinstance(node, str):
        return node if (trusted and node.startswith("http")) else None
    if isinstance(node, dict):
        for key, val in node.items():
            kl = key.lower()
            is_media_key = any(t in kl for t in (
                "download_url", "play_addr", "play_url", "video_url",
                "no_watermark", "nwm", "hd_play", "url_list"))
            found = _extract_media_url(val, trusted=trusted or is_media_key)
            if found: return found
    elif isinstance(node, list):
        for item in node:
            found = _extract_media_url(item, trusted=trusted)
            if found: return found
    return None

def _extract_title(node, depth=0):
    if depth > 6 or node is None: return None
    if isinstance(node, dict):
        for key in ("desc", "title", "video_title", "item_title"):
            v = node.get(key)
            if isinstance(v, str) and v.strip(): return v.strip()
        for v in node.values():
            found = _extract_title(v, depth + 1)
            if found: return found
    elif isinstance(node, list):
        for item in node:
            found = _extract_title(item, depth + 1)
            if found: return found
    return None

async def tikhub_resolve(url):
    """Return (media_url, title) for a YouTube/TikTok link using TikHub."""
    if not TIKHUB_API_KEY:
        raise RuntimeError("TIKHUB_API_KEY chưa được cấu hình trên server.")
    platform = detect_tikhub_platform(url)
    if not platform:
        raise RuntimeError("Chỉ hỗ trợ link YouTube hoặc TikTok.")
    from tikhub import AsyncTikHub
    last_err = None
    async with AsyncTikHub(api_key=TIKHUB_API_KEY) as client:
        for resource_name, method_name, kwarg in TIKHUB_ENDPOINTS[platform]:
            resource = getattr(client, resource_name, None)
            method = getattr(resource, method_name, None) if resource else None
            if method is None:
                continue
            try:
                result = await method(**{kwarg: url})
            except Exception as e:
                last_err = e
                continue
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            media_url = _extract_media_url(payload)
            if media_url:
                return media_url, _extract_title(payload)
    raise RuntimeError(f"TikHub không trả về media cho link này. ({last_err})")

async def download_media_url(media_url, tmpdir):
    async with aiohttp.ClientSession() as session:
        async with session.get(media_url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Tải media thất bại (HTTP {resp.status})")
            ctype = (resp.content_type or "").lower()
            ext = ".m4a" if "audio" in ctype else ".mp4"
            path_ext = os.path.splitext(urlsplit(media_url).path)[1]
            if path_ext and len(path_ext) <= 5:
                ext = path_ext
            out_path = os.path.join(tmpdir, f"download{ext}")
            with open(out_path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    fh.write(chunk)
    return out_path


# ==================== EMBEDS ====================
def build_start_embed(name):
    return discord.Embed(title=name, description=f"{get_progress_bar(0)}\n`working`", color=COLOR_WORKING)

def build_progress_embed(name, pct, status="working"):
    return discord.Embed(title=name, description=f"{get_progress_bar(pct)}\n`{status}`", color=COLOR_PROGRESS)

def build_error_embed(title, desc):
    return discord.Embed(title=title, description=desc, color=COLOR_ERROR)

def build_queue_embed(position):
    return discord.Embed(title="Queue", description=f"Position `{position}` ~{position*10}s", color=COLOR_QUEUE)


# ==================== COMPONENTS V2: MIDI RESULT ====================
class SustainButton(ui.Button):
    def __init__(self, state=False):
        super().__init__(label=f"Sustain: {'on' if state else 'off'}",
                         style=discord.ButtonStyle.secondary,
                         custom_id="midi_sustain_toggle")

    async def callback(self, interaction: discord.Interaction):
        entry = midi_cache.get(interaction.message.id)
        if not entry:
            return await interaction.response.send_message(
                "File này đã hết hạn trong cache, hãy `/transcriber` lại nhé.", ephemeral=True)
        new_state = not entry['sustain']
        old_path = entry['midi_path']
        new_path = os.path.join(CACHE_DIR, f"{interaction.message.id}_{'on' if new_state else 'off'}.mid")
        try:
            set_midi_sustain(old_path, new_path, new_state)
        except Exception as ex:
            return await interaction.response.send_message(f"Lỗi xử lý MIDI: `{ex}`", ephemeral=True)
        entry.update(midi_path=new_path, sustain=new_state, file_size=os.path.getsize(new_path))
        await interaction.response.edit_message(
            view=MidiResultView(entry),
            attachments=[discord.File(new_path, filename=entry['filename'])])
        if old_path != new_path:
            try: os.remove(old_path)
            except Exception: pass


class MidiResultView(ui.LayoutView):
    def __init__(self, entry=None):
        super().__init__(timeout=None)
        e = entry or {'display_name': '-', 'filename': 'x.mid', 'note_count': 0,
                      'bpm': 0.0, 'duration': 0, 'sustain': False, 'requester': None}

        c = ui.Container()
        c.add_item(ui.TextDisplay(f"## {e['display_name']}"))
        c.add_item(ui.TextDisplay(
            f"`{e['note_count']} notes` `{e['bpm']:.1f} BPM` `{format_duration(e['duration'])}`"))
        c.add_item(ui.Separator())
        c.add_item(ui.File(f"attachment://{e['filename']}"))

        row = ui.ActionRow()
        row.add_item(SustainButton(e['sustain']))
        c.add_item(row)

        tip = "Drop it into any MIDI player or virtual piano."
        if SHOWCASE_CHANNEL_ID:
            tip += f" Covers go in <#{SHOWCASE_CHANNEL_ID}>."
        c.add_item(ui.TextDisplay(f"-# {tip}"))
        if e['requester']:
            c.add_item(ui.TextDisplay(e['requester']))
        self.add_item(c)


# ==================== TRANSCRIPTION CORE ====================
async def run_transcription(interaction, input_path, output_midi, display_name,
                            duration, bpm, progress_msg):
    est = estimate_transcribe_time(duration)
    print(f"Duration: {format_duration(duration)} | Est: {est:.0f}s")
    start = time.time()
    last_pct = 45
    real_pct = [None]
    last_edit = [0.0]

    def on_prog(p):
        real_pct[0] = int(45 + (p / 100) * 50)

    task = asyncio.create_task(run_transkun(input_path, output_midi, "cpu", on_prog))
    while not task.done():
        now = time.time()
        elapsed = now - start
        frac = min(elapsed / est, 1.0)
        estimated = int(45 + frac * 50)
        cur = max(estimated, real_pct[0]) if real_pct[0] is not None else estimated
        if cur > last_pct: last_pct = cur
        rem = max(est - elapsed, 0)
        tl = f"{int(rem)//60:02d}:{int(rem)%60:02d}" if rem >= 60 else f"{int(rem)}s"
        if now - last_edit[0] >= 0.8:
            try:
                await progress_msg.edit(embed=build_progress_embed(display_name, last_pct, f"working ~ {tl}"))
                last_edit[0] = now
            except: pass
        if not task.done(): await asyncio.sleep(0.5)

    try: await task
    except Exception as e: raise RuntimeError(f"Transkun failed: {e}")

    try: await progress_msg.edit(embed=build_progress_embed(display_name, 100, "complete"))
    except: pass
    await asyncio.sleep(0.2)

    exists = os.path.exists(output_midi)
    size = os.path.getsize(output_midi) if exists else 0
    notes = count_midi_notes(output_midi) if exists else 0
    if exists and size > 0:
        return {"success": True, "midi_path": output_midi, "note_count": notes, "file_size": size}
    return {"success": False, "error": "Could not generate MIDI. No piano detected."}


# ==================== PREPARE AUDIO ====================
async def _show(interaction, pm, embed):
    if pm is None:
        return await interaction.followup.send(embed=embed)
    try: await pm.edit(embed=embed)
    except: pass
    return pm

async def prepare_audio(interaction, source_type, source_data, tmpdir, pm=None):
    if source_type == "file":
        f = source_data
        display_name = f.filename
        pm = await _show(interaction, pm, build_start_embed(display_name))
        input_path = os.path.join(tmpdir, f.filename)
        await f.save(input_path)
        try: await pm.edit(embed=build_progress_embed(display_name, 10, "downloaded"))
        except: pass
    else:
        url = source_data
        platform = detect_tikhub_platform(url)
        label = "TikTok" if platform == "tiktok" else "YouTube" if platform == "youtube" else "Link"
        pm = await _show(interaction, pm, build_progress_embed(label, 5, "fetching via TikHub..."))
        try:
            media_url, title = await tikhub_resolve(url)
            display_name = title or truncate_url(url)
        except Exception as e:
            await pm.edit(embed=build_error_embed("TikHub Failed", f"```{e}```"))
            return None
        try: await pm.edit(embed=build_progress_embed(display_name, 10, "downloading"))
        except: pass
        try: input_path = await download_media_url(media_url, tmpdir)
        except Exception as e:
            await pm.edit(embed=build_error_embed("Download Failed", f"```{e}```"))
            return None

    loop = asyncio.get_event_loop()
    audio = await loop.run_in_executor(None, lambda: librosa.load(input_path, sr=None, mono=True))
    duration = librosa.get_duration(y=audio[0], sr=audio[1])
    if duration > MAX_DURATION_SECONDS:
        await pm.edit(embed=build_error_embed("Too Long", f"{format_duration(duration)} / 9:00 max"))
        return None
    tempo, _ = await loop.run_in_executor(None, lambda: librosa.beat.beat_track(y=audio[0], sr=audio[1]))
    bpm = float(tempo)
    try: await pm.edit(embed=build_progress_embed(display_name, 25, f"analyzing {format_duration(duration)}"))
    except: pass
    await asyncio.sleep(0.3)
    try: await pm.edit(embed=build_progress_embed(display_name, 40, "loading model"))
    except: pass
    await asyncio.sleep(0.3)
    return input_path, display_name, duration, bpm, pm


# ==================== PROCESS TRANSCRIBE ====================
async def _send_v2(interaction, progress_msg, view, file_path, filename):
    """Biến message tiến trình thành khung Components V2 (fallback: gửi mới rồi xóa cũ). Trả về message cuối."""
    try:
        await progress_msg.edit(content=None, embed=None, view=view,
                                attachments=[discord.File(file_path, filename=filename)])
        return progress_msg
    except Exception as ex:
        print(f"Edit -> V2 failed ({ex}), sending new message")
        new_msg = await interaction.channel.send(view=view, file=discord.File(file_path, filename=filename))
        try: await progress_msg.delete()
        except: pass
        return new_msg


async def process_transcribe_job(interaction, source_type, source_data, queue_msg=None):
    async with transcribe_lock:
        tmpdir_ctx = tempfile.TemporaryDirectory()
        tmpdir = tmpdir_ctx.__enter__()
        progress_msg = queue_msg
        try:
            prep = await prepare_audio(interaction, source_type, source_data, tmpdir, pm=queue_msg)
            if not prep: return
            input_path, display_name, duration, bpm, progress_msg = prep

            output_midi = os.path.join(tmpdir, "output.mid")
            result = await run_transcription(interaction, input_path, output_midi, display_name, duration, bpm, progress_msg)

            if not result["success"]:
                try: await progress_msg.edit(embed=build_error_embed("Error", result.get('error', 'Unknown')))
                except: pass
                return

            base = Path(display_name).stem
            requester = interaction.user.mention if getattr(interaction, "user", None) else None

            fn = f"{safe_filename(base)}.mid"
            sustain = midi_has_sustain(result['midi_path'])
            entry = cache_midi(progress_msg.id, result['midi_path'], display_name, fn,
                               result['note_count'], bpm, duration, sustain, requester)
            view = MidiResultView(entry)
            final_msg = await _send_v2(interaction, progress_msg, view, entry['midi_path'], fn)
            if final_msg.id != progress_msg.id:
                midi_cache[final_msg.id] = midi_cache.pop(progress_msg.id)
        except Exception as e:
            print(f"Error: {traceback.format_exc()}")
            if progress_msg:
                try: await progress_msg.edit(embed=build_error_embed("Error", f"```{e}```"))
                except: pass
        finally:
            tmpdir_ctx.__exit__(None, None, None)


# ==================== EVENTS ====================
@bot.event
async def on_ready():
    print(f"Bot online! {bot.user}")
    try: bot.add_view(MidiResultView())
    except Exception as e: print(f"add_view error: {e}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e: print(f"Sync error: {e}")
    print(f"GUILD='{ALLOWED_GUILD_ID}' CH='{ALLOWED_CHANNEL_ID}'")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="Song > MIDI"))


# ==================== SHARED VALIDATION ====================
async def _check_access(interaction):
    g = str(interaction.guild_id) if interaction.guild_id else "None"
    c = str(interaction.channel_id) if interaction.channel_id else "None"
    if ALLOWED_GUILD_ID and g != ALLOWED_GUILD_ID:
        await interaction.response.send_message(embed=build_error_embed("Denied", "Wrong server."), ephemeral=True)
        return False
    if ALLOWED_CHANNEL_ID and c != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(embed=build_error_embed("Denied", "Wrong channel."), ephemeral=True)
        return False
    return True

async def _validate(interaction, file, url):
    if file is None and url is None:
        await interaction.response.send_message(embed=build_error_embed("Missing", "Provide **file** or **url**."), ephemeral=True)
        return False
    if file and url:
        await interaction.response.send_message(embed=build_error_embed("Too many", "Provide **file** OR **url**."), ephemeral=True)
        return False
    if file:
        if not any(file.filename.lower().endswith(e) for e in (".mp3",".wav",".flac",".ogg",".m4a")):
            await interaction.response.send_message(embed=build_error_embed("Bad format", "MP3 WAV FLAC OGG M4A"), ephemeral=True)
            return False
        if file.size / 1048576 > MAX_FILE_SIZE_MB:
            await interaction.response.send_message(embed=build_error_embed("Too large", f"{file.size/1048576:.1f}MB / {MAX_FILE_SIZE_MB}MB"), ephemeral=True)
            return False
    if url:
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message(embed=build_error_embed("Bad URL", "Must start with http/https"), ephemeral=True)
            return False
        if detect_tikhub_platform(url) is None:
            await interaction.response.send_message(embed=build_error_embed("Bad URL", "Only YouTube or TikTok links are supported."), ephemeral=True)
            return False
    return True


# ==================== QUEUE ====================
async def _refresh_queue_positions():
    for idx, (_, _, _, qm) in enumerate(job_queue, start=1):
        try: await qm.edit(embed=build_queue_embed(idx))
        except: pass

async def _drain_queue():
    while True:
        async with queue_lock:
            if not job_queue: break
            ni, nt, nd, qm = job_queue.pop(0)
            await _refresh_queue_positions()
        await process_transcribe_job(ni, nt, nd, queue_msg=qm)

async def _enqueue_or_run(interaction, file, url):
    st, sd = ("file", file) if file else ("url", url)
    await interaction.response.defer(thinking=True)
    async with queue_lock:
        if transcribe_lock.locked():
            p = len(job_queue) + 1
            qm = await interaction.followup.send(embed=build_queue_embed(p))
            job_queue.append((interaction, st, sd, qm))
            return
    await process_transcribe_job(interaction, st, sd)
    await _drain_queue()


# ==================== /transcriber ====================
@bot.tree.command(name="transcriber", description="Convert audio to MIDI")
@app_commands.describe(file="Audio file (MP3, WAV, FLAC, OGG, M4A)", url="YouTube or TikTok link")
async def transcriber_cmd(interaction: discord.Interaction, file: discord.Attachment = None, url: str = None):
    if not await _check_access(interaction): return
    if not await _validate(interaction, file, url): return
    await _enqueue_or_run(interaction, file, url)


# ==================== /help (chỉ người gọi lệnh thấy) ====================
@bot.tree.command(name="help", description="Show bot guide")
async def help_cmd(interaction: discord.Interaction):
    desc = (
        "**/transcriber** `file` `url` — Turn a song into a playable piano MIDI.\n"
        "Chuyển bài hát thành file MIDI piano có thể chơi được.\n"
    )
    e = discord.Embed(title="Commands", description=desc, color=COLOR_MAIN)
    e.add_field(
        name="Options",
        value=f"`file` — MP3, WAV, FLAC, OGG, M4A (tối đa {MAX_FILE_SIZE_MB}MB)\n"
              f"`url`  — Link YouTube hoặc TikTok",
        inline=False)
    e.add_field(
        name="Limits",
        value=f"Chỉ nhận diện piano | Tối đa {MAX_FILE_SIZE_MB}MB | Tối đa {format_duration(MAX_DURATION_SECONDS)}",
        inline=False)
    if ALLOWED_CHANNEL_ID:
        e.add_field(name="\u200b", value=f"Chạy các lệnh trên trong <#{ALLOWED_CHANNEL_ID}>.", inline=False)
    e.set_footer(text="Teto Transcriber")
    await interaction.response.send_message(embed=e, ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("DISCORD_TOKEN missing!")
        exit(1)
    bot.run(DISCORD_TOKEN)
