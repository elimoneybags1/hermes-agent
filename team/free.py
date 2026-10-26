"""Free model guard for hermes commands.

check_command() reads a shell command the way the shell will split it and
refuses any hermes call that could reach a model other than a pinned free one.

With a last -p/--profile of team-*:
  hermes [chat] ... -m <free model>    every -m/--model free, every --provider
                                       in FREE_PROVIDERS, nothing else positional
  hermes kanban ...                     all subcommands allowed
  hermes sessions ...                   all subcommands allowed
  hermes dashboard                      no model is called
  hermes skills list                    no model is called
  hermes profile list|describe          no model is called

Without -p (the default profile, which holds paid keys):
  hermes sessions list|export ...       no model is called
  hermes fallback list ...
  hermes profile list                   no model is called
  hermes kanban list|show               read-only subcommands only

Anything it cannot read for certain is refused. Reading is static, so it does
not see hermes run from a script file, from another language (python -c with a
subprocess list), or under a name assembled from variables.
"""

import fnmatch
import functools
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

# Models that are free despite not ending in ":free"
FREE_EXACT: frozenset[str] = frozenset({
    "gemini-2.5-flash",
    "openai/gpt-oss-120b",
})

# Explicit --provider values allowed. Empty until a provider is checked to serve
# only free models.
FREE_PROVIDERS: frozenset[str] = frozenset()

# The only hermes calls that run without a model (for default profile)
READ_ONLY: frozenset[tuple[str, str]] = frozenset({
    ("sessions", "list"),
    ("sessions", "export"),
    ("fallback", "list"),
    ("profile", "list"),
    ("kanban", "list"),
    ("kanban", "show"),
})

# Allowed subcommands for team-* profiles (beyond chat with free model)
TEAM_READ_ONLY: frozenset[str] = frozenset({
    "kanban",
    "sessions",
    "dashboard",
    "skills",
    "profile",
})

# Subcommands of profile that are allowed for team-* profiles
TEAM_PROFILE_SUBCOMMANDS: frozenset[str] = frozenset({"list", "describe"})

# Subcommands of skills that are allowed for team-* profiles
TEAM_SKILLS_SUBCOMMANDS: frozenset[str] = frozenset({"list"})

# Subcommands of kanban that are allowed for default profile (read-only)
DEFAULT_KANBAN_SUBCOMMANDS: frozenset[str] = frozenset({"list", "show"})

# hermes options that take a value, top level and chat (`hermes chat --help`).
# Missing one here makes the guard stricter, never looser: its value is then
# read as a positional and refused.
SHORT_VALUE = frozenset("mzqstrcp")
LONG_VALUE: frozenset[str] = frozenset({
    "query", "query-file", "image", "toolsets", "reasoning", "skills", "resume",
    "in", "continue", "max-turns", "run-budget", "source", "usage-file",
    "profile", "board",
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
    """Return True if the model is allowed (ends with :free or in FREE_EXACT),
    or is Team's LiteLLM router while its config routes only to free models."""
    if model in PROXY_MODELS:
        return proxy_is_free()
    return model.endswith(":free") or model in FREE_EXACT


# Team's LiteLLM auto-router (litellm/). Free only while litellm/build.py --check
# passes: config.yaml is exactly what free-models.json generates, and that
# refuses any model not documented free or from a blocked provider.
PROXY_MODEL = "team-auto"
# Its tier groups, which Team dispatches tasks on directly (team/proxy_tier.py).
PROXY_MODELS = frozenset({PROXY_MODEL, "tier-simple", "tier-medium", "tier-complex", "tier-reasoning"})
_PROXY_BUILD = Path(__file__).resolve().parent.parent / "litellm" / "build.py"


@functools.cache
def proxy_is_free() -> bool:
    try:
        r = subprocess.run([sys.executable, str(_PROXY_BUILD), "--check"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


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
    # First pass: collect profiles, models, providers, and find the subcommand
    models: list[str] = []
    providers: list[str] = []
    profiles: list[str] = []
    seen_chat = False
    subcommand: Optional[str] = None
    subcommand_idx: Optional[int] = None
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
            if subcommand is None:
                subcommand = text
                subcommand_idx = i
            # Don't reject subsequent positional args - they're arguments to the subcommand

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

    # Hermes uses the LAST -p/--profile. Allow only if the last profile is team-*.
    # The default profile holds paid keys: never use it.
    last_profile = profiles[-1] if profiles else None
    last_profile_is_team = last_profile is not None and last_profile.startswith("team-")

    # Check subcommand-based allowances
    if subcommand is not None:
        # First check READ_ONLY for default profile (no team-* profile)
        if not last_profile_is_team:
            # For default profile, only allow specific READ_ONLY subcommands
            # These don't require a team-* profile
            if subcommand_idx is not None:
                next_word = args[subcommand_idx + 1].text if subcommand_idx + 1 < len(args) else ""
                if (subcommand, next_word) in READ_ONLY:
                    if not (args[subcommand_idx].dynamic or 
                            (subcommand_idx + 1 < len(args) and args[subcommand_idx + 1].dynamic)):
                        return None
            return "hermes requires -p/--profile team-* for this command"

        # For team-* profile, allow these subcommands without requiring chat
        if subcommand in TEAM_READ_ONLY:
            # Additional checks for subcommands with sub-subcommands
            if subcommand == "profile":
                next_word = args[subcommand_idx + 1].text if subcommand_idx + 1 < len(args) else ""
                if next_word not in TEAM_PROFILE_SUBCOMMANDS:
                    return f"hermes profile subcommand '{next_word}' not allowed for team-* profile"
            elif subcommand == "skills":
                next_word = args[subcommand_idx + 1].text if subcommand_idx + 1 < len(args) else ""
                if next_word not in TEAM_SKILLS_SUBCOMMANDS:
                    return f"hermes skills subcommand '{next_word}' not allowed for team-* profile"
            elif subcommand == "kanban":
                # For team-* all kanban subcommands allowed
                pass
            elif subcommand == "sessions":
                # For team-* all sessions subcommands allowed
                pass
            elif subcommand == "dashboard":
                # dashboard takes no subcommand
                pass
            return None
        else:
            # Subcommand not in TEAM_READ_ONLY - for team-* profile this is refused
            if last_profile_is_team:
                return f"hermes subcommand '{subcommand}' not allowed for team-* profile"

    # If we get here and there's no subcommand, it's a bare `hermes` which starts chat
    # For team-* profile, chat requires a free model
    if last_profile_is_team:
        # Even when -m is given and free, we still require the last profile to be team-*
        # (or no model but team-* profile). No profile at all is refused.
        if not models and not seen_chat:
            return "hermes chat requires explicit -m/--model with a free model, or -p/--profile team-*"
        for model in models:
            if not is_free_model(model):
                return f"model '{model}' is not free (must end with :free or be in FREE_EXACT)"
        for provider in providers:
            if provider not in FREE_PROVIDERS:
                return f"provider '{provider}' is not free (not in FREE_PROVIDERS)"
        return None

    # Default profile without team-* - only READ_ONLY allowed (already checked above)
    # If chat is attempted without team-* profile, refuse
    if seen_chat:
        return "hermes chat requires explicit -m/--model with a free model, or -p/--profile team-*"
    
    return "hermes requires -p/--profile team-* for this command"


def check_command(cmd: str) -> Optional[str]:
    """
    Check a shell command for hermes chat invocations without free model pin.

    Returns None if allowed, otherwise a one-line refusal reason.
    """
    return _check(cmd)
