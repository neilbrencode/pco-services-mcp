"""Block commits that would reveal the maintainer's church or a credential.

    python scripts/leak_check.py --staged          # pre-commit: staged paths and added lines
    python scripts/leak_check.py --message FILE    # commit-msg: the message being written
    python scripts/leak_check.py --history         # before publishing: every commit on every ref

What it catches:
- any term in `.leakcheck-denylist` (gitignored, one per line, `#` comments): the church's
  name, city, campus, your Planning Center ids, people's full names;
- the Personal Access Token currently stored (keyring) or set in the environment, raw or as a
  Base64 `id:secret` pair;
- secret-shaped strings: `pco_pat_…`, long hex runs, `Basic <base64>`, AWS signatures and keys.

What it cannot catch: a token you have since replaced that matches none of the shapes, or a
term you never put in the denylist. Deleting something in a later commit does not remove it
from earlier history; rewrite history before publishing instead.

Matches are reported by location and rule only, never by the matched text.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DENYLIST = ".leakcheck-denylist"
ENV_SKIP_KEYRING = "LEAKCHECK_NO_KEYRING"
KEYRING_SERVICE = "pco-services-mcp"

PATTERNS = {
    "pco-pat": re.compile(r"pco_pat_[A-Za-z0-9_\-]{8,}"),
    "long-hex": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])"),
    "basic-auth": re.compile(r"\bBasic\s+[A-Za-z0-9+/]{16,}={0,2}"),
    "aws-signature": re.compile(r"X-Amz-(Signature|Credential)=", re.IGNORECASE),
    "aws-key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
}


@dataclass(frozen=True)
class Finding:
    where: str
    rule: str


class SetupError(RuntimeError):
    pass


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        # quotepath=false: otherwise non-ASCII file names arrive as octal escapes.
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def load_denylist(repo: Path) -> list[str]:
    path = repo / DENYLIST
    if not path.is_file():
        raise SetupError(f"{DENYLIST} is missing; create it (see README) before committing.")
    terms = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not terms:
        raise SetupError(f"{DENYLIST} has no terms; add your church's identifying details.")
    return terms


def load_secrets() -> list[str]:
    pairs = [(os.environ.get("PCO_APPLICATION_ID"), os.environ.get("PCO_SECRET"))]
    if not os.environ.get(ENV_SKIP_KEYRING):
        try:
            import keyring

            pairs.append(
                (
                    keyring.get_password(KEYRING_SERVICE, "application_id"),
                    keyring.get_password(KEYRING_SERVICE, "secret"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - any backend failure means "can't check"
            raise SetupError(
                f"Can't read the credential store ({type(exc).__name__}); set "
                f"{ENV_SKIP_KEYRING}=1 to commit without checking the stored token."
            ) from None
    secrets: list[str] = []
    for app_id, secret in pairs:
        secrets += [v for v in (app_id, secret) if v]
        if app_id and secret:
            secrets.append(base64.b64encode(f"{app_id}:{secret}".encode()).decode())
    return secrets


# Lockfile package hashes and their download URLs: public, and hex-shaped.
_PACKAGE_HASHES = re.compile(
    r"sha256:[0-9a-f]{64}|https://files\.pythonhosted\.org/packages/[0-9a-f/]+", re.IGNORECASE
)


def _term_pattern(term: str) -> re.Pattern:
    # A short id would match inside any hash; numbers only count as whole tokens.
    if term.isdigit():
        return re.compile(rf"(?<![0-9A-Za-z]){term}(?![0-9A-Za-z])")
    return re.compile(re.escape(term), re.IGNORECASE)


_SEPARATORS = re.compile(r"[-_./+]+")


class Scanner:
    def __init__(self, terms: list[str], secrets: list[str]):
        self.terms = [(i, _term_pattern(t)) for i, t in enumerate(terms, 1)]
        self.secrets = secrets

    def scan(self, text: str, where: str) -> list[Finding]:
        found: list[Finding] = []
        # Branch names, file names and URL slugs have no spaces: "Grace-Church" is "Grace Church".
        spaced = _SEPARATORS.sub(" ", text)
        found += [
            Finding(where, f"denylisted term #{i}")
            for i, rx in self.terms
            if rx.search(text) or rx.search(spaced)
        ]
        found += [Finding(where, "stored credential") for s in self.secrets if s in text]
        shaped = _PACKAGE_HASHES.sub("", text)
        found += [Finding(where, name) for name, rx in PATTERNS.items() if rx.search(shaped)]
        return found


# --text: a binary file otherwise shows as "Binary files differ", hiding its bytes.
# A merge is compared with its first parent, so the patch never uses the two-column format.
_DIFF = ("-U0", "--text", "--no-color", "--no-ext-diff", "-M", "--diff-merges=first-parent")
_WITHHELD = "a file whose name is withheld"


def _shown(scanner: Scanner, name: str) -> str:
    # A name that itself matches is never printed: the report would leak what it blocked.
    return _WITHHELD if scanner.scan(name, "") else name


def _name_findings(scanner: Scanner, names: list[str], label: str) -> list[Finding]:
    """Names come from `--name-only -z`, which lists every file, empty or binary, unquoted."""
    found: list[Finding] = []
    for name in filter(None, names):
        if Path(name).name == DENYLIST:
            found.append(Finding(f"{label}path {name}", "denylist file staged"))
        found += scanner.scan(name, f"{label}path {_shown(scanner, name)}")
    return found


def _content_findings(scanner: Scanner, patch: str, label: str) -> list[Finding]:
    """Scan the added lines of a unified diff."""
    found: list[Finding] = []
    path, in_hunk = "?", False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            # Inside a hunk every "+" line is content, even one reading "+++ ...".
            if line.startswith("+"):
                found += scanner.scan(line[1:], f"{label}{_shown(scanner, path)}")
        elif line.startswith("+++ b/"):
            path = line[len("+++ b/") :]
    return found


def _change_findings(scanner: Scanner, names: str, patch: str, label: str) -> list[Finding]:
    return _name_findings(scanner, names.split("\0"), label) + _content_findings(
        scanner, patch, label
    )


def check_staged(repo: Path, scanner: Scanner) -> list[Finding]:
    names = git("diff", "--cached", "--name-only", "-z", "-M", cwd=repo)
    patch = git("diff", "--cached", *_DIFF, cwd=repo)
    return _change_findings(scanner, names, patch, "")


def check_message(path: Path, scanner: Scanner) -> list[Finding]:
    return scanner.scan(path.read_text(encoding="utf-8"), "commit message")


def check_history(repo: Path, scanner: Scanner) -> list[Finding]:
    found: list[Finding] = []
    # Branch and tag names are published too, and an annotated tag carries its own message.
    refs = git("for-each-ref", "--format=%(refname)%00%(objecttype)", cwd=repo).splitlines()
    for ref, kind in (line.split("\0") for line in refs):
        found += scanner.scan(ref, "a branch or tag name")
        if kind == "tag":
            tag = git("for-each-ref", "--format=%(contents)", ref, cwd=repo)
            found += scanner.scan(tag, "an annotated tag's message")
    for sha in git("rev-list", "--all", cwd=repo).split():
        label = f"{sha[:10]} "
        found += scanner.scan(git("log", "-1", "--format=%B", sha, cwd=repo), f"{label}message")
        names = git(
            "show", "--format=", "--name-only", "-z", "--diff-merges=first-parent", sha, cwd=repo
        )
        patch = git("show", "--format=", *_DIFF, "--root", sha, cwd=repo)
        found += _change_findings(scanner, names, patch, label)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--message", type=Path)
    mode.add_argument("--history", action="store_true")
    parser.add_argument("--repo", type=Path, default=None)
    args = parser.parse_args(argv)
    repo = args.repo or Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
    try:
        scanner = Scanner(load_denylist(repo), load_secrets())
    except SetupError as exc:
        print(f"leak-check: {exc}", file=sys.stderr)
        return 1
    if args.staged:
        found = check_staged(repo, scanner)
    elif args.message:
        found = check_message(args.message, scanner)
    else:
        found = check_history(repo, scanner)
    for f in dict.fromkeys(found):
        print(f"leak-check: {f.where}: {f.rule}", file=sys.stderr)
    if found:
        print(
            "leak-check: blocked. Remove the matches (see README) and try again.", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
