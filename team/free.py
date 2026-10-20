"""Free model guard for hermes commands."""

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


def is_free_model(model: str) -> bool:
    """Return True if the model is allowed (ends with :free or in FREE_EXACT)."""
    return model.endswith(":free") or model in FREE_EXACT


def _split_commands(cmd: str) -> list[str]:
    """Split a shell command on ;, &&, ||, |, and parentheses."""
    # Simple approach: split on shell operators
    import re
    # Split on ;, &&, ||, | while respecting parentheses
    # We'll replace ( and ) with markers, split, then clean up
    parts = []
    current = []
    paren_depth = 0
    
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == '(':
            paren_depth += 1
            current.append(ch)
        elif ch == ')':
            paren_depth = max(0, paren_depth - 1)
            current.append(ch)
        elif paren_depth == 0:
            # Check for operators
            if cmd[i:i+2] in ('&&', '||'):
                if current:
                    parts.append(''.join(current).strip())
                current = []
                i += 2
                continue
            elif ch in (';', '|'):
                if current:
                    parts.append(''.join(current).strip())
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    
    if current:
        parts.append(''.join(current).strip())
    
    # Filter out empty parts and parentheses-only parts
    result = []
    for part in parts:
        stripped = part.strip()
        if stripped and stripped not in ('(', ')'):
            result.append(stripped)
    return result


def _find_hermes_invocations(cmd: str) -> list[list[str]]:
    """Find all hermes command invocations in a shell command string."""
    commands = _split_commands(cmd)
    hermes_calls = []
    
    for subcmd in commands:
        try:
            tokens = shlex.split(subcmd)
        except ValueError:
            # Unbalanced quotes - fail closed
            continue
        
        if not tokens:
            continue
            
        # Check if first token is hermes (basename)
        first = tokens[0]
        if first.endswith('/hermes') or first == 'hermes':
            hermes_calls.append(tokens)
    
    return hermes_calls


def _is_chat_invocation(tokens: list[str]) -> bool:
    """Check if this is a hermes chat invocation (or implicit chat via -q/--query/--query-file/-z)."""
    if len(tokens) < 2:
        return False
    
    subcommand = tokens[1]
    
    # Explicit chat subcommand
    if subcommand == 'chat':
        return True
    
    # Implicit chat: -q, --query, --query-file, -z without a subcommand
    # Check if any of these flags appear and there's no other subcommand
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
    """Extract model from -m/--model flag."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == '-m':
            if i + 1 < len(tokens):
                return tokens[i + 1]
        elif token.startswith('--model='):
            return token[8:]  # len('--model=') == 8
        i += 1
    return None


def _extract_provider(tokens: list[str]) -> Optional[str]:
    """Extract provider from --provider flag."""
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
    
    return None