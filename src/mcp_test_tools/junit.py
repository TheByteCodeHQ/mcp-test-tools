"""Parse JUnit XML (what pytest, Maven Surefire, TestNG and most runners can emit)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

FAILED_OUTCOMES = ("failed", "error")


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class

    test_id: str
    classname: str
    name: str
    outcome: str  # passed | failed | error | skipped
    duration: float
    message: str = ""
    details: str = ""
    output: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def parse_junit(path: Path) -> list[TestResult]:
    root = ET.parse(path).getroot()
    results: list[TestResult] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        outcome, message, details = "passed", "", ""
        for tag, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = case.find(tag)
            if child is not None:
                outcome, message, details = label, child.get("message", ""), _text(child)
                break
        output = "\n".join(part for part in (_text(case.find("system-out")), _text(case.find("system-err"))) if part)
        results.append(TestResult(
            test_id=f"{classname}::{name}" if classname else name,
            classname=classname, name=name, outcome=outcome,
            duration=float(case.get("time", "0") or 0),
            message=message, details=details, output=output,
        ))
    return results


def summarize(results: list[TestResult]) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for r in results:
        counts[r.outcome] += 1
    return {
        "total": len(results),
        **counts,
        "duration_seconds": round(sum(r.duration for r in results), 3),
    }
