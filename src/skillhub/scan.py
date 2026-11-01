"""The security scan of skill bundles: rules that need no model and no workspace (spec §9.2).

It reads what would be published, the bundle's files without `evals/`, and grades each finding:
- `critical` blocks the pull request and the publish;
- `high` blocks the pull request unless a reviewer accepts it (`ScanSettings.override_label`);
- `medium` and `low` are reported for the reviewer, and never block.

Findings go to job summaries and pull-request comments, so a message never quotes a secret: it names the rule and
shows at most the first four characters.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from skillhub.bundle import SkillBundle

SEVERITIES = ("critical", "high", "medium", "low")
BLOCKING = ("critical", "high")


@dataclass(frozen=True)
class Finding:
    skill: str  # <dept>/<skill>
    path: str  # the file, relative to the skill folder
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

# Characters that change what a reader sees without being visible: zero-width characters, bidirectional controls
# (Trojan Source) and Unicode tag characters (ASCII smuggling). A byte order mark at the very start is allowed.
_HIDDEN = re.compile(
    "[\u200b-\u200d\u2060-\u2064\u180e\ufeff\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]"
)

_REMOTE_EXEC = [
    re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"),
    re.compile(r"\b(?:ba|z)?sh\s+<\(\s*(?:curl|wget)\b"),
    re.compile(
        r"\b(?:iex|Invoke-Expression)\b[^\n]*\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod|DownloadString)\b",
        re.I,
    ),  # fmt: skip
    re.compile(
        r"\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(?:iex|Invoke-Expression)\b", re.I
    ),
    re.compile(r"\bexec\s*\(\s*(?:requests\.get|urllib\.request\.urlopen|urlopen)\s*\("),
]

_INJECTION = re.compile(
    "|".join([
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?(?:of\s+)?(?:the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|original)\s+(?:instructions|prompts?|messages|rules|guidelines)",
        r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|notify|alert|reveal\s+to|mention\s+(?:this\s+)?to)\s+the\s+user",
        r"\bwithout\s+(?:telling|informing|notifying|asking|alerting)\s+the\s+user",
        # hiding what the agent did: "there is no need to mention it in your answer"
        r"\b(?:do\s+not|don't|never|no\s+need\s+to)\s+(?:mention|tell|say|disclose|report)\s+"
        r"(?:it|this|that|these\s+steps|what\s+you\s+did)\b[^.\n]{0,30}\b(?:user|answer|response|reply)\b",
        r"\b(?:reveal|print|output|show|repeat|leak)\s+(?:your\s+|the\s+)?(?:system\s+prompt|hidden\s+instructions)",
        r"\bexfiltrat",
        r"\b(?:send|post|upload|forward|transmit)\s+(?:all\s+|the\s+|any\s+)?(?:user'?s?\s+)?"
        r"(?:conversation|chat\s+history|credentials|secrets|passwords|api\s+keys?|access\s+tokens?|environment\s+variables)",
        r"\byou\s+are\s+now\s+(?:in\s+)?(?:an?\s+)?(?:unrestricted|jailbroken|developer\s+mode|dan)\b",
    ]),
    re.I,
)  # fmt: skip

# Sending what the agent holds to a URL. Counted only when the URL's host is not in scan.allowed_domains.
_EXFIL = re.compile(
    r"\b(?:send|post|upload|forward|transmit|submit|copy)\b[^.\n]{0,100}"
    r"\b(?:conversation|chat|transcript|credentials?|secrets?|passwords?|api\s+keys?|tokens?|holdings|portfolio|"
    r"personal\s+data|customer\s+data|files?|workbook|documents?)\b[^.\n]{0,100}?(https?://[^\s<>()\[\]{}\"'`|\\]+)",
    re.I,
)
_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}|\b[0-9a-fA-F]{200,}\b")
_IMAGE_DATA_URI = re.compile(r"data:image/(?:png|jpe?g|gif|webp|svg\+xml);base64,$")

_RISKY = [
    ("eval or exec of a string", re.compile(r"(?<![\w.])(?:eval|exec)\s*\(")),
    ("a shell command with shell=True", re.compile(r"\bsubprocess\.\w+\([^)]*shell\s*=\s*True")),
    ("os.system or os.popen", re.compile(r"\bos\.(?:system|popen)\s*\(")),
    ("unpickling data", re.compile(r"\b(?:pickle|cPickle|dill)\.loads?\s*\(")),
    (
        "a recursive delete of a home or root folder",
        re.compile(r"\brm\s+-[a-zA-Z]*(?:r[a-zA-Z]*f|f[a-zA-Z]*r)[a-zA-Z]*\s+(?:/|~/?|\$HOME/?)\*?(?=\s|$)"),
    ),
    ("a write to a shell profile, SSH keys or crontab",
     re.compile(r"(?:>>?|tee\s+-a)\s*~?/?[\w./$]*(?:\.bashrc|\.zshrc|\.profile|\.bash_profile|authorized_keys)\b|\bcrontab\s+-")),
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
    {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl",
     ".php", ".lua", ".vbs", ".applescript", ".scpt"}
)  # fmt: skip
# ELF, and Mach-O in both byte orders and as a universal binary. Windows programs are checked in _executable.
_EXECUTABLE_MAGIC = (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                     b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")  # fmt: skip
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_script(path: str, text: str) -> bool:
    return PurePosixPath(path).suffix.lower() in SCRIPT_SUFFIXES or text.startswith("#!")


def _executable(data: bytes) -> bool:
    if data[:2] == b"MZ":  # a DOS header; a Windows program also has the PE signature
        return b"PE\x00\x00" in data[:1024]
    return data.startswith(_EXECUTABLE_MAGIC)


def _image(data: bytes) -> bool:
    return data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def _allowed(host: str, allowed: Iterable[str]) -> bool:
    """A host in the allowlist, or a subdomain of one. Documentation hosts (RFC 2606) are always allowed."""
    host = host.lower().rstrip(".")
    if host.endswith(_EXAMPLE_SUFFIXES):
        return True
    return any(host == d or host.endswith("." + d) for d in (*_EXAMPLE_HOSTS, *allowed))


def _code_point(ch: str) -> str:
    name = unicodedata.name(ch, "")
    return f"U+{ord(ch):04X}" + (f" {name}" if name else "")


def scan_file(skill: str, path: str, data: bytes, *, allowed_domains: Iterable[str] = ()) -> list[Finding]:
    """The findings of one file of a bundle."""
    out: list[Finding] = []

    def add(line: int, rule: str, severity: str, message: str) -> None:
        out.append(Finding(skill, path, line, rule, severity, message))

    if _executable(data):
        add(0, "binary", "critical", "an executable file; a skill must not ship programs")
        return out
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if _image(data):
            add(0, "binary", "medium", "an image; check that it shows what the skill says")
        else:
            add(0, "binary", "high", "a file that is not UTF-8 text, so it cannot be reviewed as text")
        return out
    body = text.removeprefix(
        "\ufeff"
    )  # a byte order mark at the start is harmless; anywhere else it is hidden

    for label, pattern in _SECRETS:
        for m in pattern.finditer(body):
            add(
                _line(body, m.start()),
                "secret",
                "critical",
                f"{label} ({m.group(0)[:4]}…); remove it and rotate it",
            )

    hidden: dict[int, set[str]] = {}
    for m in _HIDDEN.finditer(body):
        hidden.setdefault(_line(body, m.start()), set()).add(_code_point(m.group(0)))
    for line, points in sorted(hidden.items()):
        add(
            line,
            "hidden-unicode",
            "critical",
            f"invisible characters that can hide text: {', '.join(sorted(points))}",
        )

    for pattern in _REMOTE_EXEC:
        for m in pattern.finditer(body):
            add(_line(body, m.start()), "remote-exec", "critical", "downloads code and runs it")

    for m in _INJECTION.finditer(body):
        phrase = " ".join(m.group(0).split())
        add(
            _line(body, m.start()),
            "prompt-injection",
            "high",
            f"an instruction-override or hiding phrase: {phrase!r}",
        )

    for m in _EXFIL.finditer(body):
        host = urlsplit(m.group(1).rstrip(".,;:")).hostname or ""
        if host and not _allowed(host, allowed_domains):
            add(_line(body, m.start()), "exfiltration", "high", f"sends what the agent holds to {host}")

    for m in _BLOB.finditer(body):
        if _IMAGE_DATA_URI.search(body[max(0, m.start() - 40) : m.start()]):
            add(
                _line(body, m.start()),
                "encoded-blob",
                "medium",
                f"an embedded image of {len(m.group(0))} characters",
            )
        else:
            add(_line(body, m.start()), "encoded-blob", "high",
                f"an encoded run of {len(m.group(0))} characters that a reviewer cannot read")  # fmt: skip

    script = _is_script(path, body)
    if script:
        add(0, "script", "medium", "a script; a reviewer should read it")
        for label, pattern in _RISKY:
            for m in pattern.finditer(body):
                add(_line(body, m.start()), "risky-call", "high", label)
        m = _NETWORK.search(body)
        if m:
            add(_line(body, m.start()), "network-call", "low", "makes network calls")

    hosts: dict[str, int] = {}
    for m in _URL.finditer(body):
        host = urlsplit(m.group(0).rstrip(".,;:")).hostname or ""
        if host and not _allowed(host, allowed_domains):
            hosts.setdefault(host, _line(body, m.start()))
    for host, line in sorted(hosts.items(), key=lambda kv: kv[1]):
        add(line, "url", "medium", f"a URL on {host}, which is not in scan.allowed_domains")
    return out


def mask_secrets(text: str) -> str:
    """The text with anything that looks like a credential cut to its first four characters."""
    for _, pattern in _SECRETS:
        text = pattern.sub(lambda m: m.group(0)[:4] + "...", text)
    return text


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


def markdown(findings: list[Finding], *, allow_high: bool = False, override_label: str = "") -> Iterator[str]:
    """A Markdown summary for a job summary or a pull-request comment."""
    c = counts(findings)
    yield "### Security scan"
    yield ""
    yield " · ".join(f"{n} {sev}" for sev, n in c.items())
    stop = blocking(findings, allow_high=allow_high)
    if not findings:
        yield ""
        yield "No findings."
        return
    yield ""
    if stop:
        accept = override_label and any(f.severity == "high" for f in stop)
        yield f"**Blocked by {len(stop)} finding(s).**" + (
            f" A reviewer can accept the high ones with the `{override_label}` label; critical ones must be fixed."
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
    for f in sorted(findings, key=lambda f: (SEVERITIES.index(f.severity), f.skill, f.path, f.line)):
        where = f"{f.path}:{f.line}" if f.line else f.path
        yield f"| {f.severity} | {f.skill} | `{where}` | {f.rule} | {f.message.replace('|', '/')} |"


def comment_section(reports: list[dict], *, max_chars: int = 6000) -> str:
    """The pull request comment's security-scan section, from `skillhub scan --json` reports: the counts of each
    reviewer, and its findings at high and above. Medium and low findings are in the job summary."""
    lines = ["### Security scan", ""]
    for r in reports:
        c = r.get("counts", {})
        name = "LLM reviewer" if r.get("reviewer") == "llm" else "Rules"
        accepted = " (high findings accepted by a reviewer)" if r.get("allow_high") and c.get("high") else ""
        lines.append(f"- **{name}:** " + ", ".join(f"{c.get(sev, 0)} {sev}" for sev in SEVERITIES) + accepted)
        lines += [f"- {name}: warning: {w}" for w in r.get("warnings", [])]
    serious = [Finding(**f) for r in reports for f in r.get("findings", []) if f.get("severity") in BLOCKING]
    if serious:
        lines += ["", "| Severity | Skill | File | Rule | Finding |", "|---|---|---|---|---|"]
        for i, f in enumerate(
            sorted(serious, key=lambda f: (SEVERITIES.index(f.severity), f.skill, f.path, f.line))
        ):
            where = f"{f.path}:{f.line}" if f.line else f.path
            row = f"| {f.severity} | {f.skill} | `{where}` | {f.rule} | {f.message.replace('|', '/')} |"
            if len("\n".join(lines)) + len(row) > max_chars - 80:
                lines.append(f"\n{len(serious) - i} more finding(s) are in the job summary.")
                break
            lines.append(row)
    lines += ["", "Medium and low findings are listed in the job summary."]
    return "\n".join(lines)
