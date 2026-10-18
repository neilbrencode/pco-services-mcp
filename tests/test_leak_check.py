"""The leak check against throwaway git repos. All terms and secrets are synthetic."""

from __future__ import annotations

import base64
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "leak_check", Path(__file__).parents[1] / "scripts" / "leak_check.py"
)
leak_check = importlib.util.module_from_spec(_SPEC)
sys.modules["leak_check"] = leak_check  # dataclasses look their module up here
_SPEC.loader.exec_module(leak_check)

TERM = "Example Fellowship of Springfield"


def run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv(leak_check.ENV_SKIP_KEYRING, "1")
    monkeypatch.delenv("PCO_APPLICATION_ID", raising=False)
    monkeypatch.delenv("PCO_SECRET", raising=False)
    run(tmp_path, "init", "-q", "-b", "main")
    run(tmp_path, "config", "user.email", "test@example.invalid")
    run(tmp_path, "config", "user.name", "Test")
    run(tmp_path, "config", "core.hooksPath", "/dev/null")
    (tmp_path / leak_check.DENYLIST).write_text(f"# local only\n{TERM}\n", encoding="utf-8")
    return tmp_path


def commit(repo: Path, name: str, text: str, message: str = "change") -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    run(repo, "add", "--", name)
    run(repo, "commit", "-q", "-m", message)


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    run(repo, "add", "-f", "--", name)


def check(repo: Path, *mode: str) -> int:
    return leak_check.main([*mode, "--repo", str(repo)])


def test_clean_staged_change_passes(repo):
    stage(repo, "README.md", "A server for Example Church volunteers.\n")
    assert check(repo, "--staged") == 0


def test_denylisted_term_is_blocked_case_insensitively(repo, capsys):
    stage(repo, "notes.md", f"tested at {TERM.upper()}\n")
    assert check(repo, "--staged") == 1
    err = capsys.readouterr().err
    assert "denylisted term #1" in err and TERM.lower() not in err.lower()


def test_sensitive_filename_is_blocked(repo):
    stage(repo, f"fixtures/{TERM}.json", "{}\n")
    assert check(repo, "--staged") == 1


def test_binary_file_names_and_contents_are_checked(repo):
    (repo / "blob.bin").write_bytes(b"\x00\xff" + TERM.encode() + b"\x00")
    run(repo, "add", "--", "blob.bin")
    assert check(repo, "--staged") == 1
    run(repo, "rm", "-q", "--cached", "blob.bin")
    (repo / f"{TERM}.bin").write_bytes(b"\x00\xffclean\x00")
    run(repo, "add", "--", f"{TERM}.bin")
    assert check(repo, "--staged") == 1


@pytest.mark.parametrize("mode", ["--staged", "--history"])
def test_an_added_line_starting_with_plus_signs_is_checked(repo, mode):
    stage(repo, "notes.md", f"++{TERM}\n+++ {TERM}\n")
    if mode == "--history":
        run(repo, "commit", "-q", "-m", "notes")
    assert check(repo, mode) == 1


def test_a_matching_file_name_is_reported_without_the_name(repo, capsys, monkeypatch):
    monkeypatch.setenv("PCO_APPLICATION_ID", "synthetic-app")
    monkeypatch.setenv("PCO_SECRET", "synthetic-secret-value")
    stage(repo, f"{TERM}.md", f"{TERM}\n")
    stage(repo, "synthetic-secret-value.txt", "clean\n")
    assert check(repo, "--staged") == 1
    err = capsys.readouterr().err
    assert "withheld" in err
    assert TERM.lower() not in err.lower() and "synthetic-secret-value" not in err


def test_accented_and_empty_file_names_are_checked(repo):
    (repo / leak_check.DENYLIST).write_text("Iglesia Café\n", encoding="utf-8")
    stage(repo, "Iglesia Café/notes.md", "clean\n")
    assert check(repo, "--staged") == 1
    run(repo, "rm", "-q", "-r", "--cached", "Iglesia Café")
    (repo / leak_check.DENYLIST).write_text(f"{TERM}\n", encoding="utf-8")
    stage(repo, f"{TERM}.keep", "")
    assert check(repo, "--staged") == 1


def test_binary_file_in_history_is_checked(repo):
    (repo / "blob.bin").write_bytes(b"\x00\xff" + TERM.encode() + b"\x00")
    run(repo, "add", "--", "blob.bin")
    run(repo, "commit", "-q", "-m", "add blob")
    assert check(repo, "--history") == 1


def test_staging_the_denylist_itself_is_blocked(repo):
    run(repo, "add", "-f", leak_check.DENYLIST)
    assert check(repo, "--staged") == 1


def test_environment_credential_is_blocked_raw_and_encoded(repo, monkeypatch):
    monkeypatch.setenv("PCO_APPLICATION_ID", "synthetic-app")
    monkeypatch.setenv("PCO_SECRET", "synthetic-secret-value")
    stage(repo, "a.txt", "secret=synthetic-secret-value\n")
    assert check(repo, "--staged") == 1
    run(repo, "rm", "-q", "--cached", "a.txt")
    encoded = base64.b64encode(b"synthetic-app:synthetic-secret-value").decode()
    stage(repo, "b.txt", f"auth {encoded}\n")
    assert check(repo, "--staged") == 1


@pytest.mark.parametrize(
    "text",
    [
        # Split so that this file passes its own check.
        "token pco_" + "pat_abcdefgh12345678\n",
        "id = " + "ab12" * 16 + "\n",
        "Authorization: Ba" + "sic dXNlcjpwYXNzd29yZDEyMw==\n",
        "Ba" + "sic dXNlcjpwYXNzd29yZDEyMw==\n",
        "https://s3.amazonaws.com/b/k?X-Amz-" + "Signature=abc\n",
    ],
)
def test_secret_shapes_are_blocked(repo, text):
    stage(repo, "x.txt", text)
    assert check(repo, "--staged") == 1


def test_lockfile_hashes_are_not_secrets(repo):
    url = "https://files.pythonhosted.org/packages/5a/8e/" + "0f" * 30 + "/pkg.whl"
    stage(repo, "uv.lock", f'wheel = {{ url = "{url}", hash = "sha256:' + "0f" * 32 + '" }\n')
    assert check(repo, "--staged") == 0


def test_numeric_terms_match_whole_numbers_only(repo):
    (repo / leak_check.DENYLIST).write_text("4321\n", encoding="utf-8")
    stage(repo, "a.txt", "hash ab4321cd and 143210\n")
    assert check(repo, "--staged") == 0
    stage(repo, "b.txt", "organization 4321\n")
    assert check(repo, "--staged") == 1


def test_commit_message_is_checked(repo, tmp_path_factory):
    msg = tmp_path_factory.mktemp("msg") / "COMMIT_EDITMSG"
    msg.write_text(f"Fix schedule for {TERM}\n", encoding="utf-8")
    assert check(repo, "--message", str(msg)) == 1
    msg.write_text("Fix schedule paging\n", encoding="utf-8")
    assert check(repo, "--message", str(msg)) == 0


def test_history_finds_a_term_removed_in_a_later_commit(repo):
    commit(repo, "a.md", f"{TERM}\n")
    commit(repo, "a.md", "clean now\n")
    assert check(repo, "--staged") == 0
    assert check(repo, "--history") == 1


def test_history_checks_messages_and_other_refs(repo):
    commit(repo, "a.md", "fine\n")
    run(repo, "checkout", "-q", "-b", "side")
    commit(repo, "b.md", "fine\n", message=f"visit {TERM}")
    run(repo, "checkout", "-q", "main")
    assert check(repo, "--history") == 1


def test_missing_or_empty_denylist_fails_closed(repo):
    (repo / leak_check.DENYLIST).write_text("# nothing yet\n", encoding="utf-8")
    assert check(repo, "--staged") == 1
    (repo / leak_check.DENYLIST).unlink()
    assert check(repo, "--staged") == 1


def test_unreadable_credential_store_fails_closed(repo, monkeypatch):
    import keyring

    def broken(*_args):
        raise RuntimeError("no backend")

    monkeypatch.delenv(leak_check.ENV_SKIP_KEYRING)
    monkeypatch.setattr(keyring, "get_password", broken)
    stage(repo, "README.md", "clean\n")
    assert check(repo, "--staged") == 1


def test_history_checks_branch_and_tag_names_and_tag_messages(repo):
    commit(repo, "a.md", "fine\n")
    assert check(repo, "--history") == 0
    run(repo, "branch", TERM.replace(" ", "-"))
    assert check(repo, "--history") == 1
    run(repo, "branch", "-D", TERM.replace(" ", "-"))
    run(repo, "tag", "-a", "v1", "-m", f"release for {TERM}")
    assert check(repo, "--history") == 1


@pytest.mark.parametrize(
    "name", ["example-fellowship-of-springfield.md", "docs/Example_Fellowship_of/Springfield.md"]
)
def test_slugged_file_names_are_checked(repo, name):
    stage(repo, name, "clean\n")
    assert check(repo, "--staged") == 1
