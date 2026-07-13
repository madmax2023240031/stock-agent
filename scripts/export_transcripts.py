#!/usr/bin/env python3
"""Claude Code 대화 기록(JSONL)을 NotebookLM에 올릴 마크다운으로 변환한다.

사용법:
    python3 scripts/export_transcripts.py                # 이 프로젝트 대화 전부 내보내기
    python3 scripts/export_transcripts.py --out ~/내보내기
    python3 scripts/export_transcripts.py --keep-tools   # 도구 호출까지 포함
    python3 scripts/export_transcripts.py --single       # 전부 한 파일로 합치기

기본은 도구 호출·결과를 빼고 사람이 주고받은 대화만 남긴다.
NotebookLM은 소스 하나당 50만 단어 제한이 있어, 세션별 파일로 나누는 걸 권장한다.
"""

import argparse
import json
import unicodedata
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 대화 내용이 아닌 시스템/명령 잡음
NOISE_MARKERS = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
)


def project_slug(path: Path) -> str:
    """실제 경로를 Claude Code가 쓰는 디렉터리 이름 규칙으로 바꾼다.

    ASCII 영숫자만 남기고 나머지(한글·구분자 포함)는 전부 '-'로 바꾼다.
    macOS는 경로를 NFD(자모 분리)로 돌려주므로 먼저 NFC로 합쳐야
    한글 한 글자가 '-' 하나로 대응된다.
    """
    text = unicodedata.normalize("NFC", str(path))
    return "".join(c if (c.isascii() and c.isalnum()) else "-" for c in text)


def blocks_to_text(content, keep_tools: bool) -> str:
    """message.content(문자열 또는 블록 리스트)를 평문으로 편다."""
    if isinstance(content, str):
        return content

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            continue  # 사고 과정은 제외
        elif btype == "tool_use" and keep_tools:
            name = block.get("name", "?")
            args = json.dumps(block.get("input", {}), ensure_ascii=False)[:500]
            parts.append(f"`[도구 호출: {name}]` {args}")
        elif btype == "tool_result" and keep_tools:
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(
                    b.get("text", "") for b in body if isinstance(b, dict)
                )
            parts.append(f"`[도구 결과]` {str(body)[:500]}")

    return "\n\n".join(p for p in parts if p and p.strip())


def is_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(stripped.startswith(m) for m in NOISE_MARKERS)


def parse_session(jsonl: Path, keep_tools: bool):
    """JSONL 한 개를 (제목, 시각, 턴 목록)으로 읽는다."""
    turns = []
    title = None
    started = None

    with jsonl.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = rec.get("type")

            if rtype == "ai-title" and not title:
                title = rec.get("title") or rec.get("aiTitle")
                continue

            if rtype not in ("user", "assistant"):
                continue
            if rec.get("isMeta") or rec.get("isSidechain"):
                continue

            if started is None:
                started = rec.get("timestamp")

            text = blocks_to_text(rec.get("message", {}).get("content", ""), keep_tools)
            if is_noise(text):
                continue

            turns.append((rtype, text))

    return title, started, turns


def render(title, started, turns, session_id) -> str:
    when = ""
    if started:
        try:
            when = datetime.fromisoformat(started.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            when = started

    lines = [f"# {title or '제목 없는 대화'}", ""]
    lines.append(f"- 날짜: {when}")
    lines.append(f"- 세션: {session_id}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for role, text in turns:
        speaker = "## 사용자" if role == "user" else "## Claude"
        lines.append(speaker)
        lines.append("")
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--project",
        default=str(Path.cwd()),
        help="대상 프로젝트 경로 (기본: 현재 디렉터리)",
    )
    ap.add_argument("--out", default="./transcripts", help="출력 디렉터리")
    ap.add_argument(
        "--keep-tools", action="store_true", help="도구 호출·결과도 포함한다"
    )
    ap.add_argument(
        "--single", action="store_true", help="세션별이 아니라 한 파일로 합친다"
    )
    ap.add_argument(
        "--min-turns", type=int, default=2, help="이 턴 수 미만인 세션은 건너뛴다"
    )
    args = ap.parse_args()

    src = PROJECTS_DIR / project_slug(Path(args.project).expanduser().resolve())
    if not src.is_dir():
        raise SystemExit(f"대화 기록을 찾을 수 없다: {src}")

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    sessions = []
    for jsonl in sorted(src.glob("*.jsonl")):
        title, started, turns = parse_session(jsonl, args.keep_tools)
        if len(turns) < args.min_turns:
            continue
        sessions.append((jsonl.stem, title, started, turns))

    sessions.sort(key=lambda s: s[2] or "")

    if not sessions:
        raise SystemExit("내보낼 대화가 없다.")

    if args.single:
        body = "\n\n---\n\n".join(
            render(t, ts, turns, sid) for sid, t, ts, turns in sessions
        )
        target = out / "대화기록-전체.md"
        target.write_text(body, encoding="utf-8")
        print(f"{len(sessions)}개 세션 → {target}")
    else:
        for i, (sid, title, started, turns) in enumerate(sessions, 1):
            date = (started or "")[:10] or "날짜미상"
            slug = "".join(
                c if c.isalnum() or c in " -_가-힣" else "" for c in (title or "")
            ).strip()[:50]
            name = f"{i:02d}_{date}_{slug or sid[:8]}.md"
            (out / name).write_text(
                render(title, started, turns, sid), encoding="utf-8"
            )
        print(f"{len(sessions)}개 세션 → {out}/")

    total = sum(len(t) for _, _, _, t in sessions)
    print(f"총 {total}개 턴. NotebookLM에 이 파일들을 업로드하면 된다.")


if __name__ == "__main__":
    main()
