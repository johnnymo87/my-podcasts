import inspect

from pipeline import processor


def test_processor_calls_maybe_rewrite_transcript():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_transcript" in source
    assert "preset.feed_slug" in source


def test_processor_no_longer_calls_the_retired_gates():
    source = inspect.getsource(processor.process_email_bytes)
    assert "maybe_rewrite_chinatalk" not in source
    assert "maybe_rewrite_yglesias" not in source
