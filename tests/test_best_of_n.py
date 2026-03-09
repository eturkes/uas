"""Tests for Section 7: Best-of-N Code Generation with Execution Voting."""

import argparse
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

from orchestrator.main import (
    _APPROACH_HINTS,
    _get_best_of_n,
    generate_and_vote,
    main,
    score_result,
    MAX_RETRIES,
)


class TestGetBestOfN:
    """Section 7c: Budget-aware gating."""

    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv("UAS_BEST_OF_N", raising=False)
        assert _get_best_of_n(1) == 1
        assert _get_best_of_n(2) == 1
        assert _get_best_of_n(3) == 1

    def test_explicit_1_disabled(self, monkeypatch):
        monkeypatch.setenv("UAS_BEST_OF_N", "1")
        assert _get_best_of_n(1) == 1
        assert _get_best_of_n(2) == 1
        assert _get_best_of_n(3) == 1

    def test_first_attempt_always_single(self, monkeypatch):
        monkeypatch.setenv("UAS_BEST_OF_N", "3")
        assert _get_best_of_n(1) == 1

    def test_scales_with_attempt(self, monkeypatch):
        monkeypatch.setenv("UAS_BEST_OF_N", "3")
        assert _get_best_of_n(2) == 2
        assert _get_best_of_n(3) == 3

    def test_capped_by_env(self, monkeypatch):
        monkeypatch.setenv("UAS_BEST_OF_N", "2")
        assert _get_best_of_n(2) == 2
        assert _get_best_of_n(3) == 2  # capped at 2

    def test_large_max_n(self, monkeypatch):
        monkeypatch.setenv("UAS_BEST_OF_N", "5")
        assert _get_best_of_n(1) == 1
        assert _get_best_of_n(2) == 2
        assert _get_best_of_n(3) == 3


class TestScoreResult:
    """Section 7b: Execution-based selection scoring."""

    def test_success_scores_high(self):
        result = {"exit_code": 0, "stdout": "", "stderr": ""}
        assert score_result(result) >= 1000

    def test_failure_scores_low(self):
        result = {"exit_code": 1, "stdout": "", "stderr": "error"}
        assert score_result(result) < 1000

    def test_success_beats_failure(self):
        success = {"exit_code": 0, "stdout": "", "stderr": ""}
        failure = {"exit_code": 1, "stdout": "", "stderr": "error"}
        assert score_result(success) > score_result(failure)

    def test_uas_result_adds_score(self):
        plain = {"exit_code": 0, "stdout": "done", "stderr": ""}
        with_uas = {
            "exit_code": 0,
            "stdout": 'done\nUAS_RESULT: {"status": "ok", "files_written": ["a.txt"], "summary": "created a"}\n',
            "stderr": "",
        }
        assert score_result(with_uas) > score_result(plain)

    def test_more_files_scores_higher(self):
        one_file = {
            "exit_code": 0,
            "stdout": 'UAS_RESULT: {"status": "ok", "files_written": ["a.txt"], "summary": "done"}\n',
            "stderr": "",
        }
        two_files = {
            "exit_code": 0,
            "stdout": 'UAS_RESULT: {"status": "ok", "files_written": ["a.txt", "b.txt"], "summary": "done"}\n',
            "stderr": "",
        }
        assert score_result(two_files) > score_result(one_file)

    def test_summary_adds_score(self):
        no_summary = {
            "exit_code": 0,
            "stdout": 'UAS_RESULT: {"status": "ok", "files_written": []}\n',
            "stderr": "",
        }
        with_summary = {
            "exit_code": 0,
            "stdout": 'UAS_RESULT: {"status": "ok", "files_written": [], "summary": "details"}\n',
            "stderr": "",
        }
        assert score_result(with_summary) > score_result(no_summary)

    def test_longer_stdout_adds_score(self):
        short = {"exit_code": 1, "stdout": "x", "stderr": ""}
        long_out = {"exit_code": 1, "stdout": "x" * 500, "stderr": ""}
        assert score_result(long_out) > score_result(short)

    def test_stdout_bonus_capped(self):
        huge = {"exit_code": 1, "stdout": "x" * 100000, "stderr": ""}
        # Cap is 50 for stdout bonus
        assert score_result(huge) <= 50 + 1  # at most 50 from stdout

    def test_failure_with_rich_uas_still_below_success(self):
        rich_failure = {
            "exit_code": 1,
            "stdout": 'UAS_RESULT: {"status": "ok", "files_written": ["a", "b", "c"], "summary": "lots of stuff"}\n',
            "stderr": "",
        }
        bare_success = {"exit_code": 0, "stdout": "", "stderr": ""}
        assert score_result(bare_success) > score_result(rich_failure)


class TestGenerateAndVote:
    """Section 7a/7b: Parallel generation and execution voting."""

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_selects_successful_sample(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = ["resp0", "resp1"]

        mock_extract.side_effect = ["code0", "code1"]
        mock_sandbox.side_effect = [
            {"exit_code": 1, "stdout": "", "stderr": "error"},
            {"exit_code": 0, "stdout": "done", "stderr": ""},
        ]

        code, result = generate_and_vote(client, "prompt", 2)
        assert result["exit_code"] == 0
        assert code == "code1"

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_selects_richest_among_successes(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = ["resp0", "resp1"]

        mock_extract.side_effect = ["code0", "code1"]
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "ok", "stderr": ""},
            {
                "exit_code": 0,
                "stdout": 'UAS_RESULT: {"status": "ok", "files_written": ["f.txt"], "summary": "done"}\n',
                "stderr": "",
            },
        ]

        code, result = generate_and_vote(client, "prompt", 2)
        # Should pick the richer result
        assert "UAS_RESULT" in result["stdout"]

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_all_extraction_failures_returns_none(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = ["no code", "no code"]
        mock_extract.side_effect = [None, None]

        code, result = generate_and_vote(client, "prompt", 2)
        assert code is None
        assert result is None
        mock_sandbox.assert_not_called()

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_partial_extraction_failure(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = ["resp0", "resp1"]
        mock_extract.side_effect = [None, "code1"]
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}

        code, result = generate_and_vote(client, "prompt", 2)
        assert code == "code1"
        assert result["exit_code"] == 0
        # Only one sandbox call (for the successful extraction)
        assert mock_sandbox.call_count == 1

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_falls_back_to_least_bad(self, mock_extract, mock_sandbox):
        """When none succeed, selects the least-bad failure."""
        client = MagicMock()
        client.generate.side_effect = ["r0", "r1"]
        mock_extract.side_effect = ["code0", "code1"]
        mock_sandbox.side_effect = [
            {"exit_code": 1, "stdout": "", "stderr": "critical"},
            {"exit_code": 1, "stdout": "partial output " * 30, "stderr": "minor"},
        ]

        code, result = generate_and_vote(client, "prompt", 2)
        # Both failed, but second has more stdout so higher score
        assert code is not None
        assert result is not None

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_three_samples(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = ["r0", "r1", "r2"]
        mock_extract.side_effect = ["code0", "code1", "code2"]
        mock_sandbox.side_effect = [
            {"exit_code": 1, "stdout": "", "stderr": "err"},
            {"exit_code": 0, "stdout": "ok", "stderr": ""},
            {"exit_code": 1, "stdout": "", "stderr": "err"},
        ]

        code, result = generate_and_vote(client, "prompt", 3)
        assert result["exit_code"] == 0
        assert client.generate.call_count == 3
        assert mock_sandbox.call_count == 3

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_exception_in_one_sample_handled(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = [RuntimeError("LLM timeout"), "resp1"]
        mock_extract.return_value = "code1"
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}

        code, result = generate_and_vote(client, "prompt", 2)
        # First sample threw, second succeeded
        assert code == "code1"
        assert result["exit_code"] == 0

    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.extract_code")
    def test_prompt_hints_applied(self, mock_extract, mock_sandbox):
        client = MagicMock()
        client.generate.side_effect = ["r0", "r1", "r2"]
        mock_extract.side_effect = ["c0", "c1", "c2"]
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}

        generate_and_vote(client, "base_prompt", 3)

        # Verify different hints were used (order may vary due to threading)
        calls = client.generate.call_args_list
        prompts = {c.args[0] for c in calls}
        assert len(prompts) == 3
        # One should be bare base_prompt (empty hint)
        assert "base_prompt" in prompts
        # One should have robustness hint
        assert any("robustness" in p.lower() for p in prompts)
        # One should have simplicity hint
        assert any("simplicity" in p.lower() for p in prompts)


class TestApproachHints:
    def test_first_hint_is_empty(self):
        assert _APPROACH_HINTS[0] == ""

    def test_all_hints_are_strings(self):
        for hint in _APPROACH_HINTS:
            assert isinstance(hint, str)

    def test_hints_cycle_for_large_n(self):
        """Hints should cycle when N exceeds the number of unique hints."""
        hints = [_APPROACH_HINTS[i % len(_APPROACH_HINTS)] for i in range(6)]
        assert hints[0] == hints[3]
        assert hints[1] == hints[4]
        assert hints[2] == hints[5]


class TestMainLoopWithBestOfN:
    """Integration tests: main() loop with best-of-N enabled."""

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_disabled_by_default_first_attempt_success(
        self, mock_client_factory, mock_sandbox, mock_args, monkeypatch
    ):
        """With UAS_BEST_OF_N unset, behavior matches original single-sample."""
        monkeypatch.delenv("UAS_BEST_OF_N", raising=False)
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert mock_client.generate.call_count == 1
        # verify + execute = 2
        assert mock_sandbox.call_count == 2

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.generate_and_vote")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_best_of_n_on_retry(
        self, mock_client_factory, mock_sandbox, mock_vote, mock_args, monkeypatch
    ):
        """On retry with UAS_BEST_OF_N=3, best-of-N is used."""
        monkeypatch.setenv("UAS_BEST_OF_N", "3")
        mock_args.return_value = argparse.Namespace(task=["task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client

        # Verify sandbox OK, then first attempt fails, second attempt uses voting
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
            {"exit_code": 1, "stdout": "", "stderr": "error"},  # attempt 1 (single)
        ]
        mock_vote.return_value = (
            'print("fixed")',
            {"exit_code": 0, "stdout": "fixed", "stderr": ""},
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        # generate_and_vote called for attempt 2
        mock_vote.assert_called_once()

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.generate_and_vote")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_best_of_n_extraction_failure_continues(
        self, mock_client_factory, mock_sandbox, mock_vote, mock_args, monkeypatch
    ):
        """If generate_and_vote returns None, the loop continues to next attempt."""
        monkeypatch.setenv("UAS_BEST_OF_N", "2")
        mock_args.return_value = argparse.Namespace(task=["task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client

        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
            {"exit_code": 1, "stdout": "", "stderr": "err"},  # attempt 1 (single)
        ]
        # attempt 2: voting returns None (all extraction failures)
        # attempt 3: voting returns success
        mock_vote.side_effect = [
            (None, None),
            ('print("ok")', {"exit_code": 0, "stdout": "ok", "stderr": ""}),
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert mock_vote.call_count == 2

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_best_of_n_all_fail(
        self, mock_client_factory, mock_sandbox, mock_args, monkeypatch
    ):
        """When all attempts fail including best-of-N, exit code 1."""
        monkeypatch.setenv("UAS_BEST_OF_N", "2")
        monkeypatch.delenv("UAS_STEP_ID", raising=False)
        mock_args.return_value = argparse.Namespace(task=["task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client

        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
        ] + [
            {"exit_code": 1, "stdout": "", "stderr": "error"}
            for _ in range(10)  # enough for all attempts + best-of-N samples
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
