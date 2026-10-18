"""Parsing `text/event-stream` bodies in tests (P2-05 R2)."""

from __future__ import annotations


def parse(text: str) -> list[dict[str, str]]:
    """The frames of an event stream, each as {field: value}; comments are kept as ':'."""
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            name, _, value = line.partition(":")
            frame[name or ":"] = value.removeprefix(" ")
        frames.append(frame)
    return frames
