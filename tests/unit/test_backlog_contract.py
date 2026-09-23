from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

BACKLOG = Path("BACKLOG.md")
ALLOWED_DELIVERY = {"Planned", "In Progress", "Blocked", "Ready", "Merged"}
ALLOWED_GIT = {
    "not-started (branch absent)",
    "active-clean",
    "pushed-ci-failing",
    "pushed-ci-green",
    "merged",
}
ALLOWED_TYPES = "feat|fix|docs|test|refactor|perf|build|ci|chore"
HEADER_RE = re.compile(r"^## (PR-\d{2,3}): .+$", re.MULTILINE)
LEVEL2_RE = re.compile(r"^## .+$", re.MULTILINE)
BRANCH_RE = re.compile(r"^(pr-\d{2,3})/[a-z0-9]+(?:-[a-z0-9]+)*$")
COMMIT_RE = re.compile(rf"^({ALLOWED_TYPES})\((pr-\d{{2,3}})\): [a-z0-9].+$")
EXPECTED_DETAILED = [
    *[f"PR-{index:02d}" for index in range(114, 119)],
    "PR-113",
    *[f"PR-{index:02d}" for index in range(106, 111)],
]
REQUIRED_FIELDS = (
    "PR name",
    "Status",
    "Updated",
    "PR",
    "Git branch",
    "Git status",
    "Agent lane",
    "Depends on",
    "Commit",
    "Design patterns",
)


@dataclass(frozen=True, slots=True)
class BacklogPr:
    pr_id: str
    body: str


def _sections(text: str) -> list[BacklogPr]:
    matches = list(HEADER_RE.finditer(text))
    level2 = list(LEVEL2_RE.finditer(text))
    sections: list[BacklogPr] = []
    for match in matches:
        end = next(
            (heading.start() for heading in level2 if heading.start() > match.start()),
            len(text),
        )
        sections.append(BacklogPr(match.group(1), text[match.end() : end]))
    return sections


def _field(section: BacklogPr, name: str) -> str:
    matches = re.findall(rf"^{re.escape(name)}: (.+)$", section.body, re.MULTILINE)
    assert len(matches) == 1, f"{section.pr_id}: expected exactly one {name} field"
    return matches[0].strip()


def _unquote(value: str) -> str:
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


def _requirement_ids(section: BacklogPr, prefix: str) -> list[int]:
    return [
        int(value)
        for value in re.findall(
            rf"^- {prefix}(\d+)(?: \(verifies R\d+\))?:",
            section.body,
            re.MULTILINE,
        )
    ]


def _validate(text: str) -> list[BacklogPr]:
    sections = _sections(text)
    expected = EXPECTED_DETAILED
    assert [section.pr_id for section in sections] == expected
    assert text.rfind("## Closed Delivery Summary") > text.rfind(f"## {expected[-1]}:")

    for section in sections:
        values = {name: _field(section, name) for name in REQUIRED_FIELDS}
        pr_lower = section.pr_id.lower()
        pr_name = _unquote(values["PR name"])

        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", pr_name)
        assert values["Status"] in ALLOWED_DELIVERY

        git_status = _unquote(values["Git status"])
        assert git_status in ALLOWED_GIT or git_status.startswith("active-dirty: ")

        branch = _unquote(values["Git branch"])
        branch_match = BRANCH_RE.fullmatch(branch)
        assert branch_match is not None, f"{section.pr_id}: invalid Git branch"
        assert branch_match.group(1) == pr_lower
        assert branch.removeprefix(f"{pr_lower}/") == pr_name

        commit = _unquote(values["Commit"])
        commit_match = COMMIT_RE.fullmatch(commit)
        assert commit_match is not None, f"{section.pr_id}: invalid Conventional Commit"
        assert commit_match.group(2) == pr_lower

        patterns = values["Design patterns"].strip()
        assert patterns, f"{section.pr_id}: Design patterns must be non-empty"

        requirements = _requirement_ids(section, "R")
        acceptance = _requirement_ids(section, "A")
        assert requirements
        assert requirements == list(range(1, len(requirements) + 1))
        assert acceptance == requirements

        status = values["Status"]
        if status == "Merged":
            assert git_status == "merged"
            assert values["PR"].startswith("#")
        elif status == "Planned":
            assert git_status == "not-started (branch absent)"
        elif status == "Ready":
            assert git_status == "pushed-ci-green"
        elif status == "In Progress":
            assert git_status in {
                "active-clean",
                "pushed-ci-failing",
                "pushed-ci-green",
            } or git_status.startswith("active-dirty: ")

    return sections


def test_backlog_contains_only_current_detailed_program() -> None:
    sections = _validate(BACKLOG.read_text(encoding="utf-8"))
    assert [section.pr_id for section in sections] == EXPECTED_DETAILED


def _minimal_section(pr_number: int) -> str:
    pr_id = f"PR-{pr_number:02d}"
    scope = f"pr-{pr_number:02d}"
    return f"""## {pr_id}: Example

PR name: `example`
Status: Planned
Updated: 2026-09-19
PR: TBD
Git branch: `{scope}/example`
Git status: `not-started (branch absent)`
Agent lane: Agent A
Depends on: none
Commit: `feat({scope}): add example`
Design patterns: Architectural baseline only.

Description:
- R1: Example requirement.

Acceptance:
- A1 (verifies R1): Example acceptance.
"""


def test_validator_rejects_requirement_acceptance_mismatch() -> None:
    section = _sections(
        _minimal_section(80).replace(
            "- A1 (verifies R1): Example acceptance.",
            "- A2 (verifies R1): Example acceptance.",
        )
    )[0]
    assert _requirement_ids(section, "R") != _requirement_ids(section, "A")


def test_validator_rejects_missing_git_branch() -> None:
    section = _sections(_minimal_section(80).replace("Git branch: `pr-80/example`\n", ""))[0]
    with pytest.raises(AssertionError, match="Git branch"):
        _field(section, "Git branch")


def test_validator_rejects_commit_pr_mismatch() -> None:
    section = _sections(_minimal_section(80))[0]
    commit = _unquote(_field(section, "Commit")).replace("pr-80", "pr-81")
    match = COMMIT_RE.fullmatch(commit)
    assert match is not None
    assert match.group(2) != section.pr_id.lower()


def test_validator_rejects_missing_design_patterns() -> None:
    section = _sections(
        _minimal_section(80).replace(
            "Design patterns: Architectural baseline only.\n",
            "",
        )
    )[0]
    with pytest.raises(AssertionError, match="Design patterns"):
        _field(section, "Design patterns")


def test_validator_rejects_unknown_git_status() -> None:
    section = _sections(
        _minimal_section(80).replace(
            "Git status: `not-started (branch absent)`",
            "Git status: `almost-green`",
        )
    )[0]
    assert _unquote(_field(section, "Git status")) not in ALLOWED_GIT


def test_pushed_ci_failing_is_explicitly_allowed() -> None:
    assert "pushed-ci-failing" in ALLOWED_GIT
