"""Tests for `cinna.kit_contract` — the Local Agent Kit contract as data.

The contract is shared with two other hosts (Cinna Desktop, cinna-core's
`kit.py`), and the thing that goes wrong when a port drifts is not an error: it
is two hosts selecting different file sets, hashing them, and reporting
"unpublished changes" forever. These tests pin the semantics that make the
selection identical.
"""

import json
import os
from pathlib import Path

import pytest

from cinna.kit_contract import (
    DEFAULT_EXCLUDE,
    check_contract_compatibility,
    DEFAULT_SECRET_FILE_RULES,
    collect_export_tree,
    find_kit_root,
    is_excluded,
    is_secret_filename,
    kit_contract_version,
    matches_pattern,
    resolve_export_contract,
    utf16_sort_key,
)

RULES = DEFAULT_SECRET_FILE_RULES


# --- the shipped list ------------------------------------------------------


def test_default_exclude_matches_the_contract_list():
    """The built-in fallback must stay content-identical to `layout.json`.

    Same entries, same order. A fallback that diverges from the contract is a
    silent hash-parity break: this host and the desktop would hash different
    file sets and the drift indicator would never clear.
    """
    assert DEFAULT_EXCLUDE == [
        ".git/",
        "**/.git/",
        ".gitignore",
        "**/.gitignore",
        ".gitattributes",
        ".gitkeep",
        "**/.gitkeep",
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "Makefile",
        "publications.json",
        ".claude/",
        ".cursor/",
        ".vscode/",
        ".idea/",
        "app-data/",
        "temp/",
        "credentials/",
        "credentials.json",
        "**/credentials.json",
        "credentials/.env",
        "**/.env",
        "**/.env.local",
        "**/*.env",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.tmp",
        "**/*.pyc",
        "__pycache__/",
        "**/__pycache__/",
        ".mypy_cache/",
        "**/.mypy_cache/",
        ".ruff_cache/",
        "**/.ruff_cache/",
        ".venv/",
        "venv/",
        "node_modules/",
        "**/.DS_Store",
        "**/Thumbs.db",
    ]


@pytest.mark.skipif(
    not os.environ.get("CINNA_CORE_LAYOUT"),
    reason="set CINNA_CORE_LAYOUT to a cinna-core layout.json to check the port live",
)
def test_default_exclude_matches_a_real_layout_json():
    """Opt-in cross-repo check against cinna-core's own `layout.json`."""
    layout = json.loads(Path(os.environ["CINNA_CORE_LAYOUT"]).read_text())
    assert DEFAULT_EXCLUDE == layout["cloud_import_excludes"]
    assert DEFAULT_SECRET_FILE_RULES == layout["secret_files"]["rules"]


# --- matching semantics ----------------------------------------------------


@pytest.mark.parametrize(
    "pattern,rel,expected",
    [
        # Anchoring: a pattern without `**` consumes the whole path from the root.
        ("README.md", "README.md", True),
        ("README.md", "docs/README.md", False),
        ("README.md", "scripts/README.md", False),
        # `**` spans zero or more whole segments.
        ("**/.DS_Store", ".DS_Store", True),
        ("**/.DS_Store", "files/nested/.DS_Store", True),
        ("**/*.pyc", "scripts/check.pyc", True),
        # A `**/`-prefixed DIRECTORY pattern cannot match at the root, which is
        # why layout.json lists directories in both forms.
        ("**/__pycache__/", "scripts/__pycache__/x.pyc", True),
        ("**/__pycache__/", "__pycache__/x.pyc", False),
        ("__pycache__/", "__pycache__/x.pyc", True),
        # A trailing `/` takes the directory and everything beneath it.
        ("credentials/", "credentials/.env", True),
        ("credentials/", "credentials", True),
        ("app-data/", "app-data/storage/STATUS.md", True),
        # `*` stays inside one segment.
        ("*.env", "a/b.env", False),
        ("**/*.env", "a/b.env", True),
    ],
)
def test_matches_pattern_follows_the_contract(pattern, rel, expected):
    assert matches_pattern(pattern, rel) is expected


def test_a_trailing_space_does_not_void_a_directory_pattern():
    """§7: the one place this port deliberately diverges from `kit.py`.

    `kit.py` picks the directory branch from the raw pattern while its
    normalisation strips only `/`, so `"temp/ "` takes the directory branch with
    the space still in the pattern body and matches nothing — a directory
    silently dropped from the exclude set, with every step looking like it
    worked. Both sides of the choice are made on the stripped pattern here.
    """
    assert matches_pattern("temp/", "temp/x.txt") is True
    assert matches_pattern("temp/ ", "temp/x.txt") is True
    assert matches_pattern(" temp/", "temp/x.txt") is True


def test_is_excluded_ors_the_patterns():
    assert is_excluded("scripts/check.pyc", DEFAULT_EXCLUDE) is True
    assert is_excluded("scripts/check.py", DEFAULT_EXCLUDE) is False


# --- the secret gate -------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        (".env", True),
        (".env.prod", True),  # the leak the contract closes
        (".env.local", True),
        (".env.staging", True),
        ("staging.env", True),
        (".env.example", False),  # correct, and must stay this way
        (".env.sample", False),
        (".env.template", False),
        ("settings.json", False),
        ("environment.md", False),
    ],
)
def test_is_secret_filename_reads_the_declared_rule(name, expected):
    assert is_secret_filename(name, RULES) is expected


def test_unknown_match_clause_counts_as_a_hit():
    """Fail-safe direction: unevaluable ⇒ treat the path as secret."""
    rules = [{"id": "future", "match": {"basename_regex": ["^x"]}}]
    assert is_secret_filename("anything.md", rules) is True


def test_unknown_unless_clause_counts_as_a_miss():
    """The other half of the same direction — an exemption we cannot read
    does not exempt."""
    rules = [
        {
            "id": "dotenv",
            "match": {"basename_prefix": [".env."]},
            "unless": {"basename_regex": [r"\.example$"]},
        }
    ]
    assert is_secret_filename(".env.example", rules) is True


def test_a_rule_this_build_cannot_open_withholds_everything():
    assert is_secret_filename("README.md", ["not-an-object"]) is True


def test_a_known_clause_with_unusable_values_is_unevaluable():
    assert is_secret_filename("README.md", [{"match": {"basename_equals": [7]}}]) is True


def test_empty_rules_mean_the_gate_is_explicitly_off():
    """The one way the gate is off is a caller asking for it."""
    assert is_secret_filename(".env", []) is False


# --- resolving the contract next to an agent -------------------------------


def _kit(root: Path, layout: dict | None = None, index: dict | None = None) -> Path:
    kit_dir = root / ".cinna-kit"
    kit_dir.mkdir(parents=True, exist_ok=True)
    if layout is not None:
        (kit_dir / "layout.json").write_text(json.dumps(layout))
    if index is not None:
        (kit_dir / "kit.json").write_text(json.dumps(index))
    return kit_dir


def test_find_kit_root_walks_up_and_accepts_either_marker(tmp_path):
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    assert find_kit_root(agent) is None
    kit_dir = _kit(tmp_path, layout={"cloud_import_excludes": ["temp/"]})
    assert find_kit_root(agent) == kit_dir


def test_layout_json_is_the_authority(tmp_path):
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    _kit(tmp_path, layout={"cloud_import_excludes": ["temp/", "notes/"]})

    contract = resolve_export_contract(agent, ("credentials/", "app-data/"))
    assert contract.hashable is True
    assert contract.patterns == ["temp/", "notes/", "credentials/", "app-data/"]
    assert contract.origin.endswith("layout.json")
    assert "DEGRADED" not in contract.origin


def test_kit_json_cloud_import_is_no_longer_read(tmp_path):
    """D6 deleted the key. Reading it back would be the second authority the
    contract exists to end."""
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    _kit(tmp_path, index={"cloud_import": {"exclude": ["only-this/"]}})

    contract = resolve_export_contract(agent, ("credentials/",))
    assert "only-this/" not in contract.patterns
    assert contract.hashable is False


def test_a_missing_contract_is_reported_as_a_degradation(tmp_path):
    """The line that started this: a missing authority announced a *mode*."""
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)

    contract = resolve_export_contract(agent, ("credentials/", "app-data/"))
    assert contract.hashable is False
    assert contract.origin.startswith("DEGRADED")
    assert contract.patterns[: len(DEFAULT_EXCLUDE)] == DEFAULT_EXCLUDE


def test_an_unusable_entry_rejects_the_whole_list(tmp_path):
    """Whole or nothing: a shortened exclude list is a narrower gate reported
    as a healthy one."""
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    _kit(tmp_path, layout={"cloud_import_excludes": ["temp/", 7]})

    contract = resolve_export_contract(agent, ())
    assert contract.hashable is False
    assert contract.patterns == DEFAULT_EXCLUDE


def test_mandatory_patterns_survive_a_contract_that_drops_them(tmp_path):
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    _kit(tmp_path, layout={"cloud_import_excludes": ["temp/"]})

    contract = resolve_export_contract(agent, ("credentials/", "app-data/"))
    assert "credentials/" in contract.patterns
    assert "app-data/" in contract.patterns


def test_stray_whitespace_is_stripped_and_said_out_loud(tmp_path, capsys):
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    _kit(tmp_path, layout={"cloud_import_excludes": ["temp/ "]})

    contract = resolve_export_contract(agent, ())
    assert contract.patterns == ["temp/"]
    assert "stray whitespace" in capsys.readouterr().out


def test_secret_rules_come_from_the_contract(tmp_path):
    agent = tmp_path / "Local" / "invoice-watcher"
    agent.mkdir(parents=True)
    _kit(
        tmp_path,
        layout={
            "cloud_import_excludes": ["temp/"],
            "secret_files": {"rules": [{"match": {"basename_suffix": [".key"]}}]},
        },
    )

    contract = resolve_export_contract(agent, ())
    assert is_secret_filename("signing.key", contract.secret_rules) is True
    assert is_secret_filename(".env.prod", contract.secret_rules) is False


# --- the walk --------------------------------------------------------------


def test_collect_export_tree_applies_both_gates(tmp_path):
    agent = tmp_path / "invoice-watcher"
    (agent / "docs").mkdir(parents=True)
    (agent / "credentials").mkdir()
    (agent / "docs" / "WORKFLOW_PROMPT.md").write_text("run it\n")
    (agent / "docs" / "README.md").write_text("docs readme\n")
    (agent / "README.md").write_text("agent readme\n")
    (agent / ".env.prod").write_text("TOKEN=leaked\n")
    (agent / ".env.example").write_text("TOKEN=\n")
    (agent / "credentials" / ".env").write_text("TOKEN=leaked\n")

    files, unreadable = collect_export_tree(agent, DEFAULT_EXCLUDE, RULES)

    assert unreadable == []
    assert "docs/WORKFLOW_PROMPT.md" in files
    assert "docs/README.md" in files  # anchoring: only the agent's own README goes
    assert "README.md" not in files
    assert ".env.prod" not in files  # the secret gate, not a glob
    assert ".env.example" in files  # the `unless` block
    assert not [f for f in files if f.startswith("credentials/")]


def test_collect_export_tree_never_follows_or_lists_symlinks(tmp_path):
    agent = tmp_path / "invoice-watcher"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("not ours\n")
    agent.mkdir()
    (agent / "keep.txt").write_text("ours\n")
    (agent / "linked").symlink_to(outside, target_is_directory=True)

    files, _ = collect_export_tree(agent, DEFAULT_EXCLUDE, RULES)
    assert files == ["keep.txt"]


def test_collect_export_tree_records_what_it_could_not_read(tmp_path):
    agent = tmp_path / "invoice-watcher"
    (agent / "scripts").mkdir(parents=True)
    (agent / "scripts" / "check.py").write_text("x\n")
    (agent / "keep.txt").write_text("ours\n")
    (agent / "scripts").chmod(0o000)
    try:
        files, unreadable = collect_export_tree(agent, DEFAULT_EXCLUDE, RULES)
    finally:
        (agent / "scripts").chmod(0o755)
    assert unreadable == ["scripts/"]
    assert "scripts/check.py" not in files


def test_an_excluded_directory_is_never_descended_into(tmp_path):
    agent = tmp_path / "invoice-watcher"
    (agent / "credentials").mkdir(parents=True)
    (agent / "credentials" / ".env").write_text("x\n")
    (agent / "credentials").chmod(0o000)
    try:
        files, unreadable = collect_export_tree(agent, DEFAULT_EXCLUDE, RULES)
    finally:
        (agent / "credentials").chmod(0o755)
    # An unreadable directory that never travels costs nothing.
    assert unreadable == []
    assert files == []


def test_files_are_sorted_in_utf16_code_unit_order():
    """The desktop sorts with `Array.prototype.sort`, and the order is part of
    the hash. Plain `sorted()` disagrees for non-BMP characters."""
    names = ["￿.md", "\U0001f600.md"]
    assert sorted(names) == ["￿.md", "\U0001f600.md"]
    assert sorted(names, key=utf16_sort_key) == ["\U0001f600.md", "￿.md"]


# --- the version gate (handover §6) ----------------------------------------


@pytest.mark.parametrize(
    "agent,tool,status",
    [
        # A same-major pair passes. This is the desktop's own pinned case, and
        # it is the constraint on what a minor contract change may do: it
        # reaches a non-adopting reader as silence, not as a warning.
        ("1.0.0", "1.4.2", "ok"),
        ("1.4.2", "1.0.0", "ok"),
        ("1.0.0", "1.0.0", "ok"),
        ("1.0.0-rc.1", "1.9.9", "ok"),
        # A folder from a newer major needs a newer tool...
        ("2.0.0", "1.0.0", "app_too_old"),
        # ...and one from an older major needs migrating. Not one symmetric
        # "mismatch": the remedies differ by direction.
        ("1.0.0", "2.0.0", "migratable"),
        # Unusable on either side is `unknown`, never a guess.
        (None, "1.0.0", "unknown"),
        ("not-a-version", "1.0.0", "unknown"),
        ("1.0", "1.0.0", "unknown"),
        ("1.0.0", None, "unknown"),
    ],
)
def test_check_contract_compatibility(agent, tool, status):
    assert check_contract_compatibility(agent, tool)[0] == status


def test_check_contract_compatibility_names_the_direction():
    _, reason = check_contract_compatibility("2.0.0", "1.0.0")
    assert "upgrade" in reason.lower()
    _, reason = check_contract_compatibility("1.0.0", "2.0.0")
    assert "migrated" in reason.lower()


def test_kit_contract_version_prefers_kit_json(tmp_path):
    kit_dir = _kit(tmp_path, index={"contract_version": "1.2.3"})
    (kit_dir / "CONTRACT_VERSION").write_text("9.9.9\n")
    assert kit_contract_version(kit_dir) == "1.2.3"


def test_kit_contract_version_falls_back_to_the_file(tmp_path):
    kit_dir = _kit(tmp_path, index={})
    (kit_dir / "CONTRACT_VERSION").write_text("1.0.0\n")
    assert kit_contract_version(kit_dir) == "1.0.0"


def test_kit_contract_version_ignores_an_unrendered_token(tmp_path):
    kit_dir = _kit(tmp_path, index={})
    (kit_dir / "CONTRACT_VERSION").write_text("{{CONTRACT_VERSION}}\n")
    assert kit_contract_version(kit_dir) is None
