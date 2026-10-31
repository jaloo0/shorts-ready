import io
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

st.set_page_config(page_title="AI Shorts Processor", page_icon="🎬", layout="wide")

API = "https://api.github.com"
RELEASE_TAG = "inputs"            # release that stores your uploaded video + songs
ARTIFACT_NAME = "ai-transcribed-shorts"
VIDEO_ASSET = "my_video.mp4"      # the workflow always reads this name


def secret(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default


# ==========================================================
# OPTIONAL PASSWORD GATE (set APP_PASSWORD in Streamlit secrets)
# ==========================================================
app_pw = secret("APP_PASSWORD")
if app_pw and not st.session_state.get("authed"):
    st.title("🔒 AI Shorts Processor")
    pw = st.text_input("Password", type="password")
    if pw:
        if pw == app_pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

st.title("🎬 AI Shorts Processor")
st.caption("Upload from anywhere → run on GitHub Actions → download your Short.")

# ==========================================================
# SIDEBAR CONFIG (auto-filled from Streamlit secrets if set)
# ==========================================================
st.sidebar.header("🔐 GitHub")
token = st.sidebar.text_input(
    "Personal Access Token", value=secret("GITHUB_TOKEN"), type="password",
    help="Fine-grained token with Contents + Actions read/write on your repo.",
)
repo = st.sidebar.text_input("Repository (owner/repo)", value=secret("GITHUB_REPO"))
branch = st.sidebar.text_input("Branch", value=secret("GITHUB_BRANCH", "main"))
workflow = st.sidebar.text_input("Workflow file", value="shorts_processor.yml")

if not (token and repo):
    st.info("👈 Enter your GitHub token and repository in the sidebar to begin.")
    st.stop()

HEADERS = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def gh(method, path, **kw):
    kw.setdefault("timeout", 60)
    return requests.request(method, f"{API}/repos/{repo}{path}", headers=HEADERS, **kw)


# ==========================================================
# RELEASE STORAGE (handles files far bigger than the 100MB repo limit)
# ==========================================================
def get_or_create_release():
    r = gh("GET", f"/releases/tags/{RELEASE_TAG}")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        c = gh("POST", "/releases", json={
            "tag_name": RELEASE_TAG,
            "target_commitish": branch,
            "name": "Shorts inputs",
            "body": "Storage for uploaded source video and songs. Managed by the dashboard.",
            "draft": False,
            "prerelease": True,
        })
        if c.status_code == 201:
            return c.json()
        raise RuntimeError(f"Could not create release: {c.json().get('message', c.text)}")
    raise RuntimeError(f"Could not read releases: {r.json().get('message', r.text)}")


def upload_asset(name, fileobj):
    rel = get_or_create_release()
    for a in rel.get("assets", []):          # replace if same name exists
        if a["name"] == name:
            gh("DELETE", f"/releases/assets/{a['id']}")
    upload_url = rel["upload_url"].split("{")[0]
    fileobj.seek(0)
    r = requests.post(
        upload_url,
        params={"name": name},
        headers={**HEADERS, "Content-Type": "application/octet-stream"},
        data=fileobj,
        timeout=3600,
    )
    if r.status_code == 201:
        return True, "Uploaded."
    return False, r.json().get("message", r.text)


def list_assets():
    rel = get_or_create_release()
    return rel.get("assets", [])


# ==========================================================
# WORKFLOW CONTROL
# ==========================================================
def dispatch(source, url):
    t0 = datetime.now(timezone.utc)
    r = gh("POST", f"/actions/workflows/{workflow}/dispatches", json={
        "ref": branch,
        "inputs": {"video_source": source, "video_url": url or ""},
    })
    return r, t0


def find_new_run(t0, tries=25):
    for _ in range(tries):
        r = gh("GET", f"/actions/workflows/{workflow}/runs",
               params={"event": "workflow_dispatch", "per_page": 5})
        if r.ok:
            for run in r.json().get("workflow_runs", []):
                created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                if created >= t0 - timedelta(seconds=20):
                    return run
        time.sleep(3)
    return None


def latest_run():
    r = gh("GET", f"/actions/workflows/{workflow}/runs", params={"per_page": 1})
    if r.ok and r.json().get("workflow_runs"):
        return r.json()["workflow_runs"][0]
    return None


def step_icon(s):
    if s["status"] == "completed":
        return {"success": "✅", "skipped": "⏭️"}.get(s.get("conclusion"), "❌")
    return {"in_progress": "⏳"}.get(s["status"], "🕒")


def monitor(run_id, box, max_seconds=75 * 60):
    start = time.time()
    run = {}
    while time.time() - start < max_seconds:
        r = gh("GET", f"/actions/runs/{run_id}")
        if r.ok:
            run = r.json()
        lines = []
        jr = gh("GET", f"/actions/runs/{run_id}/jobs")
        if jr.ok and jr.json().get("jobs"):
            for s in jr.json()["jobs"][0].get("steps", []):
                lines.append(f"{step_icon(s)} {s['name']}")
        elapsed = int(time.time() - start)
        box.markdown(
            f"**Run #{run.get('run_number', '?')}** · status: `{run.get('status', '?')}` "
            f"· watching for {elapsed // 60}m {elapsed % 60}s\n\n" + "\n\n".join(lines)
        )
        if run.get("status") == "completed":
            return run
        time.sleep(8)
    return run


def fetch_video(run_id):
    r = gh("GET", f"/actions/runs/{run_id}/artifacts")
    if not r.ok:
        return None
    arts = [a for a in r.json().get("artifacts", [])
            if a["name"] == ARTIFACT_NAME and not a["expired"]]
    if not arts:
        return None
    dl = requests.get(arts[0]["archive_download_url"], headers=HEADERS, timeout=900)
    if dl.status_code != 200:
        return None
    z = zipfile.ZipFile(io.BytesIO(dl.content))
    name = next((n for n in z.namelist() if n.endswith(".mp4")), None)
    return z.read(name) if name else None


def finish(run, box):
    """Handle a completed run: load video or show the failure."""
    if run.get("status") != "completed":
        st.warning("Still running. Click **Watch latest run** to continue watching.")
        return
    if run.get("conclusion") == "success":
        with st.spinner("Fetching your Short..."):
            video = fetch_video(run["id"])
        if video:
            st.session_state["video"] = video
            st.session_state["video_run"] = run["run_number"]
            st.success("🎉 Done! Open the **Download Short** tab.")
            st.balloons()
        else:
            st.error("Run succeeded but no artifact was found (it may have expired).")
    else:
        st.error(f"Run ended with: `{run.get('conclusion')}`. Check the logs: {run.get('html_url')}")


# ==========================================================
# TABS
# ==========================================================
tab1, tab2, tab3 = st.tabs(["📤 Upload", "🚀 Run & Monitor", "⬇️ Download Short"])

# ---------------- UPLOAD ----------------
with tab1:
    st.subheader("Upload your files")
    st.markdown(
        "Files are stored in a GitHub **Release** (up to 2 GB each), not in your repo history. "
        "For files above ~500 MB, use a **link** in the Run tab instead."
    )
    kind = st.radio("What are you uploading?", ["🎥 Source video", "🎵 Background song"], horizontal=True)

    if kind.startswith("🎥"):
        up = st.file_uploader("Choose a video", type=["mp4", "mov", "mkv", "webm", "m4v"], key="up_video")
        asset_name = VIDEO_ASSET
    else:
        up = st.file_uploader("Choose a song", type=["mp3", "wav"], key="up_song")
        asset_name = ("song_" + re.sub(r"[^A-Za-z0-9._-]+", "_", up.name)) if up else ""

    if up is not None:
        st.info(f"{up.name} · {up.size / 1048576:.1f} MB")
        if st.button("⬆️ Upload", type="primary"):
            with st.spinner("Uploading to GitHub..."):
                try:
                    ok, msg = upload_asset(asset_name, up)
                except Exception as e:
                    ok, msg = False, str(e)
            st.success(f"✅ Stored as `{asset_name}`") if ok else st.error(f"❌ {msg}")

    with st.expander("📂 Files currently stored"):
        if st.button("Refresh list"):
            try:
                assets = list_assets()
                if not assets:
                    st.write("Nothing stored yet.")
                for a in assets:
                    st.write(f"`{a['name']}` — {a['size'] / 1048576:.1f} MB")
            except Exception as e:
                st.error(str(e))

# ---------------- RUN ----------------
with tab2:
    st.subheader("Start processing")
    src_choice = st.radio(
        "Where is the video?",
        ["Uploaded file (from Upload tab)", "Link (Google Drive / direct URL)"],
    )
    url = ""
    if src_choice.startswith("Link"):
        url = st.text_input(
            "Video link",
            placeholder="https://drive.google.com/file/d/.../view  or a direct .mp4 link",
            help="Google Drive files must be shared as 'Anyone with the link'.",
        )

    box = st.empty()

    if st.button("▶️ Start processing", type="primary"):
        source = "url" if src_choice.startswith("Link") else "release"
        problem = None
        if source == "url" and not url.strip():
            problem = "Paste a video link first."
        elif source == "release":
            try:
                names = [a["name"] for a in list_assets()]
                if VIDEO_ASSET not in names:
                    problem = "No uploaded video found. Upload one in the Upload tab first."
            except Exception as e:
                problem = str(e)

        if problem:
            st.error(problem)
        else:
            r, t0 = dispatch(source, url.strip())
            if r.status_code != 204:
                st.error(f"Could not start workflow: {r.json().get('message', r.text)}")
            else:
                with st.spinner("Waiting for GitHub to start the run..."):
                    run = find_new_run(t0)
                if not run:
                    st.error("Workflow was triggered but the run was not found. Check the Actions tab on GitHub.")
                else:
                    st.session_state["run_id"] = run["id"]
                    run = monitor(run["id"], box)
                    finish(run, box)

    if st.button("👀 Watch latest run"):
        run = latest_run()
        if not run:
            st.info("No runs found.")
        else:
            run = monitor(run["id"], box)
            finish(run, box)

# ---------------- DOWNLOAD ----------------
with tab3:
    st.subheader("Your Short")

    if "video" in st.session_state:
        st.caption(f"From run #{st.session_state.get('video_run', '?')}")
        st.video(st.session_state["video"])
        st.download_button(
            "💾 Save Short", st.session_state["video"],
            file_name="ai_transcribed_shorts.mp4", mime="video/mp4", type="primary",
        )
    else:
        st.info("No video loaded yet. Finish a run, or load an earlier one below.")

    st.markdown("---")
    st.markdown("**Load an earlier successful run**")
    if st.button("🔍 Find recent runs"):
        r = gh("GET", f"/actions/workflows/{workflow}/runs",
               params={"per_page": 10, "status": "success"})
        st.session_state["recent"] = r.json().get("workflow_runs", []) if r.ok else []

    recent = st.session_state.get("recent", [])
    if recent:
        labels = [f"Run #{x['run_number']} · {x['created_at'].replace('T', ' ').replace('Z', ' UTC')}"
                  for x in recent]
        pick = st.selectbox("Run", range(len(recent)), format_func=lambda i: labels[i])
        if st.button("📥 Load this run"):
            with st.spinner("Downloading..."):
                v = fetch_video(recent[pick]["id"])
            if v:
                st.session_state["video"] = v
                st.session_state["video_run"] = recent[pick]["run_number"]
                st.rerun()
            else:
                st.error("No artifact for that run (expired?).")
