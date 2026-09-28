from datetime import datetime, timezone

from cpiload.templating import TemplateContext, render_bytes, render_text


def _ctx() -> TemplateContext:
    return TemplateContext(
        seq=7,
        filename="MSCONS_0007.xml",
        uuid="11111111-2222-3333-4444-555555555555",
        now=datetime(2026, 9, 28, 12, 0, 0, 123000, tzinfo=timezone.utc),
    )


def test_all_placeholders_rendered():
    text = "{{uuid}}|{{ seq }}|{{filename}}|{{timestamp}}|{{epoch_ms}}"
    assert render_text(text, _ctx()) == (
        "11111111-2222-3333-4444-555555555555|7|MSCONS_0007.xml|2026-09-28T12:00:00.123Z|1790596800123"
    )


def test_unknown_placeholders_untouched():
    assert render_text("{{foo}} {{uuid}}", _ctx()) == "{{foo}} 11111111-2222-3333-4444-555555555555"


def test_body_and_header_share_values():
    ctx = TemplateContext(seq=1, filename="a.xml")
    assert render_text("{{uuid}}", ctx) == render_bytes(b"{{uuid}}", ctx).decode()


def test_fast_path_returns_same_object():
    body = b"<a>kein Platzhalter</a>"
    assert render_bytes(body, _ctx()) is body


def test_binary_passthrough():
    body = b"\xff\xfe{{uuid}}\x00"
    assert render_bytes(body, _ctx()) == body


def test_utf8_umlauts_preserved():
    assert render_bytes("Zählpunkt {{seq}}".encode(), _ctx()) == "Zählpunkt 7".encode()
