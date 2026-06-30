#!/usr/bin/env python3
"""
Documentation reference consistency checker (cinna-cli).

Scans markdown under docs/ plus .claude/commands/ and verifies that file-path
references to src/, tests/, scripts/, and docs/ actually exist in the project.
Adapted from cinna-core's checker for the cinna-cli layout (no backend/frontend
split — this is a Python CLI: source lives in src/cinna/, tests in tests/).

Usage:
    python3 scripts/check_docs_references.py [--verbose]
    python3 scripts/check_docs_references.py --files docs/features/foo/foo.md ...

Options:
    --verbose, -v     Show each broken reference as it's found
    --files FILE...   Check only specific files (paths relative to project root)

Suppressing false positives:
    1. Pattern-based (automatic) — segments that look like placeholders are
       skipped: starts with 'your_' / '$' / '<' ; contains '...', '[', ']',
       '{', '}', '*'.
    2. Inline annotation — add <!-- nocheck --> anywhere on a line to skip all
       reference checks on that line (an optional reason may follow, e.g.
       <!-- nocheck: cross-repo path -->). Use it for *example/illustrative* paths
       (container paths like /app/workspace/..., home paths like ~/.cinna/...,
       or convention placeholders) that are not real repo files.

Exit codes:
    0 - all references valid
    1 - broken references found
"""

import os
import re
import sys

# Reference prefixes that denote a real repo path (cinna-cli layout).
REFERENCE_PREFIXES = ("src/", "tests/", "scripts/", "docs/")

# Segments that are clearly template/placeholder names, not real files.
_PLACEHOLDER_SEGMENT_RE = re.compile(r"^your_|^\$|^<", re.IGNORECASE)


def find_project_root():
    """Project root = nearest ancestor holding pyproject.toml (fallback: docs/)."""
    path = os.path.abspath(os.getcwd())
    for _ in range(12):
        if os.path.isfile(os.path.join(path, "pyproject.toml")) or os.path.isdir(
            os.path.join(path, "docs")
        ):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(script_dir, ".."))


def find_markdown_files(root_dir, exclude_dirs=None):
    exclude_dirs = set(exclude_dirs or [])
    md_files = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in exclude_dirs]
        for f in files:
            if f.endswith(".md"):
                md_files.append(os.path.join(root, f))
    md_files.sort()
    return md_files


def is_skippable_path(path):
    """True for paths that are placeholders / patterns, not real file refs."""
    if any(ch in path for ch in ("...", "…", "[", "]", "{", "}", "*")):
        return True
    for segment in path.replace("\\", "/").split("/"):
        stem = segment.rsplit(".", 1)[0] if "." in segment else segment
        if _PLACEHOLDER_SEGMENT_RE.match(stem):
            return True
    return False


def extract_backtick_references(line):
    """Extract `src/...`-style refs from backtick spans, stripping :line / :method()."""
    refs = []
    for match in re.finditer(r"`([^`\n]+)`", line):
        content = match.group(1)
        if not any(content.startswith(p) for p in REFERENCE_PREFIXES):
            continue
        # Repo paths never contain ':', so everything after the first colon is a
        # locator (line number, range, or :symbol / :method()) — drop it.
        path = content.split(":", 1)[0]
        if is_skippable_path(path):
            continue
        if path:
            refs.append(path)
    return refs


def extract_markdown_link_references(line, source_file, project_root):
    """Extract refs from [text](path) links, resolving relative ones under docs/."""
    refs = []
    for match in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", line):
        target = match.group(2).split("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if is_skippable_path(target):
            continue
        if any(target.startswith(p) for p in REFERENCE_PREFIXES):
            refs.append(target)
        else:
            source_dir = os.path.dirname(source_file)
            rel_from_root = os.path.relpath(source_dir, project_root)
            resolved = os.path.normpath(os.path.join(rel_from_root, target))
            if not resolved.startswith(".."):
                refs.append(resolved)
    return refs


def extract_references(line, source_file, project_root):
    return extract_backtick_references(line) + extract_markdown_link_references(
        line, source_file, project_root
    )


def scan_file(filepath, project_root, verbose=False):
    issues = []
    rel_path = os.path.relpath(filepath, project_root)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (IOError, UnicodeDecodeError) as e:
        return [{"file": rel_path, "line": 0, "reference": "", "error": str(e)}]

    for line_num, line in enumerate(lines, start=1):
        # Skip lines opted out with `<!-- nocheck -->` (an optional reason may
        # follow, e.g. `<!-- nocheck: cross-repo path -->`).
        if "<!-- nocheck" in line:
            continue
        for ref in extract_references(line, filepath, project_root):
            if not os.path.exists(os.path.join(project_root, ref)):
                issues.append(
                    {
                        "file": rel_path,
                        "line": line_num,
                        "reference": ref,
                        "error": "File or directory not found",
                    }
                )
                if verbose:
                    print("  BROKEN: %s:%d -> %s" % (rel_path, line_num, ref))
    return issues


def parse_args(argv):
    verbose = False
    specific_files = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("--verbose", "-v"):
            verbose = True
        elif arg == "--files":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                specific_files.append(argv[i])
                i += 1
            continue
        i += 1
    return verbose, specific_files


def main():
    verbose, specific_files = parse_args(sys.argv)
    project_root = find_project_root()

    if specific_files:
        md_files = []
        for f in specific_files:
            full = os.path.join(project_root, f)
            if os.path.isfile(full) and f.endswith(".md"):
                md_files.append(full)
            else:
                print("WARNING: Skipping %s (not found or not .md)" % f)
        if not md_files:
            print("No valid markdown files provided.")
            sys.exit(0)
    else:
        docs_dir = os.path.join(project_root, "docs")
        if not os.path.isdir(docs_dir):
            print("ERROR: docs/ not found at %s" % docs_dir)
            sys.exit(1)
        md_files = find_markdown_files(docs_dir)
        commands_dir = os.path.join(project_root, ".claude", "commands")
        if os.path.isdir(commands_dir):
            md_files.extend(find_markdown_files(commands_dir))
        if not md_files:
            print("No markdown files found")
            sys.exit(0)

    print("Scanning %d markdown files ..." % len(md_files))
    if verbose:
        print("Project root: %s\n" % project_root)

    all_issues = []
    files_with_issues = set()
    for filepath in md_files:
        issues = scan_file(filepath, project_root, verbose=verbose)
        if issues:
            all_issues.extend(issues)
            files_with_issues.add(os.path.relpath(filepath, project_root))

    print()
    if not all_issues:
        print("OK: All references are valid across %d files." % len(md_files))
        sys.exit(0)

    print("=" * 70)
    print(
        "BROKEN REFERENCES FOUND: %d issues in %d files"
        % (len(all_issues), len(files_with_issues))
    )
    print("=" * 70 + "\n")
    by_file = {}
    for issue in all_issues:
        by_file.setdefault(issue["file"], []).append(issue)
    for filepath in sorted(by_file):
        print("%s (%d broken):" % (filepath, len(by_file[filepath])))
        for issue in by_file[filepath]:
            if issue["reference"]:
                print(
                    "  Line %d: %s - %s"
                    % (issue["line"], issue["reference"], issue["error"])
                )
            else:
                print("  %s" % issue["error"])
        print()
    sys.exit(1)


if __name__ == "__main__":
    main()
