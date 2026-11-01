"""The security scan of skill bundles: rules that need no model and no workspace.

It reads what would be published, the bundle's files without `evals/`, and grades each finding:
- `critical` blocks the pull request and the publish;
- `high` blocks the pull request unless a reviewer accepts it with the override label;
- `medium` and `low` are reported for the reviewer, and never block.

Every file is read as text, whatever its first bytes say, so a script behind an image header or in another encoding
meets the same rules. Phrases are matched per paragraph, after NFKC normalisation and without Markdown emphasis, so a
hard line wrap or fullwidth letters do not hide them. Findings go to job summaries and pull request comments, so no
message quotes a credential: each is masked to its first four characters.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import io
import re
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from skillhub.bundle import SkillBundle

SEVERITIES = ("critical", "high", "medium", "low")
BLOCKING = ("critical", "high")
MAX_PER_RULE = 20  # findings of one rule in one file; the rest are counted in one more finding
MAX_UNZIPPED = 8 * 1024 * 1024  # the text read from an Office file's parts, against zip bombs


@dataclass(frozen=True)
class Finding:
    skill: str  # <dept>/<skill>
    path: str  # the file, relative to the skill folder; <file>!<part> for a part of an Office file
    line: int  # 1-based; 0 for the whole file
    rule: str
    severity: str
    message: str
    source: str = "rules"  # rules, or llm for the LLM reviewer

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.skill}/{where}: {self.severity}: {self.rule}: {self.message}"

    def to_dict(self) -> dict:
        return asdict(self)


# A known credential format. Each is specific enough that an ordinary word does not match.
_SECRETS = [
    ("an AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("a Databricks token", re.compile(r"\bdapi[0-9a-f]{32}(?:-\d+)?\b")),
    ("a Databricks OAuth secret", re.compile(r"\bdose[0-9a-f]{32,}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("an OpenAI API key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{40,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

# Invisible characters: every format character (Unicode category Cf: zero-width characters, bidirectional controls,
# tag characters, the soft hyphen), plus the combining grapheme joiner, the Hangul fillers and the variation
# selectors of the supplement, which hide data in emoji. A byte order mark at the start of a file is allowed, and so
# is a zero-width joiner inside an emoji. The soft hyphen is high, not critical: word processors leave it in text.
# Code points, never literal characters, so this file holds none of them.
_INVISIBLE_EXTRA = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0})
_SOFT_HYPHEN, _ZWJ, _BOM, _VS16 = 0x00AD, 0x200D, 0xFEFF, 0xFE0F
_BOM_CHAR = chr(_BOM)

_PIPE_TARGET = r"(?:sudo(?:\s+-\S+)*\s+)?(?:env(?:\s+\S+=\S*)*\s+)?(?:/\S*/)?(?:(?:ba|z|k|da|fi)?sh|python[0-9.]*|perl|ruby|node)\b"
_FETCH = r"(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b"
_REMOTE_EXEC = [
    re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*" + _PIPE_TARGET),
    re.compile(r"\b(?:(?:ba|z|k|da)?sh\s+-c|eval|source|exec)\s+[\"']?\$\(\s*" + _FETCH),
    re.compile(r"(?:\b(?:ba|z|k|da)?sh|\bsource|(?:^|\s)\.)\s+<\(\s*" + _FETCH, re.M),
    re.compile(r"\b(?:iex|Invoke-Expression)\b[^\n]*" + r"\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod|DownloadString)\b", re.I),
    re.compile(r"\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(?:iex|Invoke-Expression)\b", re.I),
    re.compile(r"\bexec\s*\(\s*(?:requests\.get|urllib\.request\.urlopen|urlopen)\s*\("),
]  # fmt: skip

# Phrases matched on a paragraph's normalised text. An override phrase in a sentence that is itself negated ("Never
# post API keys in the chat", "Do not change a cell without telling the user") is a guardrail, not an attack.
_OVERRIDE = re.compile(
    "|".join([
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|every\s+)?(?:of\s+)?(?:the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|original)\s+(?:instructions?|prompts?|messages|rules|guidelines)",
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:instructions|rules)\s+(?:above|before\s+this)",
        r"\bwithout\s+(?:telling|informing|notifying|alerting)\s+the\s+user",
        r"\b(?:reveal|print|output|show|repeat|leak)\s+(?:your\s+|the\s+)?(?:system\s+prompt|hidden\s+instructions)",
        r"(?:^|[.!?]\s+|:\s*)exfiltrate\b",
        r"\b(?:send|post|upload|forward|transmit)\s+(?:all\s+|the\s+|any\s+)?(?:user'?s?\s+)?"
        r"(?:conversation|chat\s+history|credentials|secrets|passwords|api\s+keys?|access\s+tokens?|environment\s+variables)",
        r"\byou\s+are\s+now\s+(?:in\s+)?(?:an?\s+)?(?:unrestricted|jailbroken|developer\s+mode|dan)\b",
    ]),
    re.I,
)  # fmt: skip
# Hiding what the agent did. These are negations themselves, so a negation does not excuse them.
_HIDING = re.compile(
    "|".join([
        r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|notify|alert)\s+the\s+user\s+(?:about\s+(?:this|it|that)\b|"
        r"that\s+you\b|what\s+you\b|you\b)",
        r"\b(?:do\s+not|don't|never|no\s+need\s+to)\s+(?:mention|tell|say|disclose|report)\s+"
        r"(?:it|this|that|these\s+steps|what\s+you\s+did)\b[^.\n]{0,30}\b(?:user|answer|response|reply)\b",
    ]),
    re.I,
)  # fmt: skip
_NEGATION = re.compile(r"\b(?:never|not|no|cannot|can't|don't|doesn't|mustn't|shouldn't|won't)\b|n't\b", re.I)
_EMPHASIS = re.compile(r"[*_~`]+")

# Sending what the agent holds to a URL, counted only when the URL's host is not in scan.allowed_domains.
_EXFIL = re.compile(
    r"\b(?:send|post|upload|forward|transmit|submit|copy)\b[^.\n]{0,120}?"
    r"\b(?:conversation|chat|transcript|credentials?|secrets?|passwords?|api\s+keys?|tokens?|holdings|portfolio|"
    r"personal\s+data|customer\s+data|files?|workbook|documents?)\b[^\n]{0,160}?(https?://[^\s<>()\[\]{}\"'`|\\]+)",
    re.I,
)

_B64 = "A-Za-z0-9+/_-"
_BLOB = re.compile(rf"[{_B64}]{{200,}}={{0,2}}|\b[0-9a-fA-F]{{200,}}\b")
_WRAPPED = re.compile(
    rf"(?:^[ \t>]*[{_B64}]{{40,}}={{0,2}}[ \t]*$\n?){{4,}}", re.M
)  # base64 wrapped at 64 or 76
_DATA_URI = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,\s*$", re.I)

_RISKY = [
    ("eval or exec of a string", re.compile(r"(?<![\w.])(?:eval|exec)\s*\(")),
    ("a shell command with shell=True", re.compile(r"\bshell\s*=\s*True\b")),
    ("os.system or os.popen", re.compile(r"\bos\.(?:system|popen)\s*\(")),
    ("unpickling data", re.compile(r"\b(?:pickle|cPickle|dill)\.loads?\s*\(")),
    ("a recursive delete of a home or root folder",
     re.compile(r"\brm\s+(?:-\w+\s+)*-\w*[rR]\w*\s+(?:-\w+\s+)*[\"']?(?:/|~|\$HOME|\$\{HOME\})/?\*?[\"']?(?=[\s;&|)]|$)", re.M)),
    ("a write to a shell profile, SSH keys or crontab",
     re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*[\"']?(?:~|\$HOME|\$\{HOME\})?/?[\w./-]*"
                r"(?:\.bashrc|\.zshrc|\.profile|\.bash_profile|\.zprofile|authorized_keys)\b|\bcrontab\s+-")),
    ("a Node child process", re.compile(r"\brequire\(\s*['\"]child_process['\"]\s*\)|\bfrom\s+['\"]child_process['\"]")),
]  # fmt: skip

_NETWORK = re.compile(
    r"\b(?:requests|httpx)\.(?:get|post|put|patch|delete|request)\s*\(|\burllib\.request\b|\burlopen\s*\("
    r"|(?<![\w.])fetch\s*\(|\bcurl\b|\bwget\b|\bsocket\.socket\s*\("
)
_URL = re.compile(r"\bhttps?://[^\s<>()\[\]{}\"'`|\\]+", re.I)
_EXAMPLE_HOSTS = ("example.com", "example.org", "example.net", "localhost")
_EXAMPLE_SUFFIXES = (".example", ".invalid", ".test", ".localhost")

SCRIPT_SUFFIXES = frozenset(
    {".py", ".pyw", ".ipynb", ".sh", ".bash", ".zsh", ".ksh", ".csh", ".tcsh", ".fish", ".command", ".js", ".mjs",
     ".cjs", ".jsx", ".ts", ".tsx", ".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".vba", ".bas", ".wsf", ".hta", ".rb",
     ".pl", ".php", ".lua", ".r", ".jl", ".sql", ".awk", ".tcl", ".groovy", ".scala", ".kts", ".swift",
     ".applescript", ".scpt"}
)  # fmt: skip
SCRIPT_NAMES = frozenset({"makefile", "gnumakefile", "dockerfile", "justfile", "rakefile"})
SCRIPT_DIRS = frozenset({"scripts", "bin"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
OFFICE_SUFFIXES = frozenset({".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm", ".potx", ".dotx", ".xltx"})
# ELF, and Mach-O in both byte orders and as a universal binary. Windows programs are checked in _executable.
_EXECUTABLE_MAGIC = (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                     b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")  # fmt: skip
_IMAGE_MAGIC = {"png": (b"\x89PNG\r\n\x1a\n",), "jpeg": (b"\xff\xd8\xff",), "gif": (b"GIF87a", b"GIF89a")}


def mask_secrets(text: str) -> str:
    """The text with anything that looks like a credential cut to its first four characters."""
    for _, pattern in _SECRETS:
        text = pattern.sub(lambda m: m.group(0)[:4] + "...", text)
    return text


def _executable(data: bytes) -> bool:
    if data[:2] == b"MZ":  # a DOS header; a Windows program also has the PE signature
        return b"PE\x00\x00" in data[:1024]
    return data.startswith(_EXECUTABLE_MAGIC)


def _image_kind(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((kind for kind, magics in _IMAGE_MAGIC.items() if data.startswith(magics)), None)


def _is_script(path: str, text: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.suffix.lower() in SCRIPT_SUFFIXES
        or p.name.lower() in SCRIPT_NAMES
        or any(part in SCRIPT_DIRS for part in p.parts[:-1])
        or text.startswith("#!")
    )


def _allowed(host: str, allowed: Iterable[str]) -> bool:
    """A host in the allowlist, or a subdomain of one. Documentation hosts (RFC 2606) are always allowed."""
    host = host.lower().rstrip(".")
    if host.endswith(_EXAMPLE_SUFFIXES):
        return True
    return any(host == d or host.endswith("." + d) for d in (*_EXAMPLE_HOSTS, *allowed))


def _host(url: str) -> str | None:
    """The URL's host, "" when it has none, None when it does not parse (a lookalike character, a broken bracket)."""
    try:
        return urlsplit(url.rstrip(".,;:")).hostname or ""
    except ValueError:
        return None


def _code_point(cp: int) -> str:
    name = unicodedata.name(chr(cp), "")
    return f"U+{cp:04X}" + (f" {name}" if name else "")


def _emoji(cp: int) -> bool:
    return 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF or cp == _VS16


def _invisible(text: str) -> Iterator[tuple[int, int]]:
    """(offset, code point) of each invisible character the rule reports."""
    for i, ch in enumerate(text):
        cp = ord(ch)
        if not (unicodedata.category(ch) == "Cf" or cp in _INVISIBLE_EXTRA or 0xE0100 <= cp <= 0xE01EF):
            continue
        if cp == _ZWJ and 0 < i < len(text) - 1 and _emoji(ord(text[i - 1])) and _emoji(ord(text[i + 1])):
            continue
        yield i, cp


def _paragraphs(text: str) -> Iterator[tuple[str, Callable[[int], int]]]:
    """Each paragraph's normalised text (its lines NFKC-normalised, without Markdown emphasis, joined by spaces), with
    the line of an offset in it."""

    def norm(line: str) -> str:
        return _EMPHASIS.sub("", unicodedata.normalize("NFKC", line.strip()))

    def paragraph(numbers: list[int], parts: list[str]) -> tuple[str, Callable[[int], int]]:
        starts, offset = [], 0
        for part in parts:
            starts.append(offset)
            offset += len(part) + 1
        return " ".join(parts), lambda at: numbers[bisect.bisect_right(starts, at) - 1]

    numbers: list[int] = []
    parts: list[str] = []
    for n, line in enumerate(text.split("\n"), 1):
        if line.strip():
            numbers.append(n)
            parts.append(norm(line))
        elif parts:
            yield paragraph(numbers, parts)
            numbers, parts = [], []
    if parts:
        yield paragraph(numbers, parts)


def _negated(paragraph: str, start: int) -> bool:
    """True when the sentence of a match is negated before it, which makes the phrase a guardrail."""
    sentence = max(
        paragraph.rfind(". ", 0, start), paragraph.rfind("! ", 0, start), paragraph.rfind("? ", 0, start)
    )
    return bool(_NEGATION.search(paragraph, sentence + 1, start))


def _diverse(run: str) -> bool:
    return len(set(run)) >= 12  # a rule line of '=' or 'a' is not an encoded payload


def _data_uri_finding(kind: str, blob: str) -> tuple[str, str]:
    """The severity and message of an embedded data URI, from what its first bytes are."""
    sub = kind.lower().split("/")[-1].replace("jpg", "jpeg")
    if "svg" in sub:
        return "high", "an embedded SVG, which can carry script and hidden text"
    head = blob[:64].replace("-", "+").replace("_", "/")
    try:
        found = _image_kind(base64.b64decode(head + "=" * (-len(head) % 4))[:16])
    except (binascii.Error, ValueError):
        found = None
    if found == sub:
        return "medium", f"an embedded {sub} image of {len(blob)} characters"
    return "high", f"an embedded file declared as {kind} that is not one ({len(blob)} characters)"


def _decode(path: str, data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The file as text, and what reading it as text found. A file that is not UTF-8 is still read, so a script
    behind an image header or in another encoding meets every rule."""
    notes: list[tuple[str, str]] = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
            notes.append(
                ("high", "UTF-16 text, which GitHub shows as binary, so reviewers cannot read it there")
            )
            return text.removeprefix(_BOM_CHAR), notes
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8").removeprefix(_BOM_CHAR), notes
    except UnicodeDecodeError:
        pass
    kind = _image_kind(data)
    if kind and PurePosixPath(path).suffix.lower() in IMAGE_SUFFIXES:
        notes.append(("medium", f"a {kind} image; check that it shows what the skill says"))
    elif kind:
        notes.append(
            ("high", f"a {kind} image named {PurePosixPath(path).name}, which is not an image file name")
        )
    else:
        notes.append(("high", "a file that is not UTF-8 text, so it cannot be reviewed as text"))
    return data.decode("utf-8", "replace"), notes


def _office_parts(data: bytes) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]]]:
    """The XML parts of an Office file, to scan as text, and what its package says: macros are high."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if any(i.filename.lower().endswith("vbaproject.bin") for i in infos):
                notes = [("high", "an Office file with macros (vbaProject.bin)")]
            else:
                notes = [("medium", "an Office file; its text is scanned, check its content")]
            parts, total = [], 0
            for info in infos:
                if not info.filename.lower().endswith((".xml", ".rels")):
                    continue
                total += info.file_size
                if total > MAX_UNZIPPED:
                    notes.append(("high", "an Office file too large to read in full"))
                    break
                parts.append((info.filename, z.read(info)))
            return parts, notes
    except (zipfile.BadZipFile, OSError, ValueError):
        return [], [("high", "an Office file that is not a valid package")]


def scan_file(skill: str, path: str, data: bytes, *, allowed_domains: Iterable[str] = ()) -> list[Finding]:
    """The findings of one file of a bundle."""
    out: list[Finding] = []
    per_rule: dict[tuple[str, str], int] = {}
    over: dict[tuple[str, str], str] = {}

    def add(at: str, line: int, rule: str, severity: str, message: str) -> None:
        key = (at, rule)
        per_rule[key] = per_rule.get(key, 0) + 1
        if per_rule[key] > MAX_PER_RULE:
            over[key] = min(over.get(key, severity), severity, key=SEVERITIES.index)
            return
        out.append(Finding(skill, at, line, rule, severity, mask_secrets(message)))

    if _executable(data):
        add(path, 0, "binary", "critical", "an executable file; a skill must not ship programs")
        return out
    if data[:4] == b"PK\x03\x04":
        if PurePosixPath(path).suffix.lower() in OFFICE_SUFFIXES:
            parts, notes = _office_parts(data)
        else:
            parts, notes = [], [("high", "an archive, whose content cannot be reviewed in the pull request")]
        for severity, message in notes:
            add(path, 0, "binary", severity, message)
        for part, content in parts:
            _scan_text(
                add, f"{path}!{part}", content.decode("utf-8", "replace"), allowed_domains, script=False
            )
    else:
        text, notes = _decode(path, data)
        for severity, message in notes:
            add(path, 0, "binary", severity, message)
        image = bool(notes) and _image_kind(data) is not None
        _scan_text(add, path, text, allowed_domains, script=_is_script(path, text), image=image)
    for (at, rule), severity in over.items():
        n = per_rule[(at, rule)] - MAX_PER_RULE
        out.append(Finding(skill, at, 0, rule, severity, f"{n} more {rule} finding(s) in this file"))
    return out


def _scan_text(
    add, path: str, body: str, allowed_domains: Iterable[str], *, script: bool, image: bool = False
):
    starts = [0] + [i + 1 for i, ch in enumerate(body) if ch == "\n"]

    def line(offset: int) -> int:
        return bisect.bisect_right(starts, offset)

    for label, pattern in _SECRETS:
        for m in pattern.finditer(body):
            add(
                path,
                line(m.start()),
                "secret",
                "critical",
                f"{label} ({m.group(0)[:4]}...); remove it and rotate it",
            )

    if not image:  # an image's bytes, read as text, can spell anything
        hidden: dict[int, set[int]] = {}
        for offset, cp in _invisible(body):
            hidden.setdefault(line(offset), set()).add(cp)
        for n, points in sorted(hidden.items()):
            severity = "high" if points == {_SOFT_HYPHEN} else "critical"
            names = ", ".join(_code_point(cp) for cp in sorted(points))
            add(path, n, "hidden-unicode", severity, f"invisible characters that can hide text: {names}")

    for pattern in _REMOTE_EXEC:
        for m in pattern.finditer(body):
            add(path, line(m.start()), "remote-exec", "critical", "downloads code and runs it")

    for para, at in _paragraphs(body):
        for m in _OVERRIDE.finditer(para):
            if not _negated(para, m.start()):
                phrase = " ".join(m.group(0).split())[:80]
                add(
                    path,
                    at(m.start()),
                    "prompt-injection",
                    "high",
                    f"an instruction-override phrase: {phrase!r}",
                )
        for m in _HIDING.finditer(para):
            phrase = " ".join(m.group(0).split())[:80]
            add(
                path,
                at(m.start()),
                "prompt-injection",
                "high",
                f"a phrase that hides what the agent did: {phrase!r}",
            )
        for m in _EXFIL.finditer(para):
            host = _host(m.group(1))
            if host and not _allowed(host, allowed_domains) and not _negated(para, m.start()):
                add(path, at(m.start()), "exfiltration", "high", f"sends what the agent holds to {host}")

    for m in _BLOB.finditer(body):
        if not _diverse(m.group(0)):
            continue
        uri = _DATA_URI.search(body, max(0, m.start() - 80), m.start())
        if uri:
            severity, message = _data_uri_finding(uri.group(1), m.group(0))
        else:
            severity, message = (
                "high",
                f"an encoded run of {len(m.group(0))} characters that a reviewer cannot read",
            )
        add(path, line(m.start()), "encoded-blob", severity, message)
    for m in _WRAPPED.finditer(body):
        run = re.sub(r"[\s>]", "", m.group(0))
        if len(run) >= 200 and _diverse(run) and not _BLOB.search(m.group(0)):
            add(
                path,
                line(m.start()),
                "encoded-blob",
                "high",
                f"an encoded block of {len(run)} characters over lines",
            )

    if script:
        add(path, 0, "script", "medium", "a script; a reviewer should read it")
        for label, pattern in _RISKY:
            for m in pattern.finditer(body):
                add(path, line(m.start()), "risky-call", "high", label)
        m = _NETWORK.search(body)
        if m:
            add(path, line(m.start()), "network-call", "low", "makes network calls")

    hosts: dict[str, int] = {}
    for m in _URL.finditer(body):
        host = _host(m.group(0))
        if host is None:
            add(
                path,
                line(m.start()),
                "url",
                "high",
                "a URL that does not parse, as a lookalike character can make",
            )
        elif host and not _allowed(host, allowed_domains):
            hosts.setdefault(host, line(m.start()))
    for host, n in sorted(hosts.items(), key=lambda kv: kv[1]):
        add(path, n, "url", "medium", f"a URL on {host}, which is not in scan.allowed_domains")


def scan_bundle(bundle: SkillBundle, *, allowed_domains: Iterable[str] = ()) -> list[Finding]:
    """The findings of every file the bundle would publish, in file order."""
    skill = f"{bundle.dept}/{bundle.name}"
    allowed = tuple(allowed_domains)
    found: list[Finding] = []
    for path in sorted(bundle.files):
        found.extend(scan_file(skill, path, bundle.files[path], allowed_domains=allowed))
    return found


def counts(findings: Iterable[Finding]) -> dict[str, int]:
    out = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        out[f.severity] += 1
    return out


def blocking(findings: Iterable[Finding], *, allow_high: bool = False) -> list[Finding]:
    """The findings that fail the check: every critical one, and the high ones unless a reviewer accepted them."""
    return [f for f in findings if f.severity == "critical" or (f.severity == "high" and not allow_high)]


def md(value: object) -> str:
    """Text for a Markdown table cell in a public comment: no table breaks, HTML, links or @mentions."""
    text = " ".join(str(value).split()).replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
    return (
        text.replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("@", "&#64;")
    )


def _rows(findings: Iterable[Finding]) -> Iterator[str]:
    for f in sorted(findings, key=lambda f: (SEVERITIES.index(f.severity), f.skill, f.path, f.line)):
        where = f"{f.path}:{f.line}" if f.line else f.path
        yield f"| {f.severity} | {md(f.skill)} | {md(where)} | {f.rule} | {md(f.message)} |"


def markdown(findings: list[Finding], *, allow_high: bool = False, override_label: str = "") -> Iterator[str]:
    """A Markdown summary for a job summary."""
    c = counts(findings)
    yield "### Security scan"
    yield ""
    yield " / ".join(f"{n} {sev}" for sev, n in c.items())
    stop = blocking(findings, allow_high=allow_high)
    if not findings:
        yield ""
        yield "No findings."
        return
    yield ""
    if stop:
        accept = override_label and any(f.severity == "high" for f in stop)
        yield f"**Blocked by {len(stop)} finding(s).**" + (
            f" A reviewer other than the author can accept the high ones with the `{md(override_label)}` label;"
            " critical ones must be fixed."
            if accept
            else ""
        )
    elif c["high"]:
        yield "A reviewer accepted the high findings."
    else:
        yield "Nothing blocks: the findings are for the reviewer."
    yield ""
    yield "| Severity | Skill | File | Rule | Finding |"
    yield "|---|---|---|---|---|"
    yield from _rows(findings)


def comment_section(reports: list[dict], *, max_chars: int = 6000) -> str:
    """The pull request comment's security-scan section, from `skillhub scan --json` reports: the counts of each
    reviewer, and its findings at high and above. Medium and low findings are in the job summary."""
    lines = ["### Security scan", ""]
    for r in reports:
        c = r.get("counts", {})
        name = "LLM reviewer" if r.get("reviewer") == "llm" else "Rules"
        accepted = " (high findings accepted by a reviewer)" if r.get("allow_high") and c.get("high") else ""
        lines.append(f"- **{name}:** " + ", ".join(f"{c.get(sev, 0)} {sev}" for sev in SEVERITIES) + accepted)
        warnings = r.get("warnings", [])
        if warnings:
            lines.append(f"- {name}: {len(warnings)} warning(s), the first: {md(warnings[0])}")
    serious = [Finding(**f) for r in reports for f in r.get("findings", []) if f.get("severity") in BLOCKING]
    if serious:
        lines += ["", "| Severity | Skill | File | Rule | Finding |", "|---|---|---|---|---|"]
        for i, row in enumerate(_rows(serious)):
            if len("\n".join(lines)) + len(row) > max_chars - 80:
                lines.append(f"\n{len(serious) - i} more finding(s) are in the job summary.")
                break
            lines.append(row)
    lines += ["", "Medium and low findings are listed in the job summary."]
    return "\n".join(lines)
