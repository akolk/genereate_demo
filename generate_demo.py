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
      – generates Python (or Streamlit) code via OpenAI,
      – opens a PR with the generated files and comments on the issue.
* All configuration is read from environment variables, so the same container
  works locally, in CI, or as a k3s CronJob.
"""

import os, re, sys, uuid, json
from pathlib import Path
from typing import Any, List

from dotenv import load_dotenv
from github import Github, Issue, GithubException
from git import Repo, GitCommandError
import openai

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
IN_PROGRESS_LABEL = "in-progress"          # ← new constant

# ----------------------------------------------------------------------
# 2️⃣  Helper utilities
# ----------------------------------------------------------------------
def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:50] or f"demo_{uuid.uuid4().hex[:8]}"

def generate_code(prompt: str) -> Any:
    """
    Calls OpenAI and returns a dict {filename: file_content, ...}
    The model must output **pure JSON** (response_format = json_object).
    """
    openai.api_key = OPENAI_KEY

    system_prompt = """
    You are a Python code generator.

    • Write **only Python code** – no other languages.
    • If the requested solution should be a web application, implement it with **Streamlit**.
    • **Always generate a Dockerfile** that can build and run the produced Python code
      (including Streamlit UI if present). Place the Dockerfile at the repository root
      and name it `Dockerfile`.
    • Generate **all files** that are required for the program (multiple .py files,
      requirements.txt, README.md, etc.).
    • Return the result as a **single JSON object** where each key is the filename
      (relative to the repository root) and each value is the complete file content.
    • Do **not** include any additional text, explanations or markdown – the output
      must be pure JSON.
    """

    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    json_text = resp.choices[0].message.content
    files_dict = json.loads(json_text)   # {filename: content, ...}
    return files_dict

def comment_on_issue(issue: Issue.Issue, message: str) -> None:
    issue.create_comment(message)

# ----------------------------------------------------------------------
# 3️⃣  Safe file write helper
# ----------------------------------------------------------------------
def safe_save_files(files: dict, base_dir: Path) -> None:
    """
    Write each ``filename: content`` pair to ``base_dir`` after sanitising the name.
    """
    base_dir = base_dir.resolve()                 # absolute path of the project root
    for raw_name, content in files.items():
        if not raw_name or raw_name.strip() == "":
            raise ValueError("Empty filename supplied.")
        if Path(raw_name).is_absolute():
            raise ValueError(f"Absolute path not allowed: {raw_name}")
        if ".." in Path(raw_name).parts:
            raise ValueError(f"Path traversal detected in: {raw_name}")

        target_path = (base_dir / raw_name).resolve()
        if not str(target_path).startswith(str(base_dir)):
            raise ValueError(f"File {raw_name} resolves outside project root.")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        print(f"✅  Saved {target_path.relative_to(base_dir)}")

# ----------------------------------------------------------------------
# 4️⃣  Default file contents
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
# 5️⃣  Ensure required defaults exist (creates PR if needed)
# ----------------------------------------------------------------------
def ensure_renovate_file(repo, local_path: Path) -> bool:
    """Return True if renovate.json already exists, otherwise create it + PR & return False."""
    try:
        repo.get_contents("renovate.json")
        return True
    except GithubException:
        print("   ℹ️  renovate.json missing → creating default")
        (local_path / "renovate.json").write_text(
            DEFAULT_RENOVATE_JSON, encoding="utf-8"
        )
        branch = f"add-renovate-{uuid.uuid4().hex[:6]}"
        git_repo = Repo(local_path)
        git_repo.git.checkout("-b", branch)
        git_repo.index.add(["renovate.json"])
        git_repo.index.commit("Add default renovate.json")
        git_repo.remote(name="origin").push(refspec=f"{branch}:{branch}")

        pr = repo.create_pull(
            title="Add default renovate.json",
            body=(
                "A minimal `renovate.json` file was added automatically so that "
                "the repository can be processed by the `python_demonstrator` workflow. "
                "Feel free to adapt it after the PR is merged.\n\n---\n*Created by `generate_demo.py`*"
            ),
            head=branch,
            base="main",
        )
        print(f"   🎉 PR opened → {pr.html_url}")
        git_repo.git.checkout("main")
        return False


def ensure_workflow_file(repo, local_path: Path) -> bool:
    """Return True if at least one workflow exists, otherwise create default + PR & return False."""
    try:
        repo.get_contents(".github/workflows")
        return True
    except GithubException:
        print("   ℹ️  No workflow files → creating default workflow")
        wf_dir = local_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "docker.yml").write_text(DEFAULT_WORKFLOW_YAML, encoding="utf-8")

        branch = f"add-workflow-{uuid.uuid4().hex[:6]}"
        git_repo = Repo(local_path)
        git_repo.git.checkout("-b", branch)
        git_repo.index.add([".github/workflows/docker.yml"])
        git_repo.index.commit("Add default GitHub Actions workflow")
        git_repo.remote(name="origin").push(refspec=f"{branch}:{branch}")

        pr = repo.create_pull(
            title="Add default GitHub Actions workflow",
            body=(
                "A basic workflow that builds a multi‑arch Docker image and pushes it "
                "to GitHub Container Registry (`ghcr.io`) has been added automatically. "
                "You may edit or extend it after merging.\n\n---\n*Created by `generate_demo.py`*"
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
    Process a single repository:
      * guarantee renovate.json + at least one workflow
      * if either needed a PR → stop further work for this run
      * otherwise find open issues with `ISSUE_LABEL` that are *not* already `in-progress`
      * generate code, commit, push and open a PR
      * label the issue with `in-progress` and comment the PR link
    """
    repo = gh.get_repo(full_name)
    print(f"\n🚀  Scanning {full_name}")

    # ----- clone (or reuse) -----
    work_dir = Path("repo_clone") / full_name.replace("/", "_")
    if not work_dir.exists():
        clone_url = repo.clone_url.replace("https://", f"https://{GH_TOKEN}@")
        Repo.clone_from(clone_url, work_dir)

    # ----- ensure defaults exist -----
    if not ensure_renovate_file(repo, work_dir):
        return
    if not ensure_workflow_file(repo, work_dir):
        return

    # ----- fetch issues (skip those already marked in-progress) -----
    raw_issues = list(repo.get_issues(state="open", labels=[ISSUE_LABEL]))
    issues = [
        i for i in raw_issues
        if IN_PROGRESS_LABEL not in [lbl.name for lbl in i.labels]
    ]

    if not issues:
        print(f"   ✅ No open issues with label '{ISSUE_LABEL}' (or all already {IN_PROGRESS_LABEL})")
        return

    git_repo = Repo(work_dir)

    for issue in issues:
        title = issue.title
        body = issue.body or ""
        prompt = (
            f"# Issue #{issue.number}: {title}\n\n{body}\n\n"
            "# Write a single Python script that satisfies the request."
        )
        print(f"   ⚙️  Generating code for issue #{issue.number}")

        files_dict = generate_code(prompt)

        # Save every file the model returned
        safe_save_files(files_dict, work_dir)

        branch = f"demo-{issue.number}-{uuid.uuid4().hex[:6]}"
        try:
            git_repo.git.checkout("-b", branch)
        except GitCommandError as e:
            print(f"   ❌ Branch creation failed: {e}")
            continue

        # Add **all** generated files (recursively under work_dir)
        paths_to_add = [
            str(p.relative_to(work_dir))
            for p in work_dir.rglob("*")
            if p.is_file()
        ]
        git_repo.index.add(paths_to_add)

        commit_msg = f"Add demo for issue #{issue.number}: {title}"
        git_repo.index.commit(commit_msg)

        git_repo.remote(name="origin").push(refspec=f"{branch}:{branch}")

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

        # ---- label the issue as in‑progress ----
        try:
            issue.add_to_labels(IN_PROGRESS_LABEL)
        except GithubException as e:
            print(f"   ⚠️  Could not add label {IN_PROGRESS_LABEL}: {e}")

        comment_on_issue(
            issue,
            f"✅ Demo added: [{pr.title}]({pr.html_url}) – labeled **{IN_PROGRESS_LABEL}**."
        )

        git_repo.git.checkout("main")

# ----------------------------------------------------------------------
# 7️⃣  Entry point
# ----------------------------------------------------------------------
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
