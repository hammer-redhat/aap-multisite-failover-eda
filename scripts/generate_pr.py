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
        full = (repo_root / path).resolve()
        repo_resolved = repo_root.resolve()
        if not str(full).startswith(str(repo_resolved) + os.sep):
            print(f'WARNING: {path} escapes repo root, skipping', file=sys.stderr)
            continue
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
    return text[:max_len].rstrip('-')


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
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status not in (200, 201):
                print(f'WARNING: Failed to post comment, status {resp.status}', file=sys.stderr)
    except Exception as e:
        print(f'WARNING: Failed to post issue comment: {e}', file=sys.stderr)


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

    if not file_contents:
        post_issue_comment(github_token, repo, issue_number,
            'All selected files were missing or too large to load. '
            'Please make the changes manually.')
        write_github_output('changes_made', 'false')
        return 0

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
