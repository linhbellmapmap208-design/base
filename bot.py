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
import threading
import http.server
from pathlib import Path
import librosa
import mido


# ==================== HEALTH CHECK (chi can cho Cloudflare Containers) ====================
# Cloudflare Container yeu cau container phai lang nghe 1 cong HTTP thi moi coi la "song".
# Server nay khong lien quan gi den logic bot, chi tra ve "OK" de Cloudflare biet container van chay.
def _start_health_server(port=8080):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            pass  # im lang, khong spam log
    server = http.server.HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# ==================== CONFIG ====================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
ALLOWED_GUILD_ID = os.environ.get("GUILD_ID", "").strip()
ALLOWED_CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
SHOWCASE_CHANNEL_ID = os.environ.get("SHOWCASE_CHANNEL_ID", "").strip()  # tùy chọn
MAX_FILE_SIZE_MB = 25
MAX_DURATION_SECONDS = 540
SHEET_DEFAULT_OCTAVE = 0         # 0 = khớp đàn Roblox khi "Chuyển đổi" = 0

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
job_queue = []          # (interaction, source_type, source_data, mode, octave, queue_msg)
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

async def get_audio_info(url):
    import yt_dlp
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
        return await loop.run_in_executor(None, ydl.extract_info, url, False) or {}

async def download_audio_url(url, tmpdir):
    import yt_dlp
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL({
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(tmpdir, 'download.%(ext)s'),
        'quiet': True, 'no_warnings': True, 'cookiefile': None,
    }) as ydl:
        await loop.run_in_executor(None, ydl.download, [url])
    for f in os.listdir(tmpdir):
        if f.startswith('download.'): return os.path.join(tmpdir, f)
    raise FileNotFoundError("Download failed")


# ==================== ROBLOX / VIRTUAL PIANO SHEET ====================
# Đàn 61 phím trong Roblox (Chuyển đổi = 0): C2 (MIDI 36) = '1' ... C7 (MIDI 96) = 'm'
_VP_KEYS = {
    36: '1', 37: '!', 38: '2', 39: '@', 40: '3', 41: '4', 42: '$', 43: '5', 44: '%', 45: '6', 46: '^', 47: '7',
    48: '8', 49: '*', 50: '9', 51: '(', 52: '0', 53: 'q', 54: 'Q', 55: 'w', 56: 'W', 57: 'e', 58: 'E', 59: 'r',
    60: 't', 61: 'T', 62: 'y', 63: 'Y', 64: 'u', 65: 'i', 66: 'I', 67: 'o', 68: 'O', 69: 'p', 70: 'P', 71: 'a',
    72: 's', 73: 'S', 74: 'd', 75: 'D', 76: 'f', 77: 'g', 78: 'G', 79: 'h', 80: 'H', 81: 'j', 82: 'J', 83: 'k',
    84: 'l', 85: 'L', 86: 'z', 87: 'Z', 88: 'x', 89: 'c', 90: 'C', 91: 'v', 92: 'V', 93: 'b', 94: 'B', 95: 'n',
    96: 'm',
}
VP_MIN, VP_MAX = 36, 96

def _fold_to_range(note):
    while note < VP_MIN: note += 12
    while note > VP_MAX: note -= 12
    return note

def midi_to_vp_sheet(midi_path, octave=SHEET_DEFAULT_OCTAVE, chord_window=0.06, max_tokens_per_line=24):
    mid = mido.MidiFile(midi_path)
    tpb = mid.ticks_per_beat or 480
    tempo = 500000
    t = 0.0
    events = []
    for msg in mido.merge_tracks(mid.tracks):
        t += mido.tick2second(msg.time, tpb, tempo)
        if msg.type == 'set_tempo':
            tempo = msg.tempo
        elif msg.type == 'note_on' and msg.velocity > 0:
            events.append((t, msg.note))
    if not events:
        return "", 0, 0, 0

    shift = octave * 12
    events.sort(key=lambda e: e[0])

    buckets, folded = [], 0
    for t, n in events:
        n += shift
        if not (VP_MIN <= n <= VP_MAX):
            folded += 1
            n = _fold_to_range(n)
        if buckets and t - buckets[-1][0] <= chord_window:
            buckets[-1][1].add(n)
        else:
            buckets.append([t, {n}])

    gaps = [buckets[i + 1][0] - buckets[i][0] for i in range(len(buckets) - 1)
            if buckets[i + 1][0] - buckets[i][0] > 0]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.25
    median_gap = max(median_gap, 0.05)

    lines, line, chord_count = [], [], 0
    for i, (t, notes) in enumerate(buckets):
        if i > 0:
            units = (t - buckets[i - 1][0]) / median_gap
            if units >= 4:
                if line: lines.append(" ".join(line)); line = []
            else:
                rests = min(int(round(units)) - 1, 3)
                if rests > 0: line.extend(["-"] * rests)
        keys = [_VP_KEYS[n] for n in sorted(notes)]
        if len(keys) > 1:
            line.append("[" + "".join(keys) + "]")
            chord_count += 1
        else:
            line.append(keys[0])
        if len(line) >= max_tokens_per_line:
            lines.append(" ".join(line)); line = []
    if line: lines.append(" ".join(line))

    return "\n".join(lines), len(events), chord_count, folded


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
                "File này đã hết hạn trong cache, hãy `/transcribe` lại nhé.", ephemeral=True)
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

        tip = "Drop it into any MIDI player or virtual piano. `/sheet` for QWERTY letters."
        if SHOWCASE_CHANNEL_ID:
            tip += f" Covers go in <#{SHOWCASE_CHANNEL_ID}>."
        c.add_item(ui.TextDisplay(f"-# {tip}"))
        if e['requester']:
            c.add_item(ui.TextDisplay(e['requester']))
        self.add_item(c)


# ==================== COMPONENTS V2: SHEET RESULT ====================
class SheetResultView(ui.LayoutView):
    """Khung kết quả /sheet: tiêu đề, thông số, file .txt nằm trong khung, hướng dẫn, mention."""
    def __init__(self, display_name, filename, notes, chords, bpm, duration, octave, folded, requester):
        super().__init__(timeout=None)
        c = ui.Container()
        c.add_item(ui.TextDisplay(f"## {display_name}"))
        c.add_item(ui.TextDisplay(
            f"`{notes} notes` `{chords} chords` `{bpm:.1f} BPM` `{format_duration(duration)}` "
            f"`Chuyển đổi 0` `Octave {octave:+d}`"))
        c.add_item(ui.Separator())
        c.add_item(ui.File(f"attachment://{filename}"))

        tip = ("`[abc]` bấm cùng lúc • `-` nghỉ • Chữ HOA và `!@$%^*(` là phím đen (Shift). "
               "Đặt **Chuyển đổi = 0** trong game.")
        if folded:
            tip += f" `{folded}` nốt ngoài 61 phím đã gập về quãng 8 gần nhất."
        c.add_item(ui.TextDisplay(f"-# {tip}"))
        if requester:
            c.add_item(ui.TextDisplay(requester))
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
        pm = await _show(interaction, pm, build_progress_embed("SoundCloud", 5, "fetching track info..."))
        try:
            info = await get_audio_info(url)
            display_name = info.get('title') or truncate_url(url)
        except: display_name = truncate_url(url)
        try: await pm.edit(embed=build_progress_embed(display_name, 10, "downloading"))
        except: pass
        try: input_path = await download_audio_url(url, tmpdir)
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


async def process_transcribe_job(interaction, source_type, source_data, mode="midi",
                                 octave=SHEET_DEFAULT_OCTAVE, queue_msg=None):
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

            # ---------- /sheet (Components V2) ----------
            if mode == "sheet":
                sheet_text, note_total, chord_total, folded = midi_to_vp_sheet(result['midi_path'], octave=octave)
                if not sheet_text:
                    try: await progress_msg.edit(embed=build_error_embed("Error", "Could not generate sheet. No piano detected."))
                    except: pass
                    return
                fn = f"{safe_filename(base)}_sheet.txt"
                txt_path = os.path.join(tmpdir, fn)
                with open(txt_path, "w", encoding="utf-8") as fh:
                    fh.write(sheet_text)
                view = SheetResultView(display_name, fn, note_total, chord_total, bpm, duration,
                                       octave, folded, requester)
                await _send_v2(interaction, progress_msg, view, txt_path, fn)
                return

            # ---------- /transcribe (Components V2) ----------
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
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="MP3 > MIDI"))


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
    if url and not url.startswith(("http://","https://")):
        await interaction.response.send_message(embed=build_error_embed("Bad URL", "Must start with http/https"), ephemeral=True)
        return False
    return True


# ==================== QUEUE ====================
async def _refresh_queue_positions():
    for idx, (_, _, _, _, _, qm) in enumerate(job_queue, start=1):
        try: await qm.edit(embed=build_queue_embed(idx))
        except: pass

async def _drain_queue():
    while True:
        async with queue_lock:
            if not job_queue: break
            ni, nt, nd, nk, noct, qm = job_queue.pop(0)
            await _refresh_queue_positions()
        print(f"Queue: {nk}")
        await process_transcribe_job(ni, nt, nd, mode=nk, octave=noct, queue_msg=qm)

async def _enqueue_or_run(interaction, file, url, mode, octave=SHEET_DEFAULT_OCTAVE):
    st, sd = ("file", file) if file else ("url", url)
    await interaction.response.defer(thinking=True)
    async with queue_lock:
        if transcribe_lock.locked():
            p = len(job_queue) + 1
            qm = await interaction.followup.send(embed=build_queue_embed(p))
            job_queue.append((interaction, st, sd, mode, octave, qm))
            return
    await process_transcribe_job(interaction, st, sd, mode=mode, octave=octave)
    await _drain_queue()


# ==================== /transcribe ====================
@bot.tree.command(name="transcribe", description="Convert audio to MIDI")
@app_commands.describe(file="Audio file (MP3, WAV, FLAC, OGG, M4A)", url="SoundCloud or audio URL")
async def transcribe_cmd(interaction: discord.Interaction, file: discord.Attachment = None, url: str = None):
    if not await _check_access(interaction): return
    if not await _validate(interaction, file, url): return
    await _enqueue_or_run(interaction, file, url, "midi")


# ==================== /sheet ====================
@bot.tree.command(name="sheet", description="Convert audio to a Roblox / Virtual Piano QWERTY sheet")
@app_commands.describe(
    file="Audio file (MP3, WAV, FLAC, OGG, M4A)",
    url="SoundCloud or audio URL",
    octave="Dịch quãng 8 (mặc định 0 = khớp đàn Roblox ở Chuyển đổi 0)")
async def sheet_cmd(interaction: discord.Interaction, file: discord.Attachment = None, url: str = None,
                    octave: app_commands.Range[int, -2, 2] = SHEET_DEFAULT_OCTAVE):
    if not await _check_access(interaction): return
    if not await _validate(interaction, file, url): return
    await _enqueue_or_run(interaction, file, url, "sheet", octave=octave)


# ==================== /help (chỉ người gọi lệnh thấy) ====================
@bot.tree.command(name="help", description="Show bot guide")
async def help_cmd(interaction: discord.Interaction):
    desc = (
        "**/transcribe** `file` `url` — Turn a song into a playable piano MIDI.\n"
        "Chuyển bài hát thành file MIDI piano có thể chơi được.\n\n"
        "**/sheet** `file` `url` `octave` — Turn a song into a Roblox / Virtual Piano QWERTY sheet.\n"
        "Chuyển bài hát thành sheet QWERTY khớp đàn Roblox (Chuyển đổi = 0).\n"
        "`octave` từ -2 đến +2 nếu muốn hạ/nâng quãng (mặc định 0).\n"
    )
    e = discord.Embed(title="Commands", description=desc, color=COLOR_MAIN)
    e.add_field(
        name="Options",
        value=f"`file` — MP3, WAV, FLAC, OGG, M4A (tối đa {MAX_FILE_SIZE_MB}MB)\n"
              f"`url`  — SoundCloud hoặc link audio trực tiếp",
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
    _start_health_server(8080)  # bat health check truoc, de Cloudflare Container nhan la "da san sang"
    bot.run(DISCORD_TOKEN)
