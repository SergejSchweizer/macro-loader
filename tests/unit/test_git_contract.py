from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from scripts import validate_git_contract as contract

SYNTHETIC_MERGE = " ".join(
    [
        "Merge",
        "e1d6923e430497b98c02b899d186a3f0cb2641f0",
        "into",
        "b507f00042a6be7cea802d42c65d1cf62d083d4a",
    ]
)


def test_branch_pr_id_accepts_canonical_branch() -> None:
    assert contract.branch_pr_id("pr-06/cboe-volatility-provider") == "pr-06"
    assert contract.branch_pr_id("pr-100/fed-policy-point-in-time-references") == "pr-100"


@pytest.mark.parametrize(
    "branch",
    ["main", "pr-6/bad", "PR-06/bad", "pr-06/Bad_Name", "pr-06/"],
)
def test_branch_pr_id_rejects_invalid_branch(branch: str) -> None:
    with pytest.raises(ValueError):
        contract.branch_pr_id(branch)


def test_validate_subject_accepts_matching_conventional_commit() -> None:
    contract.validate_subject("feat(pr-06): ingest cboe volatility indices", "pr-06")
    contract.validate_subject("feat(pr-100): persist reference inputs", "pr-100")


@pytest.mark.parametrize(
    "subject",
    [
        "feat: missing scope",
        "feature(pr-06): invalid type",
        "feat(PR-06): uppercase scope",
        "feat(pr-06): Uppercase description",
        "feat(pr-6): short scope",
    ],
)
def test_validate_subject_rejects_malformed_subject(subject: str) -> None:
    with pytest.raises(ValueError):
        contract.validate_subject(subject, "pr-06")


def test_validate_subject_rejects_wrong_pr_scope() -> None:
    with pytest.raises(ValueError, match="does not match"):
        contract.validate_subject("feat(pr-07): ingest vstoxx history", "pr-06")


def test_validate_contract_validates_all_subjects() -> None:
    contract.validate_contract(
        "pr-01/repository-bootstrap-quality-gates",
        [
            "chore(pr-01): bootstrap python project",
            "ci(pr-01): add parallel quality gates",
        ],
    )


def test_validate_contract_skips_synthetic_pr_merge_subject() -> None:
    contract.validate_contract(
        "pr-01/repository-bootstrap-quality-gates",
        [SYNTHETIC_MERGE, "fix(pr-01): ignore synthetic pull request merge commits"],
        event="pull_request",
    )


def test_validate_contract_does_not_skip_arbitrary_merge_subject_locally() -> None:
    with pytest.raises(ValueError):
        contract.validate_contract(
            "pr-01/repository-bootstrap-quality-gates",
            [SYNTHETIC_MERGE],
            event="local",
        )


def test_validate_contract_skips_main_and_merge_group() -> None:
    contract.validate_contract("main", [], event="push")
    contract.validate_contract("merge-queue/not-a-pr", [], event="merge_group")


def test_validate_contract_rejects_empty_subjects() -> None:
    with pytest.raises(ValueError, match="no implementation commits"):
        contract.validate_contract("pr-01/bootstrap", [])


def test_git_subjects_reads_non_empty_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=[], returncode=0, stdout="feat(pr-01): add thing\n\n", stderr=""
        )

    monkeypatch.setattr(contract.subprocess, "run", fake_run)
    assert contract.git_subjects("origin/main") == ["feat(pr-01): add thing"]


def test_main_skips_merge_group() -> None:
    assert contract.main(["--branch", "queue", "--event", "merge_group"]) == 0


def test_main_validates_git_subjects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        contract,
        "git_subjects",
        lambda base: ["chore(pr-01): add repository bootstrap"],
    )
    assert (
        contract.main(
            [
                "--branch",
                "pr-01/repository-bootstrap-quality-gates",
                "--base",
                "origin/main",
            ]
        )
        == 0
    )
