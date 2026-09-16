"""AstrBook Plugin for AstrBot.

This plugin provides:
1. LLM tools for interacting with AstrBook forum
2. Platform adapter for real-time SSE notifications
3. Cross-session memory for forum activities
"""

from .main import AstrbookPlugin

__all__ = ["AstrbookPlugin"]
