# Install necessary libraries for audio analysis and Gemini AI (handled via requirements.txt in GH Actions)
import os, glob, json, re, time, subprocess
import numpy as np
import librosa
from google import genai
from google.genai import types

# ==============================================================================
# 1. SETUP WORKSPACE (Local directories for GitHub Actions)
# ==============================================================================
base_folder = os.getcwd()
song_folder = os.path.join(base_folder, 'songs')
input_folder = os.path.join(base_folder, 'input')
output_folder = os.path.join(base_folder, 'output')

for folder in [song_folder, input_folder, output_folder]:
    os.makedirs(folder, exist_ok=True)
print("✅ Local directories verified.")

# ==============================================================================
# 2. CONFIGURATION  (all the tuning knobs live here)
# ==============================================================================
INPUT_VIDEO_NAME = "my_video.mp4"
OUTPUT_SHORT_NAME = "ai_transcribed_shorts.mp4"
MODEL_NAME = "gemini-2.5-flash"      # Updated to standard available flash model
FALLBACK_MODEL_NAME = "gemini-2.5-flash"

# --- Scene selection ---
MIN_SCORE = 7          # Gemini rates each scene 1-10; keep only >= this
MAX_SCENES = 8         # hard cap, best-scoring scenes win
MIN_SCENE_SEC = 4      # shortest allowed funny scene
MAX_SCENE_SEC = 20     # longest allowed funny scene

# --- Music / slow-motion "breather" after each scene ---
SLOW_FACTOR = 0.7      # 0.5 = half speed. The tail plays slowed + effects + music
MIN_TAIL_SRC = 1.0     # needs at least this many SOURCE seconds of free gap, else no music
MAX_TAIL_SRC = 4.0     # never consume more than this much source video for the tail
GAP_SAFETY = 0.4       # seconds kept clear before the next talk begins
LAUGH_SHIFT_BACK = 5.0 # music section starts this many seconds earlier, so it covers the laugh after the punchline

SONG_START_OFFSETS = {          # keyword in filename -> where the lyrics start
    "california love": "00:00:07",
    "cheema": "00:00:07",
}

input_path = os.path.join(input_folder, INPUT_VIDEO_NAME)
output_path = os.path.join(output_folder, OUTPUT_SHORT_NAME)
temp_audio_path = os.path.join(base_folder, "extracted_audio.wav")

api_key = os.environ.get('GOOGLE_API_KEY')
if api_key:
    api_key = api_key.replace('\r', '').replace('\n', '').strip().split()[0]
    client = genai.Client(api_key=api_key)
    print("🤖 Gemini API Client initialized successfully.")
else:
    print("⚠️ GOOGLE_API_KEY missing in environment variables.")
    client = None

# ==============================================================================
# 3. SMALL HELPERS
# ==============================================================================
def run(cmd, label=""):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(f"❌ ffmpeg failed {label}:\n{r.stderr[-800:]}")
    return r.returncode == 0

def to_seconds(v):
    """Accepts 12.5, '12.5', '01:05', '00:01:05' -> float seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec

def media_duration(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=nw=1:nk=1', path],
        stdout=subprocess.PIPE, text=True)
    return float(out.stdout.strip())

# ==============================================================================
# 4. AUDIO ENERGY MAP  (used to snap AI timestamps to real pauses)
# ==============================================================================
def build_energy_map(audio_file):
    y, sr = librosa.load(audio_file, sr=16000, mono=True)
    hop = 160  # 10 ms
    rms = librosa.feature.rms(y=y, frame_length=800, hop_length=hop)[0]
    rms = np.convolve(rms, np.ones(20) / 20, mode='same')   # ~200 ms smoothing
    rms = rms / (rms.max() + 1e-9)
    times = np.arange(len(rms)) * hop / sr
    return times, rms

def snap_to_pause(times, rms, t, back, fwd):
    """Move t to the quietest nearby moment (a natural gap between talks)."""
    lo, hi = max(0.0, t - back), t + fwd
    mask = (times >= lo) & (times <= hi)
    if not mask.any():
        return t
    seg_t, seg_r = times[mask], rms[mask]
    span = max(back, fwd, 1e-6)
    cost = seg_r + 0.15 * np.abs(seg_t - t) / span    # quiet AND close
    return float(seg_t[int(np.argmin(cost))])

# ==============================================================================
# 5. TRANSITION MUSIC
# ==============================================================================
def pick_song(song_dir):
    songs = sorted(glob.glob(os.path.join(song_dir, "*.mp3")) + glob.glob(os.path.join(song_dir, "*.wav")))
    if not songs:
        print("⚠️ No songs found. Slow-motion tails will have no music.")
        return None, 0.0
    song = songs[0]
    name = os.path.basename(song).lower()
    offset_sec = 0.0
    for key, off in SONG_START_OFFSETS.items():
        if key in name:
            offset_sec = to_seconds(off)
            print(f"🎵 Lyric alignment applied ({key} -> {off} / {offset_sec}s).")
            break
    return song, offset_sec

# ==============================================================================
# 6. GEMINI: FIND ONLY THE BEST SCENES, WITH START *AND* END
# ==============================================================================
FALLBACK = [{"start": 10.0, "end": 22.0, "score": 5}, {"start": 45.0, "end": 57.0, "score": 5}]

def detect_scenes_with_ai(audio_file):
    if not client:
        return FALLBACK
    try:
        print("☁️ Uploading audio to Gemini...")
        audio_upload = client.files.upload(file=audio_file)
    except Exception as e:
        print(f"⚠️ Upload failed: {e}")
        return FALLBACK

    prompt = f"""You are a senior comedy editor cutting a viral Short from a long show.
Listen to the whole audio. Find ONLY the genuinely funniest self-contained scenes.
Most of the conversation is NOT a highlight - skip ordinary talk, small talk and mild jokes.

For each scene:
- "start": seconds where the setup/conversation begins (about 1 sec before the first useful line, at a natural sentence start).
- "end": seconds where the punchline AND the laughter/reaction have finished, right before the next topic starts.
- "score": 1-10 how funny and clip-worthy it is (10 = best). Be strict: most scenes should score below 7.
- "laugh_pause": seconds of silence/laughter right after the punchline before the next talk starts (0 if none).
- A scene can be {MIN_SCENE_SEC} to {MAX_SCENE_SEC} seconds. Short punchy exchanges stay short, long stories stay long. Do not pad or force a fixed length.
- Scenes must not overlap. Use plain seconds (e.g. 135.5), not clock format.

Return JSON only: {{"scenes": [{{"start": 135.5, "end": 151.0, "score": 9, "laugh_pause": 2.5}}]}} """

    models_to_try = [MODEL_NAME, FALLBACK_MODEL_NAME]
    for model in models_to_try:
        for attempt in range(1, 3):
            try: 
                print(f"🧠 Asking model '{model}' for scenes (attempt {attempt}/2)...")
                resp = client.models.generate_content(
                    model=model,
                    contents=[audio_upload, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                text = resp.text.strip()
                m = re.search(r"\{.*\}|\[.*\]", text, re.S)
                data = json.loads(m.group(0) if m else text)
                scenes = data["scenes"] if isinstance(data, dict) else data
                clean = []
                for s in scenes:
                    clean.append({
                        "start": to_seconds(s.get("start", s.get("start_time"))),
                        "end": to_seconds(s["end"]),
                        "score": float(s.get("score", 7)),
                    })
                print(f"🔥 {model} successfully proposed {len(clean)} scenes.")
                return clean
            except Exception as e:
                print(f"⚠️ {model} (Attempt {attempt}) failed: {e}")
                time.sleep(3)
        print(f"⏭️ {model} failed. Trying next model...")

    print("❌ All Gemini models failed, using fallback timestamps.")
    return FALLBACK

# ==============================================================================
# 7. REFINE: SNAP TO PAUSES, FIX LENGTHS, REMOVE OVERLAPS, PLAN MUSIC GAPS
# ==============================================================================
def refine_scenes(raw, times, rms, total_dur):
    scenes = [s for s in raw if s["score"] >= MIN_SCORE and s["end"] > s["start"]]
    if not scenes:  # nothing passed the bar: take the best few instead of nothing
        scenes = sorted(raw, key=lambda s: -s["score"])[:3]

    out = []
    for s in sorted(scenes, key=lambda s: s["start"]):
        start = snap_to_pause(times, rms, s["start"], back=1.5, fwd=1.0)
        end = snap_to_pause(times, rms, s["end"], back=0.5, fwd=2.5)
        start = max(0.0, start)
        end = min(total_dur, end)
        if end - start < MIN_SCENE_SEC:
            end = min(total_dur, start + MIN_SCENE_SEC)
        if end - start > MAX_SCENE_SEC:
            end = snap_to_pause(times, rms, start + MAX_SCENE_SEC, back=3.0, fwd=0.0)
        scene = {"start": start, "end": end, "score": s["score"]}

        if out and scene["start"] < out[-1]["end"]:
            if scene["score"] > out[-1]["score"]:
                out[-1] = scene
            continue
        out.append(scene)

    out = sorted(sorted(out, key=lambda s: -s["score"])[:MAX_SCENES], key=lambda s: s["start"])

    for i, s in enumerate(out):
        s["end"] = max(s["start"] + MIN_SCENE_SEC, s["end"] - LAUGH_SHIFT_BACK)
        next_start = out[i + 1]["start"] if i + 1 < len(out) else total_dur
        gap = next_start - s["end"] - GAP_SAFETY
        tail = max(0.0, min(gap, MAX_TAIL_SRC))
        s["tail_src"] = tail if tail >= MIN_TAIL_SRC else 0.0
        s["tail_out"] = s["tail_src"] / SLOW_FACTOR
    return out

# ==============================================================================
# 8. RENDERING
# ==============================================================================
V_ENC = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', '30']
A_ENC = ['-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '192k']

def render_scene(start, dur, out_file):
    return run(['ffmpeg', '-y', '-ss', f"{start:.3f}", '-t', f"{dur:.3f}", '-i', input_path,
                '-vf', 'crop=ih:ih,scale=1080:1080,fps=30', *V_ENC, *A_ENC, out_file], "(scene)")

def render_tail(src_start, src_dur, out_dur, song, song_current_pos, out_file):
    vf = (f"crop=ih:ih,scale=1080:1080,setpts=PTS/{SLOW_FACTOR},"
          f"hue=h=200:s=1.5,"
          f"crop=w=iw-40:h=ih-40:x='20+15*sin(2*PI*t*4)':y='20+15*cos(2*PI*t*5)',"
          f"scale=1080:1080,fps=30")
    if song:
        fade_st = max(0.0, out_dur - 0.6)
        fc = (f"[0:v]{vf}[v];"
              f"[0:a]atempo={SLOW_FACTOR},volume=0.25[a0];"
              f"[1:a]afade=t=in:d=0.2,afade=t=out:st={fade_st:.3f}:d=0.6[m];"
              f"[a0][m]amix=inputs=2:duration=longest:dropout_transition=0,volume=1.8[a]")
        cmd = ['ffmpeg', '-y', '-ss', f"{src_start:.3f}", '-t', f"{src_dur:.3f}", '-i', input_path,
               '-ss', f"{song_current_pos:.3f}", '-t', f"{out_dur:.3f}", '-i', song,
               '-filter_complex', fc, '-map', '[v]', '-map', '[a]',
               '-t', f"{out_dur:.3f}", *V_ENC, *A_ENC, out_file]
    else:
        cmd = ['ffmpeg', '-y', '-ss', f"{src_start:.3f}", '-t', f"{src_dur:.3f}", '-i', input_path,
               '-vf', vf, '-af', f"atempo={SLOW_FACTOR}", '-t', f"{out_dur:.3f}",
               *V_ENC, *A_ENC, out_file]
    return run(cmd, "(tail)")

# ==============================================================================
# 9. MAIN
# ==============================================================================
if not os.path.exists(input_path):
    print(f"❌ Could not find '{INPUT_VIDEO_NAME}' in {input_folder}.")
else:
    print(f"🎬 Found source video: {INPUT_VIDEO_NAME}")
    total_dur = media_duration(input_path)

    print("📊 Extracting audio...")
    run(['ffmpeg', '-y', '-i', input_path, '-vn', '-acodec', 'pcm_s16le',
         '-ar', '16000', '-ac', '1', temp_audio_path], "(audio extract)")

    times, rms = build_energy_map(temp_audio_path)
    raw_scenes = detect_scenes_with_ai(temp_audio_path)
    scenes = refine_scenes(raw_scenes, times, rms, total_dur)
    song, song_current_pos = pick_song(song_folder)

    print(f"\n📋 Final plan: {len(scenes)} scenes")
    for i, s in enumerate(scenes, 1):
        print(f"  {i}. {s['start']:.1f}s -> {s['end']:.1f}s ({s['end']-s['start']:.1f}s) "
              f"score {s['score']:.0f} | music tail: {s['tail_out']:.1f}s")

    segments = []
    for i, s in enumerate(scenes):
        scene_file = os.path.join(base_folder, f"seg_{i}_scene.mp4")
        if render_scene(s["start"], s["end"] - s["start"], scene_file):
            segments.append(scene_file)

        if s["tail_src"] > 0:
            tail_file = os.path.join(base_folder, f"seg_{i}_tail.mp4")
            if render_tail(s["end"], s["tail_src"], s["tail_out"], song, song_current_pos, tail_file):
                segments.append(tail_file)
                if song:
                    song_current_pos += s["tail_out"]

    print("🧵 Compiling final video...")
    concat_list = os.path.join(base_folder, "concat_list.txt")
    with open(concat_list, "w") as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")

    ok = run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list,
              '-c', 'copy', output_path], "(concat copy)")
    if not ok: 
        ok = run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list,
                  *V_ENC, *A_ENC, output_path], "(concat re-encode)")

    for seg in segments:
        if os.path.exists(seg): os.remove(seg)
    for p in (temp_audio_path, concat_list):
        if os.path.exists(p): os.remove(p)

    print(f"\n🎉 SUCCESS! Saved to: {output_path}" if ok else "\n❌ Stitching failed.")
