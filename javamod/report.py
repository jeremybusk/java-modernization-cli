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


def _cap_diff_stat(diff_stat: str, limit: int) -> str:
    """Keep the first *limit* file lines and git's totals line ("N files changed, ...")."""
    *files, totals = diff_stat.splitlines()
    if limit <= 0 or len(files) <= limit:
        return diff_stat
    return "\n".join(files[:limit] + [f" ... {len(files) - limit} more file(s) (full list in --report)", totals])


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
    residual_issues: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    build_log: str | None = None
    skipped_tests: list[str] = dataclasses.field(default_factory=list)
    agent: str | None = None
    agent_ok: bool | None = None
    agent_passes: int = 0
    agent_skills: list[str] = dataclasses.field(default_factory=list)
    agent_log: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    def print_summary(self, diff_stat_lines: int = 0) -> None:
        """Print the human summary; *diff_stat_lines* caps the changed-file list (0 = all)."""
        print()
        print(f"source:      {self.source}" + (f"  (ref: {self.source_ref})" if self.source_ref else ""))
        print(f"build:       {self.build_tool} @ {self.build_root}  -> Java {self.target_java}"
              + (f", Spring Boot {self.boot_target}" if self.boot_target else ""))
        print(f"profile:     {self.profile}   engine: {self.engine}")
        print(f"recipes:     {len(self.recipes)} applied" if self.recipes else "recipes:     none (ai-only engine)")
        if self.agent:
            passes = f"{self.agent_passes} passes, " if self.agent_passes > 1 else ""
            status = ("not run: build passed without it" if not self.agent_passes
                      else f"{passes}{'finished' if self.agent_ok else 'did NOT finish cleanly'}")
            print(f"agent:       {self.agent} ({status})"
                  + (f", skills: {', '.join(self.agent_skills)}" if self.agent_skills else "")
                  + (f"\n             transcript: {self.agent_log}" if self.agent_log else ""))
        print(f"changed:     {'yes' if self.changed else 'no'}")
        if self.diff_stat:
            print("\n" + _cap_diff_stat(self.diff_stat, diff_stat_lines))
        if self.build_ok is not None:
            print(f"\nbuild/test:  {'passed' if self.build_ok else 'FAILED'}")
            if self.skipped_tests:
                print(f"             excluding test(s): {', '.join(self.skipped_tests)}")
            if not self.build_ok:
                print(self.build_output_tail)
                if self.build_log:
                    print(f"\nfull build log: {self.build_log}")
                if self.residual_issues:
                    print(f"\n({len(self.residual_issues)} residual issue(s) triaged above)")
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
