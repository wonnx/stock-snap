"""Pick the articles that explain today's move, and say so plainly when none do.

Two backends, tried in this order:

1. Claude Code (`claude -p`) authenticated with a subscription OAuth token
   (`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`). This is what runs on CI.
2. The Anthropic API through the SDK (`ANTHROPIC_API_KEY`).

When neither is configured, or the call fails, the result carries no articles and a
`degraded` reason. The caller must then say the cause is not established instead of
narrating unfiltered headlines - the pipeline did exactly that for months because the
old code returned the raw list on every failure path. See VERIFICATION.md, rules 1 and 6.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 10
MAX_SELECTED = 3
CLAUDE_TIMEOUT_SECS = int(os.getenv("NEWS_FILTER_TIMEOUT", "180"))

# Article = (title, detail). Input is whatever the collectors returned, usually English;
# output is Korean written by the model.
Article = tuple[str, str]

SYSTEM_PROMPT = (
    "You are filtering news for a Korean stock video. Answer only from the articles given; "
    "do not use tools, do not browse, do not invent causes. If nothing qualifies, return an "
    "empty list. Write titles and details in Korean."
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
        }
    },
    "required": ["articles"],
}


@dataclass
class DirectionResult:
    articles: list[Article]
    backend: str  # "claude_code" | "api" | "none"
    degraded: list[str] = field(default_factory=list)

    @property
    def cause_found(self) -> bool:
        return bool(self.articles)


def build_prompt(articles: list[Article], symbol: str, change_pct: float) -> str:
    direction_ko = "급등(상승)" if change_pct > 0 else "급락(하락)"
    listing = "\n".join(
        f"{i + 1}. 제목: {t}\n   내용: {d}" if d else f"{i + 1}. 제목: {t}"
        for i, (t, d) in enumerate(articles[:MAX_CANDIDATES])
    )
    return (
        f"주식 {symbol}이 오늘 {change_pct:+.2f}% {direction_ko}했습니다.\n\n"
        f"아래는 수집된 뉴스 기사 목록입니다:\n{listing}\n\n"
        f"다음 지시를 반드시 따르세요:\n"
        f"1. {symbol} 종목과 직접 관련된 기사만 선별하세요. "
        f"유가, 금리, 환율 등 {symbol}과 무관한 거시경제 기사는 제외하세요.\n"
        f"2. 기사 내용의 방향이 오늘의 주가 {direction_ko} 방향과 일치하는지 판단하세요. "
        f"급등 종목에는 상승/호재 사유를, 급락 종목에는 하락/악재 사유를 설명하는 기사를 선별하세요.\n"
        f"3. 위 조건을 만족하는 기사 최대 {MAX_SELECTED}개를 선별하세요. "
        f"조건에 맞는 기사가 없으면 빈 배열을 반환하세요. 없는 원인을 만들어내지 마세요.\n"
        f"4. 각 기사의 제목과 핵심 내용을 한국어로 간결하게 작성하세요. "
        f"제목은 당일 주가 방향과 일치하는 톤으로 작성하세요.\n"
        f'5. 응답 형식: {{"articles": [{{"title": "제목", "detail": "한 문장 핵심 내용"}}, ...]}}'
    )


def _parse_items(items: object) -> list[Article]:
    if not isinstance(items, list):
        return []
    out: list[Article] = []
    for item in items[:MAX_SELECTED]:
        if isinstance(item, dict) and item.get("title"):
            out.append((str(item["title"]).strip(), str(item.get("detail", "")).strip()))
    return out


def _via_claude_code(prompt: str) -> list[Article]:
    """Run the prompt through `claude -p` and read the structured output.

    Runs from an empty temp directory so no project hooks, MCP servers or CLAUDE.md
    get loaded, and with every tool disabled: this is a text-in, JSON-out call.
    """
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(OUTPUT_SCHEMA),
        "--tools", "",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    with tempfile.TemporaryDirectory() as scratch:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECS,
            cwd=scratch,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}: {(result.stderr or result.stdout)[-400:]}"
        )
    payload = json.loads(result.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude reported an error: {str(payload.get('result'))[:400]}")
    structured = payload.get("structured_output") or {}
    return _parse_items(structured.get("articles"))


def _via_api(prompt: str, api_key: str) -> list[Article]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model response")
    return _parse_items(json.loads(text[start:end]).get("articles"))


def pick_backend() -> str:
    """Which backend will run, from the environment alone. No network."""
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        return "claude_code"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        # Local machine with an interactive Claude Code login.
        return "claude_code"
    return "none"


def select_direction_articles(
    articles: list[Article], symbol: str, change_pct: float
) -> DirectionResult:
    """Return the articles that explain the move, in Korean, or none with a reason."""
    if not articles:
        return DirectionResult([], "none", ["no_candidates"])

    backend = pick_backend()
    if backend == "none":
        logger.warning(
            "No LLM backend for news filtering (set CLAUDE_CODE_OAUTH_TOKEN or "
            "ANTHROPIC_API_KEY); %d headlines will not be used", len(articles)
        )
        return DirectionResult([], "none", ["no_llm_backend"])

    prompt = build_prompt(articles, symbol, change_pct)
    try:
        if backend == "claude_code":
            selected = _via_claude_code(prompt)
        else:
            selected = _via_api(prompt, os.environ["ANTHROPIC_API_KEY"])
    except FileNotFoundError:
        logger.error("claude CLI not found on PATH; install @anthropic-ai/claude-code")
        return DirectionResult([], "none", ["claude_cli_missing"])
    except subprocess.TimeoutExpired:
        logger.error("news filter timed out after %ds", CLAUDE_TIMEOUT_SECS)
        return DirectionResult([], "none", [f"{backend}_timeout"])
    except Exception as exc:
        logger.error("news filter (%s) failed: %s", backend, exc)
        return DirectionResult([], "none", [f"{backend}_failed"])

    if selected:
        logger.info("news filter (%s): %d of %d articles kept", backend, len(selected), len(articles))
    else:
        logger.info("news filter (%s): no article explains the move", backend)
    return DirectionResult(selected, backend)
