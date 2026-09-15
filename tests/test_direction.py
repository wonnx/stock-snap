"""News direction filter: backend selection, the claude -p path, and honest failure.

None of this touches the network or the real CLI. The one thing these tests must
guarantee is that no failure path ever hands unfiltered headlines back to the caller -
that silent fallback is how English, direction-mismatched headlines were narrated as
"the cause" for months.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stock_snap.news import direction  # noqa: E402

CANDIDATES = [
    ("Intel jumps on foundry customer report", "A large chipmaker signed on."),
    ("Oil slides as OPEC+ signals more output", "Crude fell 3%."),
    ("Intel to cut 15% of staff", "Announced last quarter."),
]


def _completed(stdout: str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude"], returncode=code, stdout=stdout, stderr=stderr)


def _claude_payload(articles: list[dict], is_error: bool = False) -> str:
    return json.dumps({"structured_output": {"articles": articles}, "is_error": is_error})


@pytest.fixture
def no_backends(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(direction.shutil, "which", lambda _name: None)


@pytest.fixture
def claude_backend(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


class TestBackendSelection:
    def test_token_wins(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert direction.pick_backend() == "claude_code"

    def test_api_key_when_no_token(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert direction.pick_backend() == "api"

    def test_local_cli_login(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(direction.shutil, "which", lambda _n: "/usr/local/bin/claude")
        assert direction.pick_backend() == "claude_code"

    def test_nothing(self, no_backends):
        assert direction.pick_backend() == "none"


class TestClaudeCodeBackend:
    def test_parses_structured_output(self, claude_backend):
        payload = _claude_payload(
            [
                {"title": "인텔, 파운드리 고객 확보 보도에 급등", "detail": "대형 고객 계약 보도."},
                {"title": "증권사 목표주가 상향", "detail": "두 곳이 상향."},
            ]
        )
        with patch.object(direction.subprocess, "run", return_value=_completed(payload)) as run:
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)

        assert result.backend == "claude_code"
        assert result.cause_found
        assert result.degraded == []
        assert [t for t, _ in result.articles] == [
            "인텔, 파운드리 고객 확보 보도에 급등",
            "증권사 목표주가 상향",
        ]

        cmd = run.call_args.args[0]
        assert cmd[:2] == ["claude", "-p"]
        assert "--json-schema" in cmd
        # Text in, JSON out: no tools, no permission prompts, no repo context.
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "--permission-prompts" in cmd
        assert "--bare" not in cmd, "bare mode ignores the OAuth token"
        assert run.call_args.kwargs["cwd"] != os.getcwd()
        assert "INTC" in run.call_args.kwargs["input"]

    def test_kept_titles_are_logged(self, claude_backend, caplog):
        """The log must name what was selected: the published video cannot be audited otherwise."""
        payload = _claude_payload([{"title": "인텔 파운드리 고객 확보", "detail": "보도."}])
        with patch.object(direction.subprocess, "run", return_value=_completed(payload)):
            with caplog.at_level("INFO"):
                direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert "kept: 인텔 파운드리 고객 확보" in caplog.text

    def test_empty_selection_is_a_valid_verdict(self, claude_backend):
        with patch.object(direction.subprocess, "run", return_value=_completed(_claude_payload([]))):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.backend == "claude_code"
        assert result.articles == []
        assert result.degraded == [], "no article qualifying is not a degradation"

    def test_caps_selection(self, claude_backend):
        many = [{"title": f"기사 {i}", "detail": "내용"} for i in range(6)]
        with patch.object(direction.subprocess, "run", return_value=_completed(_claude_payload(many))):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert len(result.articles) == direction.MAX_SELECTED


class TestFailureNeverLeaksRawHeadlines:
    """Every failure must come back empty and flagged - never the input list."""

    def test_non_zero_exit(self, claude_backend):
        with patch.object(direction.subprocess, "run", return_value=_completed("", 1, "boom")):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.articles == []
        assert result.degraded == ["claude_code_failed"]

    def test_error_payload(self, claude_backend):
        payload = json.dumps({"is_error": True, "result": "Invalid API key"})
        with patch.object(direction.subprocess, "run", return_value=_completed(payload)):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.articles == []
        assert result.degraded == ["claude_code_failed"]

    def test_garbage_stdout(self, claude_backend):
        with patch.object(direction.subprocess, "run", return_value=_completed("not json")):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.articles == []
        assert result.degraded == ["claude_code_failed"]

    def test_cli_missing(self, claude_backend):
        with patch.object(direction.subprocess, "run", side_effect=FileNotFoundError("claude")):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.articles == []
        assert result.degraded == ["claude_cli_missing"]

    def test_timeout(self, claude_backend):
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=1)
        with patch.object(direction.subprocess, "run", side_effect=exc):
            result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.articles == []
        assert result.degraded == ["claude_code_timeout"]

    def test_no_backend_configured(self, no_backends):
        result = direction.select_direction_articles(CANDIDATES, "INTC", 8.96)
        assert result.backend == "none"
        assert result.articles == []
        assert result.degraded == ["no_llm_backend"]

    def test_no_candidates_skips_the_call(self, claude_backend):
        with patch.object(direction.subprocess, "run") as run:
            result = direction.select_direction_articles([], "INTC", 8.96)
        run.assert_not_called()
        assert result.articles == []
        assert result.degraded == ["no_candidates"]


def test_prompt_names_symbol_direction_and_cap():
    prompt = direction.build_prompt(CANDIDATES, "INTC", -4.2)
    assert "INTC" in prompt
    assert "급락" in prompt
    assert f"최대 {direction.MAX_SELECTED}개" in prompt
    assert "빈 배열" in prompt
