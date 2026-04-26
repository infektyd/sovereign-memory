"""
Sovereign Memory — Token Counting Utilities.

Provides a singleton tiktoken encoder and a helper for counting tokens in
text strings. Used by the chunker and retrieval engine for accurate context
budgeting (replacing the previous word-count / 0.75 approximation).

The encoder is cl100k_base — the same encoding used by GPT-4 and Claude's
tokenizer family. It gives accurate counts for mixed prose, code, and
markdown.

Usage:
    from tokens import count_tokens, get_encoder

    n = count_tokens("Hello, world!")   # → 4
    enc = get_encoder()                 # tiktoken.Encoding singleton
"""

import functools
import logging

logger = logging.getLogger("sovereign.tokens")


@functools.cache
def get_encoder():
    """
    Return the process-wide tiktoken encoder singleton (cl100k_base).

    Returns the encoder on success, or None if tiktoken is not installed.
    Callers that receive None should fall back to a word-count approximation.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return enc
    except ImportError:
        logger.warning(
            "tiktoken not installed — token counting will use word-count approximation"
        )
        return None
    except Exception as e:
        logger.warning("Failed to load tiktoken encoder: %s", e)
        return None


def count_tokens(text: str) -> int:
    """
    Count the number of tokens in *text* using cl100k_base encoding.

    Falls back to a word-count approximation (words / 0.75) if tiktoken
    is unavailable, preserving the same degraded behavior as before this
    module was introduced.

    Args:
        text: The string to count tokens for.

    Returns:
        Integer token count.
    """
    enc = get_encoder()
    if enc is not None:
        return len(enc.encode(text))
    # Fallback: rough word-count approximation (1 token ≈ 0.75 words)
    return int(len(text.split()) / 0.75)
