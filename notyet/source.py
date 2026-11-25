"""Parsing Python source as the interpreter would, whatever its encoding."""
import ast
from typing import Optional


def parse(text: str) -> Optional[ast.Module]:
    """The syntax tree, or None if it can't be parsed. `text` comes from git with undecodable bytes kept as
    surrogates; as bytes again, a coding cookie means what it says."""
    try:
        return ast.parse(text.encode("utf-8", "surrogateescape"))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def printable(text: str) -> str:
    """Safe to write anywhere: no stray surrogates."""
    return text.encode("utf-8", "replace").decode("utf-8")
