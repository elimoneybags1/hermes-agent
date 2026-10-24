"""Free model guard for hermes commands.

check_command() reads a shell command the way the shell will split it and
refuses any hermes call that could reach a model other than a pinned free one.
A hermes call is allowed only as:

  hermes sessions list|export ...      no model is called
  hermes fallback list ...
  hermes [chat] ... -m <free model>    every -m/--model free, every --provider
                                       in FREE_PROVIDERS, nothing else positional

Anything it cannot read for certain is refused. Reading is static, so it does
not see hermes run from a script file, from another language (python -c with a
subprocess list), or under a name assembled from variables.
"""

import fnmatch
import re
from typing import NamedTuple, Optional

# Models that are free despite not ending in ":free"
FREE_EXACT: frozenset[str] = frozenset({
    "gemini-2.5-flash",
    "openai/gpt-oss-120b",
})

# Explicit --provider values allowed. Empty until a provider is checked to serve
# only free models.
FREE_PROVIDERS: frozenset[str] = frozenset()

# The only hermes calls that run without a model
READ_ONLY: frozenset[tuple[str, str]] = frozenset({
    ("sessions", "list"),
    ("sessions", "export"),
    ("fallback", "list"),
})

# hermes options that take a value, top level and chat (`hermes chat --help`).
# Missing one here makes the guard stricter, never looser: its value is then
# read as a positional and refused.
SHORT_VALUE = frozenset("mzqstrcp")
LONG_VALUE: frozenset[str] = frozenset({
    "query", "query-file", "image", "toolsets", "reasoning", "skills", "resume",
    "in", "continue", "max-turns", "run-budget", "source", "usage-file",
    "profile",
})

# Commands that only print or search: `grep hermes` names hermes, it does not
# run it. Not exempt when their output is piped or substituted.
HARMLESS: frozenset[str] = frozenset({
    "echo", "printf", "which", "type", "whereis", "man", "grep", "egrep",
    "fgrep", "rg", "pgrep", "ps", "ls", "cat", "head", "tail", "wc",
})

UNVERIFIED = "cannot verify hermes invocation is pinned to a free model"
MAX_DEPTH = 10

_HERMES_WORD = re.compile(r"\bhermes\b", re.IGNORECASE)
_CODE_CHARS = re.compile(r"[\s;&|<>()$`]")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"^\d*(?:>>|>&|>\||&>>|&>|<<<|<<-|<<|<&|<>|>|<)")


class _Unbalanced(Exception):
    pass


class Word(NamedTuple):
    text: str
    dynamic: bool  # contains an expansion: its value is not this text
    splits: bool   # unquoted expansion, glob or brace: may become several words


class Segment(NamedTuple):
    words: list[Word]
    exposed: bool  # output is piped or captured, so it may be run as code


def is_free_model(model: str) -> bool:
    """Return True if the model is allowed (ends with :free or in FREE_EXACT)."""
    return model.endswith(":free") or model in FREE_EXACT


def _close_paren(s: str, i: int) -> int:
    """Index of the ')' closing a '(' just before s[i], honouring quotes."""
    depth, quote = 1, None
    while i < len(s):
        c = s[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            i += 1
        elif quote == '"':
            if c == '"':
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Unbalanced


def _close_backtick(s: str, i: int) -> int:
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    raise _Unbalanced


def _scan(cmd: str, exposed: bool) -> tuple[list[Segment], list[str]]:
    """Split cmd into simple commands of words, and collect the bodies of
    $(...), `...`, <(...) and >(...) so they can be checked as commands too."""
    segments: list[Segment] = []
    subs: list[str] = []
    words: list[Word] = []
    cur: list[str] = []
    state = {"word": False, "dynamic": False, "splits": False, "piped_in": False}
    quote = None

    def end_word() -> None:
        if state["word"]:
            words.append(Word("".join(cur), state["dynamic"], state["splits"]))
        cur.clear()
        state.update(word=False, dynamic=False, splits=False)

    def end_segment(sep: str) -> None:
        end_word()
        piped_out = sep in ("|", "|&", "(", ")")
        if words:
            segments.append(Segment(list(words), exposed or state["piped_in"] or piped_out))
        words.clear()
        state["piped_in"] = piped_out

    def expansion(unquoted: bool) -> None:
        state.update(word=True, dynamic=True)
        if unquoted:
            state["splits"] = True

    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                cur.append(c)
            i += 1
            continue
        if c == "\\":
            if i + 1 < n:
                nxt = cmd[i + 1]
                if quote == '"' and nxt not in '$`"\\\n':
                    cur.append(c)
                if nxt != "\n":
                    cur.append(nxt)
                    state["word"] = True
            i += 2
            continue
        if cmd.startswith("$(", i) or (quote is None and cmd.startswith(("<(", ">("), i)):
            j = _close_paren(cmd, i + 2)
            subs.append(cmd[i + 2:j])
            cur.append("$")
            expansion(quote is None)
            i = j + 1
            continue
        if c == "`":
            j = _close_backtick(cmd, i + 1)
            subs.append(cmd[i + 1:j])
            cur.append("$")
            expansion(quote is None)
            i = j + 1
            continue
        if quote == '"':
            if c == '"':
                quote = None
            else:
                cur.append(c)
                if c == "$":
                    expansion(False)
            i += 1
            continue
        # Unquoted
        if c in "'\"":
            quote = c
            state["word"] = True
        elif c in " \t":
            end_word()
        elif cmd.startswith(("&&", "||", "|&", ";;"), i):
            end_segment(cmd[i:i + 2])
            i += 2
            continue
        elif c == "&" and (cmd[i - 1:i] in ("<", ">") or cmd[i + 1:i + 2] == ">"):
            cur.append(c)  # part of a redirection: 2>&1, &>file
            state["word"] = True
        elif c in ";&|\n()":
            end_segment(c)
        else:
            if c == "$":
                expansion(True)
            elif c in "*?[{":
                state["splits"] = True
            cur.append(c)
            state["word"] = True
        i += 1

    if quote is not None:
        raise _Unbalanced
    end_segment(";")
    return segments, subs


def _check(cmd: str, exposed: bool = False, depth: int = 0) -> Optional[str]:
    if depth > MAX_DEPTH:
        return UNVERIFIED
    try:
        segments, subs = _scan(cmd, exposed)
    except _Unbalanced:
        return "unbalanced quotes in command"
    for segment in segments:
        reason = _check_segment(segment, depth)
        if reason:
            return reason
    for body in subs:
        reason = _check(body, True, depth + 1)
        if reason:
            return reason
    return None


def _command_index(words: list[Word]) -> Optional[int]:
    """Index of the command word: past assignments and redirections."""
    i = 0
    while i < len(words):
        text = words[i].text
        if _ASSIGNMENT.match(text):
            i += 1
        elif m := _REDIRECT.match(text):
            i += 1 if text[m.end():] else 2
        else:
            return i
    return None


def _is_hermes(word: Word, is_command: bool) -> bool:
    base = word.text.rsplit("/", 1)[-1].lower()
    if base == "hermes":
        return True
    # herm?s, ~/.local/bin/h* as the command word expand to hermes
    return is_command and word.splits and fnmatch.fnmatchcase("hermes", base)


def _check_segment(segment: Segment, depth: int) -> Optional[str]:
    words = segment.words
    cmd = _command_index(words)
    harmless = (
        cmd is not None
        and not segment.exposed
        and words[cmd].text.rsplit("/", 1)[-1] in HARMLESS
    )
    for k, word in enumerate(words):
        if _is_hermes(word, k == cmd):
            if harmless and k != cmd:
                continue
            return _check_hermes(words[k + 1:])
        # A string that may be run as code: bash -c, eval, H=hermes, commit messages
        text = word.text
        embedded = []
        if _CODE_CHARS.search(text):
            embedded.append(text)
        if "=" in text:
            embedded.append(text.split("=", 1)[1])
        for code in embedded:
            if _HERMES_WORD.search(code):
                reason = _check(code, False, depth + 1)
                if reason:
                    return f"{UNVERIFIED}: {reason}"
    return None


def _check_hermes(args: list[Word]) -> Optional[str]:
    """Check the arguments of one hermes call."""
    if len(args) >= 2 and (args[0].text, args[1].text) in READ_ONLY:
        if not (args[0].dynamic or args[1].dynamic):
            return None

    models: list[str] = []
    providers: list[str] = []
    profiles: list[str] = []
    seen_chat = False
    i = 0
    while i < len(args):
        word = args[i]
        text = word.text
        if m := _REDIRECT.match(text):
            i += 1 if text[m.end():] else 2
            continue
        if word.dynamic:
            # "$X" alone may be --provider, unquoted $X may be several words
            return f"{UNVERIFIED}: argument '{text}' expands at run time"

        value: Optional[str] = None
        kind = None
        takes_value = False
        if text.startswith("--") and len(text) > 2:
            name, eq, inline = text[2:].partition("=")
            # argparse accepts any unambiguous prefix: --mod, --prov
            if "model".startswith(name):
                kind = "model"
            elif "provider".startswith(name):
                kind = "provider"
            elif "profile".startswith(name):
                kind = "profile"
            takes_value = kind is not None or name in LONG_VALUE
            if eq:
                value, takes_value = inline, False
        elif text.startswith("-") and len(text) > 1:
            # A cluster such as -Qm X or -mX: the first value option takes the rest
            for j, ch in enumerate(text[1:], 1):
                if ch in SHORT_VALUE:
                    kind = "model" if ch == "m" else ("profile" if ch == "p" else None)
                    if text[j + 1:]:
                        value = text[j + 1:]
                    else:
                        takes_value = True
                    break
        else:
            if text == "chat" and not seen_chat:
                seen_chat = True
                i += 1
                continue
            return (
                f"hermes argument '{text}' is not allowed: only chat on a free model, "
                "sessions list/export and fallback list"
            )

        if takes_value and i + 1 < len(args):
            nxt = args[i + 1]
            # argparse does not take an option-like word as a value
            if not nxt.text.startswith("-") or " " in nxt.text:
                if nxt.splits or (kind and nxt.dynamic):
                    return f"{UNVERIFIED}: argument '{nxt.text}' expands at run time"
                value = nxt.text
                i += 1
        if kind == "model" and value is not None:
            models.append(value)
        elif kind == "provider" and value is not None:
            providers.append(value)
        elif kind == "profile" and value is not None:
            profiles.append(value)
        i += 1

    # Allow team-* profiles (cloned from team-worker which holds free-only settings)
    team_profile_allowed = any(p.startswith("team-") for p in profiles)

    if not models and not team_profile_allowed:
        return "hermes chat requires explicit -m/--model with a free model, or -p/--profile team-*"
    for model in models:
        if not is_free_model(model):
            return f"model '{model}' is not free (must end with :free or be in FREE_EXACT)"
    for provider in providers:
        if provider not in FREE_PROVIDERS:
            return f"provider '{provider}' is not free (not in FREE_PROVIDERS)"
    return None


def check_command(cmd: str) -> Optional[str]:
    """
    Check a shell command for hermes chat invocations without free model pin.

    Returns None if allowed, otherwise a one-line refusal reason.
    """
    return _check(cmd)
