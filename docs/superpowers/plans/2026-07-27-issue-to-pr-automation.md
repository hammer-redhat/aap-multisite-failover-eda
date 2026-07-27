# Issue-to-PR Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a GitHub issue is opened, a GPT-4o-powered workflow reads it, generates file changes, and opens a draft PR; if the PR is closed without merging, it comments on the original issue.

**Architecture:** A Python script (`scripts/generate_pr.py`) makes two sequential GPT-4o calls — the first selects relevant files, the second generates their new contents — then writes files to disk. A GitHub Actions workflow (`pr-agent.yml`) orchestrates checkout, script execution, branch creation, and draft PR opening. A second workflow (`pr-closed.yml`) watches for AI-generated PRs closed without merging and posts feedback to the original issue.

**Tech Stack:** Python 3.11+, `openai` PyPI package (GPT-4o), GitHub Actions, `gh` CLI, `pytest` for unit tests.

## Global Constraints

- Default branch is `aap-26-failover` — all PRs target this branch.
- Secrets available: `OPENAI_KEY`, `GH_TOKEN`.
- AI-generated branches are named `ai-issue-{number}-{slug}` (slug max 50 chars, lowercase alphanumeric and hyphens only).
- Draft PRs must include `Closes #{number}` in the body so `pr-closed.yml` can find the issue.
- Workflows must set `if: github.event.sender.type != 'Bot'` on the issues job to avoid feedback loops.
- `pr-closed.yml` only fires for branches starting with `ai-issue-`.
- No third-party CLI tools beyond `openai` pip package and the pre-installed `gh` CLI.

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `scripts/generate_pr.py` | Create | All GPT-4o logic: file tree, two API calls, file writes, GitHub output, issue comments |
| `tests/test_generate_pr.py` | Create | Unit tests for every function in `generate_pr.py` (mocking OpenAI) |
| `.github/workflows/pr-agent.yml` | Modify | Replace broken pr-agent with issue-to-PR workflow |
| `.github/workflows/pr-closed.yml` | Create | Comment on issue when AI PR is closed unmerged |

---

## Task 1: `scripts/generate_pr.py` — core script

**Files:**
- Create: `scripts/generate_pr.py`
- Create: `tests/test_generate_pr.py`

**Interfaces:**
- Produces: `get_file_tree(repo_root: Path) -> str`
- Produces: `load_files(repo_root: Path, paths: list[str]) -> dict[str, str]`
- Produces: `select_files(client, file_tree: str, issue_title: str, issue_body: str) -> list[str]`
- Produces: `generate_changes(client, file_contents: dict[str, str], issue_title: str, issue_body: str) -> list[dict]`
- Produces: `write_changes(repo_root: Path, changes: list[dict]) -> int`
- Produces: `slugify(text: str, max_len: int = 50) -> str`
- Produces: `write_github_output(key: str, value: str) -> None`
- Produces: `post_issue_comment(github_token: str, repo: str, issue_number: str, body: str) -> None`
- Produces: `main() -> int` (entry point, returns exit code)

- [ ] **Step 1: Install test dependency**

```bash
pip install openai pytest
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_generate_pr.py`:

```python
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

# The script lives at scripts/generate_pr.py — add scripts/ to sys.path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))
import generate_pr


# ── slugify ────────────────────────────────────────────────────────────────

def test_slugify_basic():
    assert generate_pr.slugify('Update README file') == 'update-readme-file'

def test_slugify_special_chars():
    assert generate_pr.slugify('Fix: AAP 2.6 (postgres)!') == 'fix-aap-26-postgres'

def test_slugify_truncates():
    long = 'a' * 60
    result = generate_pr.slugify(long, max_len=50)
    assert len(result) == 50

def test_slugify_collapses_hyphens():
    assert generate_pr.slugify('foo   ---   bar') == 'foo-bar'


# ── get_file_tree ──────────────────────────────────────────────────────────

def test_get_file_tree_excludes_git(tmp_path):
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'config').write_text('x')
    (tmp_path / 'README.md').write_text('hello')
    tree = generate_pr.get_file_tree(tmp_path)
    assert 'README.md' in tree
    assert '.git' not in tree

def test_get_file_tree_excludes_pyc(tmp_path):
    (tmp_path / 'module.pyc').write_bytes(b'\x00')
    (tmp_path / 'script.py').write_text('pass')
    tree = generate_pr.get_file_tree(tmp_path)
    assert 'script.py' in tree
    assert '.pyc' not in tree

def test_get_file_tree_excludes_screenshots(tmp_path):
    (tmp_path / 'screenshots').mkdir()
    (tmp_path / 'screenshots' / 'demo.png').write_bytes(b'\x00')
    (tmp_path / 'playbooks').mkdir()
    (tmp_path / 'playbooks' / 'deploy.yml').write_text('---')
    tree = generate_pr.get_file_tree(tmp_path)
    assert 'playbooks/deploy.yml' in tree
    assert 'screenshots' not in tree


# ── load_files ─────────────────────────────────────────────────────────────

def test_load_files_reads_existing(tmp_path):
    (tmp_path / 'README.md').write_text('# Hello')
    result = generate_pr.load_files(tmp_path, ['README.md'])
    assert result == {'README.md': '# Hello'}

def test_load_files_skips_missing(tmp_path):
    result = generate_pr.load_files(tmp_path, ['nonexistent.md'])
    assert result == {}

def test_load_files_skips_oversized(tmp_path):
    big = tmp_path / 'big.txt'
    big.write_bytes(b'x' * (generate_pr.MAX_FILE_SIZE + 1))
    result = generate_pr.load_files(tmp_path, ['big.txt'])
    assert result == {}


# ── select_files ───────────────────────────────────────────────────────────

def test_select_files_parses_files_key():
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps({'files': ['README.md']})))]
    )
    result = generate_pr.select_files(client, 'README.md\nvars/main.yml', 'Fix docs', 'Update versions')
    assert result == ['README.md']

def test_select_files_parses_plain_list():
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps({'result': ['vars/main.yml']})))]
    )
    result = generate_pr.select_files(client, 'vars/main.yml', 'Fix vars', 'Update')
    assert result == ['vars/main.yml']

def test_select_files_returns_empty_on_empty_files():
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps({'files': []})))]
    )
    result = generate_pr.select_files(client, 'README.md', 'Nothing', 'No changes')
    assert result == []


# ── generate_changes ───────────────────────────────────────────────────────

def test_generate_changes_parses_changes_key():
    client = MagicMock()
    changes = [{'path': 'README.md', 'action': 'modify', 'content': '# New'}]
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps({'changes': changes})))]
    )
    result = generate_pr.generate_changes(client, {'README.md': '# Old'}, 'Fix docs', 'Update')
    assert result == changes

def test_generate_changes_returns_empty_on_no_list():
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps({'changes': []})))]
    )
    result = generate_pr.generate_changes(client, {'README.md': '# Old'}, 'Fix', 'Body')
    assert result == []


# ── write_changes ──────────────────────────────────────────────────────────

def test_write_changes_modifies_existing_file(tmp_path):
    (tmp_path / 'README.md').write_text('# Old')
    changes = [{'path': 'README.md', 'action': 'modify', 'content': '# New'}]
    count = generate_pr.write_changes(tmp_path, changes)
    assert count == 1
    assert (tmp_path / 'README.md').read_text() == '# New'

def test_write_changes_creates_new_file(tmp_path):
    changes = [{'path': 'playbooks/new.yml', 'action': 'create', 'content': '---'}]
    count = generate_pr.write_changes(tmp_path, changes)
    assert count == 1
    assert (tmp_path / 'playbooks' / 'new.yml').read_text() == '---'

def test_write_changes_skips_modify_missing(tmp_path):
    changes = [{'path': 'ghost.yml', 'action': 'modify', 'content': '---'}]
    count = generate_pr.write_changes(tmp_path, changes)
    assert count == 0

def test_write_changes_skips_empty_content(tmp_path):
    changes = [{'path': 'README.md', 'action': 'create', 'content': ''}]
    count = generate_pr.write_changes(tmp_path, changes)
    assert count == 0


# ── write_github_output ────────────────────────────────────────────────────

def test_write_github_output_writes_to_file(tmp_path):
    output_file = tmp_path / 'github_output'
    output_file.write_text('')
    with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output_file)}):
        generate_pr.write_github_output('changes_made', 'true')
    assert 'changes_made=true' in output_file.read_text()

def test_write_github_output_no_env_var_is_noop():
    env = {k: v for k, v in os.environ.items() if k != 'GITHUB_OUTPUT'}
    with patch.dict(os.environ, env, clear=True):
        generate_pr.write_github_output('key', 'val')  # should not raise


# ── post_issue_comment ─────────────────────────────────────────────────────

def test_post_issue_comment_calls_github_api():
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        generate_pr.post_issue_comment('tok', 'org/repo', '5', 'hello')
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert 'issues/5/comments' in req.full_url
```

- [ ] **Step 3: Run tests — expect failures (functions not yet defined)**

```bash
cd /Users/chrhamme/aap-multisite-failover-eda
pytest tests/test_generate_pr.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError` or `ImportError` — `generate_pr` doesn't exist yet.

- [ ] **Step 4: Create `scripts/generate_pr.py`**

```python
#!/usr/bin/env python3
"""Two-step GPT-4o issue-to-PR change generator."""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import openai

EXCLUDED_DIRS = {'.git', 'screenshots', '__pycache__'}
EXCLUDED_EXTENSIONS = {'.pyc', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.pdf'}
MAX_FILE_SIZE = 50_000  # bytes


def get_file_tree(repo_root: Path) -> str:
    """Return a newline-separated list of all non-excluded repo files."""
    lines = []
    for path in sorted(repo_root.rglob('*')):
        if not path.is_file():
            continue
        parts = path.relative_to(repo_root).parts
        if any(p in EXCLUDED_DIRS for p in parts):
            continue
        if path.suffix in EXCLUDED_EXTENSIONS:
            continue
        lines.append(str(path.relative_to(repo_root)))
    return '\n'.join(lines)


def load_files(repo_root: Path, paths: list) -> dict:
    """Load file contents, skipping missing or oversized files."""
    contents = {}
    for p in paths:
        full = repo_root / p
        if not full.exists():
            print(f'WARNING: {p} does not exist, skipping', file=sys.stderr)
            continue
        if full.stat().st_size > MAX_FILE_SIZE:
            print(f'WARNING: {p} too large ({full.stat().st_size} bytes), skipping', file=sys.stderr)
            continue
        contents[p] = full.read_text(encoding='utf-8')
    return contents


def select_files(client, file_tree: str, issue_title: str, issue_body: str) -> list:
    """Call 1: ask GPT-4o which files need to change. Returns list of paths."""
    response = client.chat.completions.create(
        model='gpt-4o',
        response_format={'type': 'json_object'},
        messages=[
            {
                'role': 'system',
                'content': (
                    'You are an expert at Ansible automation and infrastructure-as-code. '
                    'Given a GitHub issue and a repo file tree, return a JSON object with '
                    'key "files" containing an array of file paths that need to be created '
                    'or modified to address the issue. '
                    'Return {"files": []} if no changes are needed. '
                    'Return ONLY valid JSON, no explanation.'
                ),
            },
            {
                'role': 'user',
                'content': (
                    f'Issue title: {issue_title}\n\n'
                    f'Issue body:\n{issue_body}\n\n'
                    f'Repo file tree:\n{file_tree}'
                ),
            },
        ],
    )
    data = json.loads(response.choices[0].message.content)
    if 'files' in data and isinstance(data['files'], list):
        return data['files']
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


def generate_changes(client, file_contents: dict, issue_title: str, issue_body: str) -> list:
    """Call 2: ask GPT-4o for full new file contents. Returns list of change dicts."""
    files_block = '\n\n'.join(
        f'=== {path} ===\n{content}' for path, content in file_contents.items()
    )
    response = client.chat.completions.create(
        model='gpt-4o',
        response_format={'type': 'json_object'},
        messages=[
            {
                'role': 'system',
                'content': (
                    'You are an expert at Ansible automation and infrastructure-as-code. '
                    'Given a GitHub issue and the current contents of relevant files, '
                    'return a JSON object with key "changes" containing an array of objects. '
                    'Each object must have: '
                    '"path" (string, repo-relative), '
                    '"action" ("modify" for existing files or "create" for new files), '
                    '"content" (complete new file content as a string). '
                    'Return ONLY valid JSON, no explanation.'
                ),
            },
            {
                'role': 'user',
                'content': (
                    f'Issue title: {issue_title}\n\n'
                    f'Issue body:\n{issue_body}\n\n'
                    f'Current file contents:\n\n{files_block}'
                ),
            },
        ],
    )
    data = json.loads(response.choices[0].message.content)
    if 'changes' in data and isinstance(data['changes'], list):
        return data['changes']
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


def write_changes(repo_root: Path, changes: list) -> int:
    """Write changes to disk. Returns number of files actually written."""
    written = 0
    for change in changes:
        path = change.get('path', '').strip()
        action = change.get('action', '').strip()
        content = change.get('content', '')
        if not path or not content:
            print(f'WARNING: Skipping change with missing path or content', file=sys.stderr)
            continue
        full = repo_root / path
        if action == 'modify' and not full.exists():
            print(f'WARNING: modify target {path} does not exist, skipping', file=sys.stderr)
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding='utf-8')
        print(f'Written: {path}')
        written += 1
    return written


def slugify(text: str, max_len: int = 50) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    return text[:max_len]


def write_github_output(key: str, value: str) -> None:
    """Append key=value to $GITHUB_OUTPUT if set."""
    output_file = os.environ.get('GITHUB_OUTPUT')
    if output_file:
        with open(output_file, 'a') as f:
            f.write(f'{key}={value}\n')


def post_issue_comment(github_token: str, repo: str, issue_number: str, body: str) -> None:
    """Post a comment on a GitHub issue via the REST API."""
    url = f'https://api.github.com/repos/{repo}/issues/{issue_number}/comments'
    data = json.dumps({'body': body}).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Bearer {github_token}')
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req) as resp:
        if resp.status not in (200, 201):
            print(f'WARNING: Failed to post comment, status {resp.status}', file=sys.stderr)


def main() -> int:
    openai_key = os.environ.get('OPENAI_KEY') or os.environ.get('OPENAI.KEY')
    if not openai_key:
        print('ERROR: OPENAI_KEY not set', file=sys.stderr)
        return 1

    github_token = os.environ.get('GITHUB_TOKEN')
    if not github_token:
        print('ERROR: GITHUB_TOKEN not set', file=sys.stderr)
        return 1

    issue_number = os.environ.get('ISSUE_NUMBER', '')
    issue_title = os.environ.get('ISSUE_TITLE', '')
    issue_body = os.environ.get('ISSUE_BODY', '')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    repo_root = Path(os.environ.get('GITHUB_WORKSPACE', '.')).resolve()

    client = openai.OpenAI(api_key=openai_key)

    # ── Call 1: select files ──────────────────────────────────────────────
    print('Step 1: Selecting files to modify...')
    file_tree = get_file_tree(repo_root)
    try:
        selected = select_files(client, file_tree, issue_title, issue_body)
    except Exception as e:
        print(f'ERROR in Call 1: {e}', file=sys.stderr)
        post_issue_comment(github_token, repo, issue_number,
            'AI automation failed to identify files to change. '
            'Please add more detail to the issue or make the changes manually.')
        write_github_output('changes_made', 'false')
        return 0

    if not selected:
        print('No files selected.')
        post_issue_comment(github_token, repo, issue_number,
            'The AI agent could not identify which files need to change for this issue. '
            'Please add more detail or make the changes manually.')
        write_github_output('changes_made', 'false')
        return 0

    print(f'Selected: {selected}')
    file_contents = load_files(repo_root, selected)

    # ── Call 2: generate changes ──────────────────────────────────────────
    print('Step 2: Generating changes...')
    changes = None
    for attempt in range(2):
        try:
            changes = generate_changes(client, file_contents, issue_title, issue_body)
            break
        except Exception as e:
            print(f'Attempt {attempt + 1} failed: {e}', file=sys.stderr)

    if changes is None:
        post_issue_comment(github_token, repo, issue_number,
            'The AI agent failed to generate valid changes after two attempts. '
            'Please make the changes manually.')
        write_github_output('changes_made', 'false')
        return 0

    # ── Write files ───────────────────────────────────────────────────────
    written = write_changes(repo_root, changes)

    if written == 0:
        post_issue_comment(github_token, repo, issue_number,
            'The AI agent ran but did not produce any file changes. '
            'Please make the changes manually.')
        write_github_output('changes_made', 'false')
        return 0

    write_github_output('changes_made', 'true')
    write_github_output('branch_slug', slugify(issue_title))
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 5: Run tests — expect all to pass**

```bash
cd /Users/chrhamme/aap-multisite-failover-eda
pytest tests/test_generate_pr.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_pr.py tests/test_generate_pr.py
git commit -m "feat: add generate_pr.py with GPT-4o two-step logic and unit tests"
```

---

## Task 2: Update `.github/workflows/pr-agent.yml`

**Files:**
- Modify: `.github/workflows/pr-agent.yml`

**Interfaces:**
- Consumes: `scripts/generate_pr.py` (Task 1) — runs it as `python scripts/generate_pr.py`
- Consumes: step output `steps.generate.outputs.changes_made` and `steps.generate.outputs.branch_slug`

- [ ] **Step 1: Replace the full content of `.github/workflows/pr-agent.yml`**

Read the current file first (it lives at `.github/workflows/pr-agent.yml`), then replace it entirely with:

```yaml
name: Issue-to-PR Agent

on:
  issues:
    types: [opened]

jobs:
  issue_agent:
    runs-on: ubuntu-latest
    if: ${{ github.event.sender.type != 'Bot' }}
    timeout-minutes: 10
    permissions:
      issues: write
      pull-requests: write
      contents: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.GH_TOKEN }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install openai

      - name: Generate PR changes
        id: generate
        env:
          OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
          GITHUB_TOKEN: ${{ secrets.GH_TOKEN }}
          GITHUB_REPOSITORY: ${{ github.repository }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          ISSUE_TITLE: ${{ github.event.issue.title }}
          ISSUE_BODY: ${{ github.event.issue.body }}
          GITHUB_WORKSPACE: ${{ github.workspace }}
        run: python scripts/generate_pr.py

      - name: Create branch and open draft PR
        if: steps.generate.outputs.changes_made == 'true'
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
          ISSUE_NUMBER: ${{ github.event.issue.number }}
          ISSUE_TITLE: ${{ github.event.issue.title }}
          BRANCH_SLUG: ${{ steps.generate.outputs.branch_slug }}
        run: |
          BRANCH="ai-issue-${ISSUE_NUMBER}-${BRANCH_SLUG}"
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git checkout -b "$BRANCH"
          git add -A
          git commit -m "ai: address issue #${ISSUE_NUMBER} – ${ISSUE_TITLE}"
          git push origin "$BRANCH"
          gh pr create \
            --draft \
            --title "ai: ${ISSUE_TITLE}" \
            --body "Closes #${ISSUE_NUMBER}

Generated by AI based on issue description. Please review all changes carefully before merging." \
            --base aap-26-failover
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/pr-agent.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/pr-agent.yml
git commit -m "feat: replace pr-agent workflow with issue-to-PR automation"
```

---

## Task 3: Create `.github/workflows/pr-closed.yml`

**Files:**
- Create: `.github/workflows/pr-closed.yml`

**Interfaces:**
- Consumes: `github.event.pull_request.body` — must contain `Closes #N`
- Consumes: `github.event.pull_request.head.ref` — must start with `ai-issue-`

- [ ] **Step 1: Create `.github/workflows/pr-closed.yml`**

```yaml
name: PR Closed Feedback

on:
  pull_request:
    types: [closed]

jobs:
  notify_issue:
    runs-on: ubuntu-latest
    if: |
      github.event.pull_request.merged == false &&
      startsWith(github.event.pull_request.head.ref, 'ai-issue-')
    permissions:
      issues: write
    steps:
      - name: Post comment on related issue
        env:
          GH_TOKEN: ${{ secrets.GH_TOKEN }}
          PR_BODY: ${{ github.event.pull_request.body }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          REPO: ${{ github.repository }}
        run: |
          ISSUE_NUMBER=$(echo "$PR_BODY" | grep -oP '(?<=Closes #)\d+' | head -1)
          if [ -n "$ISSUE_NUMBER" ]; then
            gh issue comment "$ISSUE_NUMBER" \
              --repo "$REPO" \
              --body "Draft PR #${PR_NUMBER} was closed without merging. Open a new issue with updated requirements to try again."
          fi
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/pr-closed.yml'))" && echo "YAML OK"
```

Expected: `YAML OK`

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/pr-closed.yml
git commit -m "feat: add pr-closed feedback workflow"
git push origin aap-26-failover
```

- [ ] **Step 4: End-to-end smoke test**

Open a new issue in the repo with a concrete, narrow request (e.g., "Update the README to mention that OCP 4.20 is now supported"). Watch the Actions tab:

1. `Issue-to-PR Agent` workflow should start within ~30 seconds.
2. Python install + `generate_pr.py` should run (allow up to 2 minutes for GPT-4o).
3. A draft PR titled `ai: Update the README...` should appear, targeting `aap-26-failover`.
4. The PR diff should contain the expected change to `README.md`.
5. Close the draft PR without merging → `PR Closed Feedback` workflow fires → a comment appears on the original issue.
