"""Readable terminal output shared by embedded apps and the command client."""
from __future__ import annotations

import re
import sys
from threading import Lock

from .models import RepairEvent

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class TerminalReporter:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self._lock = Lock()

    def __call__(self, event: RepairEvent) -> None:
        label = event.phase.upper().replace("_", " ")
        identifier = f" [{event.proposal_id}]" if event.proposal_id else ""
        output = f"\n[SELFHEAL {label}]{identifier}\n{event.message}\n"
        if event.diff:
            output += "\n" + event.diff + ("" if event.diff.endswith("\n") else "\n")
        output = _CONTROL.sub("", output)
        with self._lock:
            try:
                self.stream.write(output)
            except UnicodeEncodeError:
                encoding = getattr(self.stream, "encoding", None) or "utf-8"
                self.stream.write(output.encode(encoding, errors="replace").decode(encoding))
            self.stream.flush()
