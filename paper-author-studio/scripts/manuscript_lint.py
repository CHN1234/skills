#!/usr/bin/env python3
"""Lightweight deterministic checks for Markdown or LaTeX manuscripts."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path


PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|FIXME|XXX)\b|\[(?:AUTHOR INPUT REQUIRED|待补|待确认)[^\]]*\]",
    re.IGNORECASE,
)
VERBATIM_RE = re.compile(
    r"\\begin\{(?P<env>verbatim\*?|Verbatim|lstlisting|minted|comment)\}"
    r".*?\\end\{(?P=env)\}",
    re.DOTALL,
)
INCLUDE_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")


def word_count(text: str) -> int:
    """Count whitespace-delimited words plus standalone CJK characters."""
    latin = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", text)
    cjk = re.findall(r"[\u3400-\u9fff]", text)
    return len(latin) + len(cjk)


def strip_latex_comments(text: str) -> str:
    """Remove unescaped LaTeX comments while preserving line boundaries."""
    cleaned: list[str] = []
    for line in text.splitlines(keepends=True):
        cut = len(line)
        for index, char in enumerate(line):
            if char != "%":
                continue
            slashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                slashes += 1
                cursor -= 1
            if slashes % 2 == 0:
                cut = index
                break
        suffix = "\n" if line.endswith("\n") else ""
        cleaned.append(line[:cut].rstrip("\r\n") + suffix)
    return "".join(cleaned)


def clean_latex(text: str) -> str:
    return strip_latex_comments(VERBATIM_RE.sub("", text))


def latex_word_text(text: str) -> str:
    """Approximate visible prose by removing common LaTeX markup."""
    text = clean_latex(text)
    text = re.sub(
        r"\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}"
        r".*?\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}",
        " ",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\$\$.*?\$\$|\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\]",
        " ",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\\(?:cite\w*|ref|eqref|autoref|cref|Cref|label|url|href)"
        r"(?:\[[^\]]*\])*\{[^{}]*\}(?:\{[^{}]*\})?",
        " ",
        text,
    )
    text = re.sub(r"\\(?:begin|end)\{[^{}]+\}", " ", text)
    for _ in range(4):
        updated = re.sub(
            r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r" \1 ", text
        )
        if updated == text:
            break
        text = updated
    text = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", text)
    return text.translate(str.maketrans({"{": " ", "}": " ", "~": " "}))


def extract_abstract(text: str) -> str | None:
    latex = re.search(
        r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if latex:
        return latex.group(1).strip()

    markdown = re.search(
        r"^#{1,3}\s+Abstract\s*$\n(.*?)(?=^#{1,3}\s+|\Z)",
        text,
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    return markdown.group(1).strip() if markdown else None


def latex_reference_issues(text: str) -> tuple[list[str], list[str]]:
    text = clean_latex(text)
    labels = re.findall(r"\\label\{([^}]+)\}", text)
    ref_groups = re.findall(r"\\(?:ref|eqref|autoref|cref|Cref)\{([^}]+)\}", text)
    refs = [
        label.strip()
        for group in ref_groups
        for label in group.split(",")
        if label.strip()
    ]
    duplicate_labels = sorted(
        label for label, count in Counter(labels).items() if count > 1
    )
    missing_labels = sorted(set(refs) - set(labels))
    return duplicate_labels, missing_labels


def read_manuscript(path: Path) -> tuple[str, list[str]]:
    """Read a manuscript and recursively include local LaTeX source files."""
    if path.suffix.lower() != ".tex":
        return path.read_text(encoding="utf-8"), []

    warnings: list[str] = []
    seen: set[Path] = set()

    def visit(source: Path) -> str:
        source = source.resolve()
        if source in seen:
            return ""
        seen.add(source)
        text = source.read_text(encoding="utf-8")
        parts = [text]
        for target in INCLUDE_RE.findall(clean_latex(text)):
            child = source.parent / target.strip()
            if not child.suffix:
                child = child.with_suffix(".tex")
            if child.is_file():
                parts.append(visit(child))
            else:
                warnings.append(f"未读取 LaTeX 子文件: {target}")
        return "\n".join(parts)

    return visit(path), warnings


def lint(text: str, abstract_max: int | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    is_latex = "\\begin{" in text or "\\documentclass" in text
    visible_text = clean_latex(text) if is_latex else text

    placeholders = PLACEHOLDER_RE.findall(visible_text)
    if placeholders:
        errors.append(f"发现 {len(placeholders)} 个未解决占位符")

    abstract = extract_abstract(visible_text)
    if abstract_max is not None:
        if abstract is None:
            warnings.append("未识别到 Abstract，无法检查摘要长度")
        else:
            count = word_count(latex_word_text(abstract) if is_latex else abstract)
            status = "可能超过" if count > abstract_max else "约"
            warnings.append(f"Abstract {status} {count}/{abstract_max} 词（近似值）")

    duplicate_labels, missing_labels = latex_reference_issues(text)
    if duplicate_labels:
        errors.append("重复 LaTeX label: " + ", ".join(duplicate_labels))
    if missing_labels:
        errors.append("找不到对应 label 的引用: " + ", ".join(missing_labels))

    countable = latex_word_text(text) if is_latex else text
    warnings.append(f"全文约 {word_count(countable)} 词")
    return errors, warnings


def self_test() -> None:
    md = "# Abstract\nA compact test abstract.\n\n# Introduction\nText."
    assert extract_abstract(md) == "A compact test abstract."
    assert word_count(extract_abstract(md) or "") == 4

    tex = r"""\begin{abstract}A \textbf{short} abstract \cite{ignored}.\end{abstract}
Figures~\cref{fig:a,fig:b}. \label{fig:a} \label{fig:b}
% \ref{fig:commented}
\begin{lstlisting}\ref{fig:listing}\end{lstlisting}"""
    assert latex_reference_issues(tex) == ([], [])
    abstract = extract_abstract(clean_latex(tex)) or ""
    assert word_count(latex_word_text(abstract)) == 3

    broken = r"TODO \ref{fig:missing} \label{fig:dup} \label{fig:dup}"
    errors, _ = lint(broken)
    assert len(errors) == 3

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "main.tex").write_text(r"\input{section}", encoding="utf-8")
        (root / "section.tex").write_text(r"\label{fig:child}", encoding="utf-8")
        project, warnings = read_manuscript(root / "main.tex")
        assert not warnings and r"\label{fig:child}" in project

    print("self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manuscript", nargs="?", type=Path)
    parser.add_argument("--abstract-max", type=int, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.manuscript is None:
        parser.error("manuscript is required unless --self-test is used")
    if not args.manuscript.is_file():
        print(f"ERROR: file not found: {args.manuscript}", file=sys.stderr)
        return 2

    text, read_warnings = read_manuscript(args.manuscript)
    errors, warnings = lint(text, args.abstract_max)

    for item in read_warnings + warnings:
        print(f"INFO: {item}")
    for item in errors:
        print(f"ERROR: {item}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
