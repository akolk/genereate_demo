#!/usr/bin/env python3
"""
generate_demo.py (multi‑repo + Renovate guard + auto‑create defaults)
--------------------------------------------------------------------
* Reads a comma‑separated list of repos from TARGET_REPOS.
* For each repo it guarantees that:
      • a file named `renovate.json` exists,
      • at least one workflow file exists under `.github/workflows/`.
  If either file is missing the script creates a **default version**, opens a PR,
  and **stops further processing for that repository on this run**.
* When both files are present the script behaves exactly as before:
      – scans for open issues labelled `python_demonstrator`,
      – generates Python code via OpenAI,
      – opens a PR with the generated file and comments on the issue.
* All configuration is read from environment variables, so the same container
  works locally, in CI, or as a k3s CronJob.
"""

import os, re, sys, uuid
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from github import Github, Issue, GithubException
from git import Repo, GitCommandError
import openai
import json

# ----------------------------------------------------------------------
# 1️⃣  Load configuration from the environment
# ----------------------------------------------------------------------
load_dotenv()   # .env → os.environ (useful for local dev)

GH_TOKEN   = os.getenv("GH_TOKEN")          # PAT with `repo` scope
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TARGET_REPOS = os.getenv("TARGET_REPOS")   # "org1/repoA,org2/repoB"
if not all([GH_TOKEN, OPENAI_KEY, TARGET_REPOS]):
    sys.exit("❌ Missing GH_TOKEN, OPENAI_API_KEY or TARGET_REPOS env variable")

ISSUE_LABEL = os.getenv("PYTHON_DEMONSTRATOR_LABEL", "python_demonstrator")


def safe_save_files(files: dict, base_dir: pathlib.Path):
    """
    Write each ``filename: content`` pair to ``base_dir`` after sanitising the name.
    """
    base_dir = base_dir.resolve()                 # absolute path of the project root
    for raw_name, content in files.items():
        # ---- 1️⃣ Reject dangerous names -------------------------------
        # - Absolute paths (start with / or a drive letter)
        # - Path traversal (contain "..")
        # - Empty or whitespace‑only names
        if not raw_name or raw_name.strip() == "":
            raise ValueError("Empty filename supplied.")
        if pathlib.Path(raw_name).is_absolute():
            raise ValueError(f"Absolute path not allowed: {raw_name}")
        if ".." in pathlib.Path(raw_name).parts:
            raise ValueError(f"Path traversal detected in: {raw_name}")

        # ---- 2️⃣ Build the final path ---------------------------------
        target_path = (base_dir / raw_name).resolve()

        # Ensure the resolved path is still inside `base_dir`
        if not str(target_path).startswith(str(base_dir)):
            raise ValueError(f"File {raw_name} resolves outside project root.")

        # ---- 3️⃣ Create parent directories if needed -----------------
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # ---- 4️⃣ Write the file ---------------------------------------
        # Use UTF‑8 encoding; you can also add a trailing newline if you like.
        target_path.write_text(content, encoding="utf-8")
        print(f"✅  Saved {target_path.relative_to(base_dir)}")




# ----------------------------------------------------------------------
# 2️⃣  Helper utilities
# ----------------------------------------------------------------------
def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:50] or f"demo_{uuid.uuid4().hex[:8]}"

def generate_code(prompt: str) -> Any:
    openai.api_key = OPENAI_KEY
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system",
             "content": """
             You are a code generator. Provide the generated files as a JSON object where each key is the file name
             (relative to the project root) and each value is the file content.
              Only output pure JSON, nothing else.
             """},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"}, 
        temperature=0.2,
    )
    json_text = resp.choices[0].message.content
    files_dict = json.loads(json_text)  

    return file_dict

def comment_on_issue(issue: Issue.Issue, message: str):
    issue.create_comment(message)

# ----------------------------------------------------------------------
# 3️⃣  Default file contents
# ----------------------------------------------------------------------
DEFAULT_RENOVATE_JSON = """{
  "extends": [
    "config:base"
  ],
  "schedule": [
    "before 5am on Monday"
  ],
  "automerge": false
}
"""

# The workflow builds a multi‑arch image (amd64 + arm64) and pushes it to ghcr.io.
# It uses the same environment variables that the CronJob already provides
# (GHCR_USERNAME, GHCR_TOKEN) for authentication.
DEFAULT_WORKFLOW_YAML = """name: Build & Push Docker Image

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Log in to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ env.GHCR_USERNAME }}
          password: ${{ secrets.GHCR_TOKEN }}

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          platforms: linux/amd64,linux/arm64
          tags: |
            ghcr.io/${{ env.GHCR_USERNAME }}/python-demonstrator:latest
            ghcr.io/${{ env.GHCR_USERNAME }}/python-demonstrator:${{ github.sha }}
"""

# ----------------------------------------------------------------------
# 4️⃣  Ensure a default renovate.json exists (creates PR if necessary)
# ----------------------------------------------------------------------
def ensure_renovate_file(repo, local_path: Path) -> bool:
    """
    Returns True if the file already existed.
    If it does not exist the function creates a default file, commits it on a
    temporary branch, opens a PR and returns False (caller should skip the rest).
    """
    try:
        repo.get_contents("renovate.json")
        return True
    except GithubException:
        print("   ℹ️  renovate.json missing → creating default")
        (local_path / "renovate.json").write_text(
            DEFAULT_RENOVATE_JSON, encoding="utf-8"
        )
        branch = f"add-renovate-{uuid.uuid4().hex[:6]}"
        try:
            git_repo = Repo(local_path)
            git_repo.git.checkout("-b", branch)
        except GitCommandError as e:
            print(f"   ❌ Could not create branch: {e}")
            return False

        git_repo.index.add(["renovate.json"])
        git_repo.index.commit("Add default renovate.json")
        origin = git_repo.remote(name="origin")
        origin.push(refspec=f"{branch}:{branch}")

        pr = repo.create_pull(
            title="Add default renovate.json",
            body=(
                "A minimal `renovate.json` file was added automatically so that "
                "the repository can be processed by the `python_demonstrator` "
                "workflow. Feel free to adapt it after the PR is merged.\n\n---\n"
                "*Created by `generate_demo.py`*"
            ),
            head=branch,
            base="main",
        )
        print(f"   🎉 PR opened → {pr.html_url}")

        git_repo.git.checkout("main")
        return False

# ----------------------------------------------------------------------
# 5️⃣  Ensure at least one workflow file exists (creates PR if necessary)
# ----------------------------------------------------------------------
def ensure_workflow_file(repo, local_path: Path) -> bool:
    """
    Returns True if any file exists under `.github/workflows/`.
    If none exist the function creates a default workflow (see DEFAULT_WORKFLOW_YAML),
    pushes it on a temporary branch and opens a PR, then returns False.
    """
    try:
        # `get_contents` on a directory returns a list of FileContent objects.
        repo.get_contents(".github/workflows")
        return True
    except GithubException:
        print("   ℹ️  No workflow files → creating default workflow")
        workflow_dir = local_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True, exist_ok=True)

        (workflow_dir / "docker.yml").write_text(
            DEFAULT_WORKFLOW_YAML, encoding="utf-8"
        )

        branch = f"add-workflow-{uuid.uuid4().hex[:6]}"
        try:
            git_repo = Repo(local_path)
            git_repo.git.checkout("-b", branch)
        except GitCommandError as e:
            print(f"   ❌ Could not create branch: {e}")
            return False

        git_repo.index.add([".github/workflows/docker.yml"])
        git_repo.index.commit("Add default GitHub Actions workflow")
        origin = git_repo.remote(name="origin")
        origin.push(refspec=f"{branch}:{branch}")

        pr = repo.create_pull(
            title="Add default GitHub Actions workflow",
            body=(
                "A basic workflow that builds a multi‑arch Docker image and pushes it "
                "to GitHub Container Registry (`ghcr.io`) has been added automatically. "
                "You may edit or extend it after merging.\n\n---\n"
                "*Created by `generate_demo.py`*"
            ),
            head=branch,
            base="main",
        )
        print(f"   🎉 PR opened → {pr.html_url}")

        git_repo.git.checkout("main")
        return False

# ----------------------------------------------------------------------
# 6️⃣  Main per‑repository processing
# ----------------------------------------------------------------------
def process_one_repo(gh: Github, full_name: str) -> None:
    """
    Handles a single repository.
    * Guarantees a `renovate.json` file exists.
    * Guarantees at least one workflow file exists under `.github/workflows/`.
    * If either file had to be created a PR is opened and the function returns
      early (the next hourly run will continue after the PR is merged).
    * Otherwise the standard demo‑generation flow runs.
    """
    repo = gh.get_repo(full_name)
    print(f"\n🚀  Scanning {full_name}")

    # ----- 1️⃣  Prepare a local clone (or reuse) -----
    work_dir = Path("repo_clone") / full_name.replace("/", "_")
    if not work_dir.exists():
        clone_url = repo.clone_url.replace("https://", f"https://{GH_TOKEN}@")
        Repo.clone_from(clone_url, work_dir)

    # ----- 2️⃣  Ensure required files exist -----
    if not ensure_renovate_file(repo, work_dir):
        return         # a PR was opened – skip demo work for now
    if not ensure_workflow_file(repo, work_dir):
        return         # a PR was opened – skip demo work for now

    # ----- 3️⃣  Find issues with the requested label -----
    issues = [
        i for i in repo.get_issues(state="open", labels=[ISSUE_LABEL])
    ]
    if not issues:
        print("   ✅ No open issues with label:", ISSUE_LABEL)
        return

    # ----- 4️⃣  Demo‑generation for each issue -----
    git_repo = Repo(work_dir)
    for issue in issues:
        title = issue.title
        body  = issue.body or ""
        prompt = (
            f"# Issue #{issue.number}: {title}\n\n{body}\n\n"
            "# Write a single Python script that satisfies the request."
        )
        print(f"   ⚙️  Generating code for issue #{issue.number}")

        files_dict = generate_code(prompt)
        # ----------------------------------------------------------------------
        # 3️⃣ Choose a destination folder (e.g. ./generated_project) and save
        # ----------------------------------------------------------------------
        safe_save_files(files_dict, work_dir)

        branch = f"demo-{issue.number}-{uuid.uuid4().hex[:6]}"
        try:
            git_repo.git.checkout("-b", branch)
        except GitCommandError as e:
            print(f"   ❌ Branch creation failed: {e}")
            continue

        git_repo.index.add([str(fpath.relative_to(work_dir))])
        commit_msg = f"Add demo for issue #{issue.number}: {title}"
        git_repo.index.commit(commit_msg)

        origin = git_repo.remote(name="origin")
        origin.push(refspec=f"{branch}:{branch}")

        pr = repo.create_pull(
            title=commit_msg,
            body=(
                f"Generated from issue #{issue.number} by an automated demonstrator.\n\n"
                "---\n*Created by `generate_demo.py`*"
            ),
            head=branch,
            base="main",
        )
        print(f"       🎉 PR opened → {pr.html_url}")

        comment_on_issue(
            issue,
            f"✅ Demo added: [{pr.title}]({pr.html_url})"
        )
        git_repo.git.checkout("main")

def main() -> None:
    gh = Github(GH_TOKEN)
    repos = [r.strip() for r in TARGET_REPOS.split(",") if r.strip()]
    for full_name in repos:
        try:
            process_one_repo(gh, full_name)
        except Exception as exc:
            print(f"❗️ Error processing {full_name}: {exc}")

if __name__ == "__main__":
    main()
