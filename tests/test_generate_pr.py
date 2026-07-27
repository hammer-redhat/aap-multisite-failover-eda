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
