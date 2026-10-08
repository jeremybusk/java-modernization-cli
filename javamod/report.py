"""A short human summary printed at the end of every run, plus the same facts
as JSON for scripting. There is no retained run history, approval queue, or
portfolio state -- one run prints what it did and exits; rerun it to retry.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class RunReport:
    source: str
    source_ref: str | None
    build_tool: str
    build_root: str
    target_java: int
    boot_target: str | None
    profile: str
    engine: str
    recipes: list[str]
    changed: bool
    diff_stat: str
    build_ok: bool | None
    build_output_tail: str
    commit: str | None
    branch: str | None
    destination: str | None
    pushed: bool

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    def print_summary(self) -> None:
        print()
        print(f"source:      {self.source}" + (f"  (ref: {self.source_ref})" if self.source_ref else ""))
        print(f"build:       {self.build_tool} @ {self.build_root}  -> Java {self.target_java}"
              + (f", Spring Boot {self.boot_target}" if self.boot_target else ""))
        print(f"profile:     {self.profile}   engine: {self.engine}")
        print(f"recipes:     {len(self.recipes)} applied" if self.recipes else "recipes:     none (ai-only engine)")
        print(f"changed:     {'yes' if self.changed else 'no'}")
        if self.diff_stat:
            print("\n" + self.diff_stat)
        if self.build_ok is not None:
            print(f"\nbuild/test:  {'passed' if self.build_ok else 'FAILED'}")
            if not self.build_ok:
                print(self.build_output_tail[-2000:])
        if self.commit:
            print(f"\ncommit:      {self.commit}  (branch {self.branch})")
        if self.destination:
            print(f"destination: {self.destination}#{self.branch}  ({'pushed' if self.pushed else 'not pushed (plan only)'})")
        print()

    def write_json(self, destination: str) -> None:
        """Write the report as JSON to a file path, or to stdout if *destination* is '-'."""
        text = json.dumps(self.as_dict(), indent=2)
        if destination == "-":
            print(text)
            return
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
