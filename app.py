import base64
import io
import zipfile
import requests
import streamlit as st

st.set_page_config(
    page_title="AI Shorts Processor Dashboard", page_icon="🎬", layout="wide"
)

st.title("🎬 AI Shorts Processor - Control Panel")
st.markdown(
    "Manage your video uploads, trigger GitHub Actions, and download your processed AI shorts."
)

# ==========================================
# SIDEBAR: GitHub Credentials & Settings
# ==========================================
st.sidebar.header("🔐 GitHub Configuration")
github_token = st.sidebar.text_input(
    "GitHub Personal Access Token (PAT)",
    type="password",
    help="Needs `repo` or `workflow` permissions.",
)
repo_name = st.sidebar.text_input(
    "Repository (owner/repo)",
    placeholder="e.g., username/repo-name",
    help="Your GitHub username and repository name.",
)
branch_name = st.sidebar.text_input("Branch", value="main")
workflow_filename = st.sidebar.text_input(
    "Workflow Filename", value="shorts_processor.yml"
)

# Helper headers for GitHub API
headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def upload_to_github(file_bytes, remote_path, commit_message):
  """Uploads a file directly to the GitHub repository via Contents API."""
  if not github_token or not repo_name:
    return False, "GitHub Token and Repository Name are required."

  url = f"https://api.github.com/repos/{repo_name}/contents/{remote_path}"

  # Check if file already exists to get its SHA (required for updating)
  response = requests.get(url, headers=headers)
  sha = None
  if response.status_code == 200:
    sha = response.json().get("sha")

  encoded_content = base64.b64encode(file_bytes).decode("utf-8")
  payload = {
      "message": commit_message,
      "content": encoded_content,
      "branch": branch_name,
  }
  if sha:
    payload["sha"] = sha

  put_response = requests.put(url, headers=headers, json=payload)
  if put_response.status_code in [201, 200]:
    return True, "Successfully uploaded to repository!"
  else:
    return False, f"Error: {put_response.json().get('message', 'Unknown error')}"


# ==========================================
# MAIN TABS
# ==========================================
tab1, tab2, tab3 = st.tabs(
    ["📤 Upload Media", "🚀 Run & Monitor", "⬇️ Download Shorts"]
)

# --- TAB 1: UPLOAD MEDIA ---
with tab1:
  st.subheader("Upload Source Video or Background Song")
  st.markdown(
      "> *Note: Standard GitHub repositories have a 100MB file size limit per"
      " file via API.*"
  )

  upload_type = st.radio(
      "Select File Type to Upload",
      ["Source Video (input/my_video.mp4)", "Background Song (songs/)"],
  )

  uploaded_file = st.file_uploader(
      "Choose a file", type=["mp4", "mov", "mp3", "wav"]
  )

  if uploaded_file is not None:
    file_bytes = uploaded_file.read()
    file_size_mb = len(file_bytes) / (1024 * 1024)
    st.info(f"File size: {file_size_mb:.2f} MB")

    if file_size_mb > 95:
      st.warning(
          "⚠️ File is close to or exceeds GitHub's 100MB limit. Consider"
          " compressing your video."
      )

    if st.button("🚀 Upload to Repository"):
      with st.spinner("Uploading file to repository..."):
        if "Source Video" in upload_type:
          remote_path = "input/my_video.mp4"
          msg = "Add source video via Streamlit dashboard"
        else:
          remote_path = f"songs/{uploaded_file.name}"
          msg = f"Add song {uploaded_file.name} via Streamlit dashboard"

        success, message = upload_to_github(file_bytes, remote_path, msg)
        if success:
          st.success(f"✅ {message} (`{remote_path}`)")
        else:
          st.error(f"❌ {message}")

# --- TAB 2: RUN & MONITOR ---
with tab2:
  st.subheader("Trigger GitHub Actions Workflow")

  if st.button("▶️ Start Processing Workflow"):
    if not github_token or not repo_name:
      st.error("Please configure your GitHub Token and Repository in the sidebar.")
    else:
      url = f"https://api.github.com/repos/{repo_name}/actions/workflows/{workflow_filename}/dispatches"
      payload = {"ref": branch_name}
      res = requests.post(url, headers=headers, json=payload)

      if res.status_code == 204:
        st.success(
            "🎉 Workflow triggered successfully! Check status below or on"
            " GitHub."
        )
      else:
        st.error(
            f"❌ Failed to trigger workflow: {res.json().get('message', res.text)}"
        )

  st.markdown("---")
  st.subheader("📊 Recent Workflow Runs")
  if st.button("🔄 Refresh Status"):
    pass

  if github_token and repo_name:
    runs_url = f"https://api.github.com/repos/{repo_name}/actions/runs?per_page=5"
    runs_res = requests.get(runs_url, headers=headers)
    if runs_res.status_code == 200:
      runs = runs_res.json().get("workflow_runs", [])
      for run in runs:
        if run["path"].endswith(workflow_filename):
          status = run["status"]
          conclusion = run["conclusion"] or "in_progress"
          created_at = run["created_at"]
          run_id = run["id"]

          color_icon = (
              "🟢"
              if conclusion == "success"
              else ("🔴" if conclusion == "failure" else "🟡")
          )
          st.markdown(
              f"{color_icon} **Run ID:** `{run_id}` | **Status:** `{status}` |"
              f" **Result:** `{conclusion}` | **Time:** `{created_at}`"
          )
    else:
      st.warning("Could not fetch workflow runs.")

# --- TAB 3: DOWNLOAD OUTPUT ---
with tab3:
  st.subheader("Download Generated Shorts")
  st.markdown(
      "Fetch the output video artifact from the latest successful GitHub Actions"
      " run."
  )

  if st.button("🔍 Check for Available Artifacts"):
    if not github_token or not repo_name:
      st.error("Please configure your GitHub Token and Repository in the sidebar.")
    else:
      artifacts_url = (
          f"https://api.github.com/repos/{repo_name}/actions/artifacts"
      )
      art_res = requests.get(artifacts_url, headers=headers)

      if art_res.status_code == 200:
        artifacts = art_res.json().get("artifacts", [])
        if not artifacts:
          st.info(
              "No artifacts found yet. Make sure your workflow has finished and"
              " uploaded an artifact."
          )
        else:
          latest_art = artifacts[0]  # Most recent artifact
          art_name = latest_art["name"]
          art_size = latest_art["size_in_bytes"] / (1024 * 1024)
          download_url = latest_art["archive_download_url"]
          expired = latest_art["expired"]

          st.success(
              f"Found artifact: **{art_name}** ({art_size:.2f} MB)"
          )

          if expired:
            st.warning("⚠️ This artifact has expired.")
          else:
            # Download the zip archive directly through Streamlit proxy
            if st.button("📥 Download Final Short"):
              with st.spinner("Downloading artifact from GitHub..."):
                dl_res = requests.get(download_url, headers=headers)
                if dl_res.status_code == 200:
                  # GitHub returns a zip file containing the artifact folder/file
                  z = zipfile.ZipFile(io.BytesIO(dl_res.content))
                  for filename in z.namelist():
                    if filename.endswith(".mp4"):
                      video_data = z.read(filename)
                      st.download_button(
                          label="💾 Click here to save `ai_transcribed_shorts.mp4`",
                          data=video_data,
                          file_name="ai_transcribed_shorts.mp4",
                          mime="video/mp4",
                      )
                else:
                  st.error("Failed to download artifact file.")
      else:
        st.error("Could not fetch artifacts from GitHub.")
