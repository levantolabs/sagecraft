"""Shared binding contract; never repair or guess an observed target name."""
import re


def plain_target_name(value):
    return (isinstance(value, str) and value == value.strip()
        and re.fullmatch(r"[A-Za-z][A-Za-z '-]{0,63}", value) is not None)
