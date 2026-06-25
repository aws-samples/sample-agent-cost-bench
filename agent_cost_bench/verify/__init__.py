"""
Generalized verification: a declarative ``verify:`` spec on a task runs a test
command inside a Docker image and a named result parser turns the output into a
graduated score — so a new task needs only config + tests, no bespoke shell.
"""

from .parsers import ParseResult, parse_results, PARSERS
from .runner import DockerVerifyRunner

__all__ = ["ParseResult", "parse_results", "PARSERS", "DockerVerifyRunner"]
