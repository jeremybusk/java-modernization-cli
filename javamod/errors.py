"""The one exception type javamod raises for anything the user needs to fix.

Internal bugs surface as ordinary exceptions (and a traceback); ModError is
reserved for conditions the CLI prints as a one-line ``error:`` message and
exits 2 for, e.g. a missing branch, a dirty source tree, or a failed git/build
command.
"""


class ModError(RuntimeError):
    """A user-facing, actionable failure."""
