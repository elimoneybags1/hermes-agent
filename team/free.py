"""Free model guard for hermes commands."""

import re
import shlex
from typing import Optional

# Models that are free despite not ending in ":free"
FREE_EXACT: frozenset[str] = frozenset({
    "gemini-2.5-flash",
    "openai/gpt-oss-120b",
})

# Providers that are known to be paid
PAID_PROVIDERS: frozenset[str] = frozenset({
    "anthropic",
    "openai",
    "openai-codex",
    "codex",
    "cerebras",
    "xai",
    "deepseek",
})

# Wrapper commands that can precede the actual command
WRAPPER_COMMANDS: frozenset[str] = frozenset({
    "env",
    "command",
    "exec",
    "nohup",
    "time",
    "timeout",
    "nice",
    "uv",
    "uvx",
})


def is_free_model(model: str) -> bool:
    """Return True if the model is allowed (ends with :free or in FREE_EXACT)."""
    return model.endswith(":free") or model in FREE_EXACT


def _split_commands(cmd: str) -> list[str]:
    """Split a shell command on ;, &, &&, ||, |, and newlines. Respects braces and parens."""
    parts = []
    current = []
    paren_depth = 0
    brace_depth = 0

    i = 0
    while i < len(cmd):
        ch = cmd[i]
        # Handle parentheses and braces FIRST (before checking for separators)
        if ch == '(':
            paren_depth += 1
            current.append(ch)
        elif ch == ')':
            paren_depth = max(0, paren_depth - 1)
            current.append(ch)
        elif ch == '{':
            brace_depth += 1
            current.append(ch)
        elif ch == '}':
            brace_depth = max(0, brace_depth - 1)
            current.append(ch)
        elif paren_depth == 0 and brace_depth == 0:
            # At top level - check for operators
            if cmd[i:i+2] in ('&&', '||'):
                if current:
                    parts.append(''.join(current).strip())
                current = []
                i += 2
                continue
            elif ch in (';', '|', '&', '\n'):
                if current:
                    parts.append(''.join(current).strip())
                current = []
                i += 1
                continue
            else:
                # Regular character at top level
                current.append(ch)
        else:
            # Inside parens/braces - just append
            current.append(ch)
        i += 1

    if current:
        parts.append(''.join(current).strip())

    # Filter out empty parts and parentheses/braces-only parts
    result = []
    for part in parts:
        stripped = part.strip()
        if stripped and stripped not in ('(', ')', '{', '}'):
            result.append(stripped)
    return result


def _skip_var_assignments_and_wrappers(tokens: list[str]) -> list[str]:
    """Skip leading VAR=value assignments and wrapper commands."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # VAR=value assignment
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', token):
            i += 1
            continue
        # Wrapper commands
        if token in WRAPPER_COMMANDS:
            # Special handling: "timeout N" or "nice -N" consume extra args
            if token == "timeout" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
                i += 2
                continue
            if token == "nice" and i + 1 < len(tokens) and tokens[i + 1].startswith("-"):
                i += 2
                continue
            # uv run, uvx need special handling
            if token == "uv" and i + 1 < len(tokens) and tokens[i + 1] == "run":
                i += 2
                continue
            i += 1
            continue
        break
    return tokens[i:]


def _extract_body_braces(s: str) -> Optional[str]:
    """Extract content from the first { ... } pair, handling nested braces."""
    start = s.find('{')
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(s[start:], start=start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return s[start + 1:i]
    return None


def _extract_body_parens(s: str) -> Optional[str]:
    """Extract content from the first ( ... ) pair, handling nested parens."""
    start = s.find('(')
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(s[start:], start=start):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return s[start + 1:i]
    return None


def _find_hermes_in_string(s: str) -> list[list[str]]:
    """Find hermes invocations in a command string (recursively handles subshells)."""
    hermes_calls = []

    # Split on command separators at top level
    commands = _split_commands(s)

    for subcmd in commands:
        subcmd = subcmd.strip()
        if not subcmd:
            continue

        # Try to parse as tokens first
        try:
            tokens = shlex.split(subcmd)
        except ValueError:
            # Unbalanced quotes - skip this part
            continue

        if not tokens:
            continue

        # Check for function definition: name() { ... } or name () { ... }
        # Pattern: first token ends with () or first token is name and second is ()
        is_func_def = False
        func_body = None
        if len(tokens) >= 2:
            if tokens[0].endswith('()') or (tokens[0] and tokens[1] == '()'):
                is_func_def = True
            elif tokens[0] == '{' or tokens[0].endswith('{'):
                # Brace group at start
                is_func_def = True
        
        if is_func_def:
            body = _extract_body_braces(subcmd)
            if body:
                hermes_calls.extend(_find_hermes_in_string(body))
            continue

        # Skip var assignments and wrappers
        tokens = _skip_var_assignments_and_wrappers(tokens)

        if not tokens:
            continue

        # Check if first token is hermes (basename)
        first = tokens[0]
        if first.endswith('/hermes') or first == 'hermes':
            hermes_calls.append(tokens)
            continue

        # Check for bash -c, sh -c, zsh -c
        if len(tokens) >= 3 and tokens[0] in ('bash', 'sh', 'zsh') and tokens[1] == '-c':
            inner_cmd = tokens[2]
            hermes_calls.extend(_find_hermes_in_string(inner_cmd))
            continue

        # Check for brace group { ... } at top level
        if tokens[0] == '{' or (tokens[0] and tokens[0].endswith('{')):
            body = _extract_body_braces(subcmd)
            if body:
                hermes_calls.extend(_find_hermes_in_string(body))
            continue

        # Check for subshell ( ... ) at top level
        if tokens[0] == '(' or (tokens[0] and tokens[0].endswith('(')):
            body = _extract_body_parens(subcmd)
            if body:
                hermes_calls.extend(_find_hermes_in_string(body))
            continue

        # Check for $(...) or `...` - command substitution
        for match in re.finditer(r'\$\(([^)]+)\)', subcmd):
            hermes_calls.extend(_find_hermes_in_string(match.group(1)))
        for match in re.finditer(r'`([^`]+)`', subcmd):
            hermes_calls.extend(_find_hermes_in_string(match.group(1)))

    return hermes_calls


def _find_hermes_invocations(cmd: str) -> list[list[str]]:
    """Find all hermes command invocations in a shell command string."""
    return _find_hermes_in_string(cmd)


def _is_chat_invocation(tokens: list[str]) -> bool:
    """Check if this is a hermes chat invocation (or implicit chat via -q/--query/--query-file/-z)."""
    if len(tokens) < 2:
        return False

    subcommand = tokens[1]

    # Explicit chat subcommand
    if subcommand == 'chat':
        return True

    # Implicit chat: -q, --query, --query-file, -z without a subcommand
    chat_flags = {'-q', '--query', '--query-file', '-z'}
    has_chat_flag = any(t in chat_flags for t in tokens[1:])

    # List of known non-chat subcommands
    non_chat_subcommands = {
        'sessions', 'fallback', 'models', 'providers', 'skills',
        'plugins', 'cron', 'memory', 'config', 'auth', 'doctor'
    }

    # If first arg after hermes is a known non-chat subcommand, it's not chat
    if subcommand in non_chat_subcommands:
        return False

    # If we have a chat flag and no explicit subcommand, it's implicit chat
    if has_chat_flag:
        return True

    return False


def _extract_model(tokens: list[str]) -> Optional[str]:
    """Extract model from -m/--model flag (supports -m X, --model X, --model=X)."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == '-m':
            if i + 1 < len(tokens):
                return tokens[i + 1]
        elif token == '--model':
            if i + 1 < len(tokens):
                return tokens[i + 1]
        elif token.startswith('--model='):
            return token[8:]  # len('--model=') == 8
        i += 1
    return None


def _extract_provider(tokens: list[str]) -> Optional[str]:
    """Extract provider from --provider flag (supports --provider X, --provider=X)."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == '--provider':
            if i + 1 < len(tokens):
                return tokens[i + 1]
        elif token.startswith('--provider='):
            return token[11:]  # len('--provider=') == 11
        i += 1
    return None


def _count_hermes_chat_occurrences(cmd: str) -> int:
    """Count occurrences of hermes with chat-related keywords in the raw command."""
    pattern = r'\bhermes\b.*?(?:chat|-q\b|-z\b|--query\b|--query-file\b)|(?:chat|-q\b|-z\b|--query\b|--query-file\b).*?\bhermes\b'
    matches = re.findall(pattern, cmd, re.IGNORECASE)
    return len(matches)


def _count_approved_hermes_chat(tokens_list: list[list[str]]) -> int:
    """Count approved hermes chat invocations (those with free model pin)."""
    count = 0
    for tokens in tokens_list:
        if _is_chat_invocation(tokens):
            model = _extract_model(tokens)
            if model and is_free_model(model):
                provider = _extract_provider(tokens)
                if not (provider and provider in PAID_PROVIDERS):
                    count += 1
    return count


def check_command(cmd: str) -> Optional[str]:
    """
    Check a shell command for hermes chat invocations without free model pin.

    Returns None if allowed, otherwise a one-line refusal reason.
    """
    try:
        # First, try to parse the whole command to catch unbalanced quotes
        shlex.split(cmd)
    except ValueError:
        return "unbalanced quotes in command"

    hermes_calls = _find_hermes_invocations(cmd)

    # Check each hermes call
    for tokens in hermes_calls:
        if not _is_chat_invocation(tokens):
            # Read-only subcommands like sessions list, fallback list are allowed
            continue

        # This is a chat invocation - check for model pin
        model = _extract_model(tokens)
        if not model:
            return "hermes chat requires explicit -m/--model with a free model"

        if not is_free_model(model):
            return f"model '{model}' is not free (must end with :free or be in FREE_EXACT)"

        # Check for paid provider
        provider = _extract_provider(tokens)
        if provider and provider in PAID_PROVIDERS:
            return f"provider '{provider}' is not free"

    # Backstop: fail closed if raw command has hermes+chat keywords but we didn't approve them
    raw_chat_count = _count_hermes_chat_occurrences(cmd)
    approved_count = _count_approved_hermes_chat(hermes_calls)

    if raw_chat_count > approved_count:
        return "cannot verify hermes invocation is pinned to a free model"

    return None