from __future__ import annotations

from pipeline.chinatalk_writer import ReportOutput, build_report_prompt


def test_build_prompt_includes_subject_and_body():
    prompt = build_report_prompt(
        body="Speaker A: Hello\nSpeaker B: Hi",
        subject="ChinaTalk: Talking with Foo",
    )
    assert "ChinaTalk: Talking with Foo" in prompt
    assert "Speaker A: Hello" in prompt
    assert "Speaker B: Hi" in prompt


def test_report_output_dataclass_fields():
    r = ReportOutput(script="text", summary="sum")
    assert r.script == "text"
    assert r.summary == "sum"
