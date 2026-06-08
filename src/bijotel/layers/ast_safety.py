"""F14 / Bijuteria #5: AST-First structural code safety.

Detects dangerous code constructs in LLM-generated text by parsing
structurally instead of string-matching. The killer-example from the
catalog: ``rm -rf`` vs ``rm -r -f`` vs ``rm -rfv`` — string matching
catches only the literal "rm -rf"; AST matching catches the structural
pattern ``command name=rm AND args contain both r and f flags`` across
ALL surface variants.

Two code paths:

* **Python** patterns use the standard-library :mod:`ast` module (always
  available, no extra deps). Covers ``exec``/``eval``,
  ``subprocess.run(shell=True)``, ``pickle.loads``, ``os.system``/popen,
  ``__import__``.
* **Bash** patterns use `tree-sitter <https://tree-sitter.github.io/>`_
  + ``tree-sitter-bash``. Installed via the optional extra
  ``pip install bijotel[ast]``. Without the extra, bash checks are
  silently skipped (graceful degradation). Covers dangerous ``rm``,
  ``chmod`` world-writable, ``curl | sh`` pipe-to-shell, ``sudo``.

The :class:`ASTSafetyChecker` exposes ``check_code()`` (parse a single
language snippet) and ``check_prompt()`` (extract \\`\\`\\`python / \\`\\`\\`bash
code blocks from prompt text and check all). The :func:`ast_safety_check`
factory wraps the checker as a :class:`PolicyEngine` rule for pre-call
gating.

Composes naturally with F11 ``prompt_pattern_deny``: regex catches
classic jailbreak phrases; AST catches structural code-execution
patterns the regex misses.

Provenance: pattern catalog inspired by Bijuteria #5 (Aisophical
research notes); tree-sitter-bash grammar from upstream
`tree-sitter-bash <https://github.com/tree-sitter/tree-sitter-bash>`_ (MIT).
"""

from __future__ import annotations

import ast as py_ast
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from bijotel.policy.decision import Decision

_LOG = logging.getLogger("bijotel.ast_safety")


# ============================================================================
# Violation record
# ============================================================================


@dataclass(frozen=True)
class ASTViolation:
    """A single structural-safety violation found in code.

    Attributes:
        pattern_name: Identifier like ``"dangerous_rm"`` or ``"exec_or_eval"``.
        language: ``"python"`` or ``"bash"``.
        node_type: AST node type that matched (e.g. ``"Call"``, ``"command"``).
        line: 1-indexed source line.
        code_snippet: Up to 80 chars of the matching code (truncated).
        severity: ``"critical"`` (sandbox-escape, data loss) or
            ``"warning"`` (suspicious but maybe legitimate).
    """

    pattern_name: str
    language: str
    node_type: str
    line: int
    code_snippet: str
    severity: str


# ============================================================================
# Code-block extraction from prompt text
# ============================================================================

# Markdown fenced code block. Capture language hint if present.
_CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[a-zA-Z]+)?\s*\n(?P<code>.*?)```",
    re.DOTALL,
)

_LANG_ALIAS = {
    "py": "python",
    "python": "python",
    "python3": "python",
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
}


def extract_code_blocks(prompt_text: str) -> list[tuple[str, str]]:
    """Pull (language, code) tuples from markdown-style fenced blocks.

    Recognizes ``\\`\\`\\`python``, ``\\`\\`\\`bash``, plus common aliases
    (``py``, ``sh``, ``shell``, ``zsh``). Unlabeled blocks (just
    ``\\`\\`\\``` with no language hint) are skipped — we don't guess the
    language, false-positives are worse than missed detections in policy
    contexts.
    """
    out: list[tuple[str, str]] = []
    for m in _CODE_BLOCK_RE.finditer(prompt_text):
        lang_raw = (m.group("lang") or "").lower()
        canonical = _LANG_ALIAS.get(lang_raw)
        if canonical is None:
            continue
        out.append((canonical, m.group("code")))
    return out


# ============================================================================
# Python patterns (stdlib ast)
# ============================================================================


class _PythonPattern:
    """Base for Python AST patterns. Subclasses override ``visit``."""

    pattern_name: str = "abstract"
    severity: str = "warning"

    def visit(
        self, tree: py_ast.Module, source: str  # noqa: ARG002
    ) -> Iterator[ASTViolation]:
        raise NotImplementedError


def _snippet(node: py_ast.AST) -> str:
    """Render a Python AST node as source, truncated to 80 chars."""
    try:
        s = py_ast.unparse(node)
    except Exception:
        s = node.__class__.__name__
    return s if len(s) <= 80 else s[:77] + "..."


def _is_name(node: py_ast.AST, name: str) -> bool:
    return isinstance(node, py_ast.Name) and node.id == name


def _is_attr_of(node: py_ast.AST, module: str, attr: str | None = None) -> bool:
    """``mod.X`` or ``mod.attr`` access."""
    if not isinstance(node, py_ast.Attribute):
        return False
    if not _is_name(node.value, module):
        return False
    return attr is None or node.attr == attr


class ExecOrEvalCall(_PythonPattern):
    """``exec(...)`` or ``eval(...)`` direct calls."""

    pattern_name = "exec_or_eval_call"
    severity = "critical"

    def visit(self, tree, source):  # noqa: ARG002
        for node in py_ast.walk(tree):
            if isinstance(node, py_ast.Call) and _is_name(node.func, "exec"):
                yield self._mk(node, "exec")
            elif isinstance(node, py_ast.Call) and _is_name(node.func, "eval"):
                yield self._mk(node, "eval")

    def _mk(self, node: py_ast.Call, which: str) -> ASTViolation:
        return ASTViolation(
            pattern_name=f"{self.pattern_name}:{which}",
            language="python",
            node_type="Call",
            line=node.lineno,
            code_snippet=_snippet(node),
            severity=self.severity,
        )


class SubprocessShellTrue(_PythonPattern):
    """``subprocess.{run,Popen,call,...}(..., shell=True)``."""

    pattern_name = "subprocess_shell_true"
    severity = "critical"

    _SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output", "getoutput"}

    def visit(self, tree, source):  # noqa: ARG002
        for node in py_ast.walk(tree):
            if not isinstance(node, py_ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, py_ast.Attribute)
                and _is_name(func.value, "subprocess")
                and func.attr in self._SUBPROCESS_FUNCS
            ):
                continue
            for kw in node.keywords:
                if kw.arg == "shell" and self._is_true(kw.value):
                    yield ASTViolation(
                        pattern_name=self.pattern_name,
                        language="python",
                        node_type="Call",
                        line=node.lineno,
                        code_snippet=_snippet(node),
                        severity=self.severity,
                    )
                    break

    @staticmethod
    def _is_true(node: py_ast.AST) -> bool:
        return (
            isinstance(node, py_ast.Constant) and node.value is True
        ) or (isinstance(node, py_ast.Name) and node.id == "True")


class PickleLoads(_PythonPattern):
    """``pickle.loads`` / ``pickle.load`` / ``cPickle.loads`` (unsafe deserialization)."""

    pattern_name = "pickle_loads"
    severity = "critical"

    def visit(self, tree, source):  # noqa: ARG002
        for node in py_ast.walk(tree):
            if not isinstance(node, py_ast.Call):
                continue
            func = node.func
            if not isinstance(func, py_ast.Attribute):
                continue
            if func.attr not in ("loads", "load"):
                continue
            if not (
                isinstance(func.value, py_ast.Name)
                and func.value.id in ("pickle", "cPickle", "_pickle")
            ):
                continue
            yield ASTViolation(
                pattern_name=self.pattern_name,
                language="python",
                node_type="Call",
                line=node.lineno,
                code_snippet=_snippet(node),
                severity=self.severity,
            )


class OsSystemOrPopen(_PythonPattern):
    """``os.system``, ``os.popen``, ``os.exec*``, ``os.spawn*`` (shell-level exec)."""

    pattern_name = "os_system_or_popen"
    severity = "critical"

    _OS_FUNCS = {
        "system", "popen", "spawnl", "spawnv", "execl", "execv", "execlp",
        "execvp", "execle", "execve", "spawnlp", "spawnvp",
    }

    def visit(self, tree, source):  # noqa: ARG002
        for node in py_ast.walk(tree):
            if not isinstance(node, py_ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, py_ast.Attribute)
                and _is_attr_of(func, "os")
                and func.attr in self._OS_FUNCS
            ):
                yield ASTViolation(
                    pattern_name=f"{self.pattern_name}:os.{func.attr}",
                    language="python",
                    node_type="Call",
                    line=node.lineno,
                    code_snippet=_snippet(node),
                    severity=self.severity,
                )


class DynamicImport(_PythonPattern):
    """``__import__(...)`` call (dynamic module loading on potentially-tainted input)."""

    pattern_name = "dynamic_import"
    severity = "warning"

    def visit(self, tree, source):  # noqa: ARG002
        for node in py_ast.walk(tree):
            if isinstance(node, py_ast.Call) and _is_name(node.func, "__import__"):
                yield ASTViolation(
                    pattern_name=self.pattern_name,
                    language="python",
                    node_type="Call",
                    line=node.lineno,
                    code_snippet=_snippet(node),
                    severity=self.severity,
                )


BUILTIN_PYTHON_PATTERNS: list[_PythonPattern] = [
    ExecOrEvalCall(),
    SubprocessShellTrue(),
    PickleLoads(),
    OsSystemOrPopen(),
    DynamicImport(),
]


# ============================================================================
# Bash patterns (tree-sitter, optional)
# ============================================================================


def _get_bash_parser():  # noqa: ANN202 (tree_sitter may not be installed)
    """Lazy-load tree-sitter bash parser. Returns None if [ast] extra missing.

    Raises only on programming errors; missing optional deps return None
    so callers can gracefully skip bash checks.
    """
    try:
        import tree_sitter
        import tree_sitter_bash
    except ImportError:
        return None
    lang = tree_sitter.Language(tree_sitter_bash.language())
    return tree_sitter.Parser(lang)


def _walk_bash(node) -> Iterator:  # noqa: ANN001 (tree-sitter node)
    """Yield all descendants of a tree-sitter node (depth-first, includes root)."""
    yield node
    for child in node.children:
        yield from _walk_bash(child)


def _node_text(node, source_bytes: bytes) -> str:  # noqa: ANN001
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _bash_snippet(node, source_bytes: bytes) -> str:  # noqa: ANN001
    text = _node_text(node, source_bytes)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= 80 else text[:77] + "..."


class _BashPattern:
    pattern_name: str = "abstract"
    severity: str = "warning"

    def visit(self, tree, source_bytes: bytes) -> Iterator[ASTViolation]:  # noqa: ANN001 ARG002
        raise NotImplementedError


class DangerousRm(_BashPattern):
    """``rm`` with BOTH -r/-R/--recursive AND -f/--force in any combination.

    Catches the entire variant family that string matching misses:
    ``rm -rf``, ``rm -r -f``, ``rm -fr``, ``rm -rfv``, ``rm --recursive --force``,
    ``rm -R -f``, etc. The structural pattern is: ``command name=rm AND
    args contain BOTH a recursive flag AND a force flag``.
    """

    pattern_name = "dangerous_rm"
    severity = "critical"

    def visit(self, tree, source_bytes):
        for node in _walk_bash(tree.root_node):
            if node.type != "command" or not node.children:
                continue
            head = node.children[0]
            if head.type != "command_name" or _node_text(head, source_bytes).strip() != "rm":
                continue
            flags: set[str] = set()
            for ch in node.children[1:]:
                if ch.type != "word":
                    continue
                text = _node_text(ch, source_bytes).strip()
                if text.startswith("--"):
                    flags.add(text[2:].lower())
                elif text.startswith("-") and len(text) > 1:
                    for c in text[1:]:
                        flags.add(c.lower())
            has_r = "r" in flags or "recursive" in flags or "R" in flags
            has_f = "f" in flags or "force" in flags
            if has_r and has_f:
                yield ASTViolation(
                    pattern_name=self.pattern_name,
                    language="bash",
                    node_type="command",
                    line=node.start_point[0] + 1,
                    code_snippet=_bash_snippet(node, source_bytes),
                    severity=self.severity,
                )


class ChmodWorldWritable(_BashPattern):
    """``chmod 7XX file`` (octal world-write) or ``chmod a+w/o+w file`` (symbolic)."""

    pattern_name = "chmod_world_writable"
    severity = "critical"

    # Octal mode where the last digit (other) has the write bit (2): 2, 3, 6, 7
    _OCTAL_WORLD_WRITE = re.compile(r"^[0-7]?[0-7][0-7][2367]$")
    # Symbolic mode: a+w or o+w (other or all add write)
    _SYMBOLIC_WORLD_WRITE = re.compile(r"[ao][+=][^,\s]*w")

    def visit(self, tree, source_bytes):
        for node in _walk_bash(tree.root_node):
            if node.type != "command" or not node.children:
                continue
            head = node.children[0]
            if head.type != "command_name" or _node_text(head, source_bytes).strip() != "chmod":
                continue
            # Find first non-flag arg. tree-sitter-bash classifies pure-digit
            # tokens as ``number`` and other unquoted tokens as ``word``;
            # both are valid chmod mode arguments.
            for ch in node.children[1:]:
                if ch.type not in ("word", "number"):
                    continue
                arg = _node_text(ch, source_bytes).strip()
                if arg.startswith("-"):
                    continue
                if self._OCTAL_WORLD_WRITE.match(arg) or self._SYMBOLIC_WORLD_WRITE.search(arg):
                    yield ASTViolation(
                        pattern_name=self.pattern_name,
                        language="bash",
                        node_type="command",
                        line=node.start_point[0] + 1,
                        code_snippet=_bash_snippet(node, source_bytes),
                        severity=self.severity,
                    )
                break  # only first non-flag arg


class CurlPipeToShell(_BashPattern):
    """``curl URL | sh`` / ``wget URL | bash`` — fetched-content piped to shell."""

    pattern_name = "curl_pipe_to_shell"
    severity = "critical"

    _FETCHERS = {"curl", "wget", "fetch"}
    _SHELLS = {"sh", "bash", "zsh", "ksh", "dash"}

    def visit(self, tree, source_bytes):
        for node in _walk_bash(tree.root_node):
            if node.type != "pipeline":
                continue
            cmd_names: list[str] = []
            for cmd in node.children:
                if cmd.type != "command" or not cmd.children:
                    continue
                head = cmd.children[0]
                if head.type == "command_name":
                    cmd_names.append(_node_text(head, source_bytes).strip())
            has_fetch = any(n in self._FETCHERS for n in cmd_names)
            has_shell = any(n in self._SHELLS for n in cmd_names)
            if has_fetch and has_shell:
                yield ASTViolation(
                    pattern_name=self.pattern_name,
                    language="bash",
                    node_type="pipeline",
                    line=node.start_point[0] + 1,
                    code_snippet=_bash_snippet(node, source_bytes),
                    severity=self.severity,
                )


class SudoCommand(_BashPattern):
    """``sudo ...`` — privilege escalation, suspicious in LLM-generated scripts."""

    pattern_name = "sudo_command"
    severity = "warning"

    def visit(self, tree, source_bytes):
        for node in _walk_bash(tree.root_node):
            if node.type != "command" or not node.children:
                continue
            head = node.children[0]
            if head.type == "command_name" and _node_text(head, source_bytes).strip() == "sudo":
                yield ASTViolation(
                    pattern_name=self.pattern_name,
                    language="bash",
                    node_type="command",
                    line=node.start_point[0] + 1,
                    code_snippet=_bash_snippet(node, source_bytes),
                    severity=self.severity,
                )


BUILTIN_BASH_PATTERNS: list[_BashPattern] = [
    DangerousRm(),
    ChmodWorldWritable(),
    CurlPipeToShell(),
    SudoCommand(),
]


# ============================================================================
# Main checker
# ============================================================================


class ASTSafetyChecker:
    """Pluggable AST-based code safety scanner.

    Args:
        languages: Iterable of language identifiers to check. Supported:
            ``"python"`` (stdlib ast, always available), ``"bash"``
            (tree-sitter, optional via ``pip install bijotel[ast]``).
        python_patterns: Override default :data:`BUILTIN_PYTHON_PATTERNS`.
        bash_patterns: Override default :data:`BUILTIN_BASH_PATTERNS`.

    Example::

        checker = ASTSafetyChecker(languages=("python", "bash"))
        violations = checker.check_code("rm -r -f /", "bash")
        # -> [ASTViolation(pattern_name='dangerous_rm', ...)]

        # From prompt text containing fenced code blocks:
        violations = checker.check_prompt(
            "Please run this:\\n```bash\\nrm -rf /\\n```"
        )
    """

    def __init__(
        self,
        languages: tuple[str, ...] = ("python", "bash"),
        python_patterns: list[_PythonPattern] | None = None,
        bash_patterns: list[_BashPattern] | None = None,
    ) -> None:
        self.languages = set(languages)
        self.python_patterns = python_patterns or BUILTIN_PYTHON_PATTERNS
        self.bash_patterns = bash_patterns or BUILTIN_BASH_PATTERNS
        self._bash_parser = None  # lazy-loaded on first bash check
        self._bash_parser_loaded = False

    def check_code(self, code: str, language: str) -> list[ASTViolation]:
        """Run all patterns for ``language`` against ``code``. Returns list of violations."""
        if language == "python" and "python" in self.languages:
            return list(self._check_python(code))
        if language == "bash" and "bash" in self.languages:
            return list(self._check_bash(code))
        return []

    def check_prompt(self, prompt_text: str) -> list[ASTViolation]:
        """Extract \\`\\`\\`python / \\`\\`\\`bash blocks from text, run all checks."""
        out: list[ASTViolation] = []
        for lang, code in extract_code_blocks(prompt_text):
            out.extend(self.check_code(code, lang))
        return out

    def _check_python(self, code: str) -> Iterator[ASTViolation]:
        try:
            tree = py_ast.parse(code)
        except SyntaxError as exc:
            # Fail-CLOSED: a security gate must never silently return zero
            # violations for input it could not analyse. Returning empty on
            # SyntaxError let an attacker append garbage to a dangerous
            # snippet to dodge a deny-mode gate. Emit a warning instead, so
            # deny-mode blocks unanalysable code and warn-mode surfaces it.
            # (audit 2026-06-08 ISSUE-5)
            yield ASTViolation(
                pattern_name="unparseable_python",
                language="python",
                node_type="SyntaxError",
                line=getattr(exc, "lineno", 0) or 0,
                code_snippet=code.strip()[:80],
                severity="warning",
            )
            return
        for pattern in self.python_patterns:
            yield from pattern.visit(tree, code)

    def _check_bash(self, code: str) -> Iterator[ASTViolation]:
        if not self._bash_parser_loaded:
            self._bash_parser = _get_bash_parser()
            self._bash_parser_loaded = True
            if self._bash_parser is None:
                _LOG.info(
                    "tree-sitter / tree-sitter-bash not installed; bash AST checks "
                    "skipped. Install with: pip install bijotel[ast]"
                )
        if self._bash_parser is None:
            return
        source_bytes = code.encode("utf-8")
        tree = self._bash_parser.parse(source_bytes)
        for pattern in self.bash_patterns:
            yield from pattern.visit(tree, source_bytes)


# ============================================================================
# PolicyEngine integration
# ============================================================================


def _extract_prompt_text(messages: object) -> str:
    """Concatenate user-message text from a request's messages list.

    Handles plain-string, Anthropic-multipart, and OTel parts shapes.
    Same approach as F11 prompt_pattern_deny / fingerprint._extract_text.
    """
    if isinstance(messages, str):
        return messages
    parts: list[str] = []
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict):
                        t = p.get("text") or p.get("content") or ""
                        if isinstance(t, str):
                            parts.append(t)
            ps = m.get("parts")
            if isinstance(ps, list):
                for p in ps:
                    if isinstance(p, dict):
                        t = p.get("content") or p.get("text") or ""
                        if isinstance(t, str):
                            parts.append(t)
    return " ".join(p for p in parts if p)


def ast_safety_check(
    languages: tuple[str, ...] = ("python", "bash"),
    *,
    mode: str = "warn",
    checker: ASTSafetyChecker | None = None,
) -> Callable[[dict], Decision]:
    """Factory: PolicyEngine rule that scans code blocks for AST-level violations.

    Args:
        languages: Which languages to scan (``"python"`` always works;
            ``"bash"`` needs ``[ast]`` extra).
        mode: ``"deny"`` (block + raise via guard) or ``"warn"`` (audit
            + allow).
        checker: Override the default :class:`ASTSafetyChecker`. Useful
            for custom pattern sets.

    Returns:
        A rule callable matching the :class:`PolicyEngine` contract.

    Example::

        from bijotel import PolicyEngine, prompt_pattern_deny, ast_safety_check

        engine = PolicyEngine([
            prompt_pattern_deny(mode="warn"),
            ast_safety_check(languages=("python", "bash"), mode="warn"),
        ])
        decision, warns = engine.evaluate({
            "messages": [{"role": "user", "content": "```bash\\nrm -r -f /\\n```"}]
        })

    Raises:
        ValueError: if ``mode`` is neither ``"deny"`` nor ``"warn"``.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    chk = checker or ASTSafetyChecker(languages=languages)

    def rule(request: dict) -> Decision:
        messages = request.get("messages", [])
        prompt_text = _extract_prompt_text(messages)
        if not prompt_text:
            return Decision.allow()

        violations = chk.check_prompt(prompt_text)
        if not violations:
            return Decision.allow()

        # Prefer the first critical, else first warning
        critical = [v for v in violations if v.severity == "critical"]
        first = critical[0] if critical else violations[0]
        reason = (
            f"AST safety violation: {first.pattern_name} "
            f"({first.language} line {first.line}): {first.code_snippet}"
        )
        if len(reason) > 200:
            reason = reason[:197] + "..."

        if mode == "deny":
            return Decision.deny(rule="ast_safety_check", reason=reason)
        return Decision.warn(rule="ast_safety_check", reason=reason)

    return rule
