"""Platzhalter pro Request: `{{uuid}}`, `{{seq}}`, `{{filename}}`, `{{timestamp}}`, `{{epoch_ms}}`.

Innerhalb eines Requests haben Body und Header dieselben Werte – so lässt sich
z. B. dieselbe UUID in `SAP_ApplicationID` und im Payload wiederfinden.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

PLACEHOLDERS = ("uuid", "seq", "filename", "timestamp", "epoch_ms")
_PATTERN = re.compile(r"\{\{\s*(" + "|".join(PLACEHOLDERS) + r")\s*\}\}")


@dataclass
class TemplateContext:
    seq: int
    filename: str
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def values(self) -> dict[str, str]:
        return {
            "uuid": self.uuid,
            "seq": str(self.seq),
            "filename": self.filename,
            "timestamp": self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "epoch_ms": str(int(self.now.timestamp() * 1000)),
        }


def render_text(text: str, ctx: TemplateContext) -> str:
    if "{{" not in text:
        return text
    values = ctx.values()
    return _PATTERN.sub(lambda m: values[m.group(1)], text)


def render_bytes(body: bytes, ctx: TemplateContext) -> bytes:
    """Ersetzt Platzhalter in UTF-8-Inhalten; Binärdaten bleiben unverändert."""
    if b"{{" not in body:
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    return render_text(text, ctx).encode("utf-8")
