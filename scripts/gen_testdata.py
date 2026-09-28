"""Erzeugt Dummy-XML-Dateien mit Platzhaltern für Smoke- und Lasttests.

Beispiel:
    python scripts/gen_testdata.py --count 20000 --out testdata/sample
    python scripts/gen_testdata.py --count 280000 --out testdata/voll --per-folder 50000
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Messwertmeldung>
  <MessageId>{{{{uuid}}}}</MessageId>
  <Sequenz>{{{{seq}}}}</Sequenz>
  <Datei>{{{{filename}}}}</Datei>
  <Erstellt>{{{{timestamp}}}}</Erstellt>
  <Marktlokation>{malo}</Marktlokation>
  <Zaehlpunkt>DE0001234567890000000000{nr:09d}</Zaehlpunkt>
  <Messwert einheit="kWh">{value:.3f}</Messwert>
{padding}</Messwertmeldung>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=1000, help="Anzahl Dateien")
    parser.add_argument("--out", type=Path, default=Path("testdata/sample"), help="Zielordner")
    parser.add_argument("--per-folder", type=int, default=0, help="Dateien je Unterordner (0 = alles in einen Ordner)")
    parser.add_argument("--size-kb", type=float, default=0, help="Dateien auf ca. diese Größe auffüllen")
    args = parser.parse_args()

    rnd = random.Random(42)
    pad_line = "  <Position>" + "0" * 60 + "</Position>\n"
    width = len(str(args.count))
    for nr in range(1, args.count + 1):
        folder = args.out
        if args.per_folder:
            folder = folder / f"teil_{(nr - 1) // args.per_folder + 1:03d}"
        if nr == 1 or (args.per_folder and (nr - 1) % args.per_folder == 0):
            folder.mkdir(parents=True, exist_ok=True)
        fields = {"malo": f"5{nr:010d}", "nr": nr, "value": rnd.uniform(0, 500)}
        padding = ""
        if args.size_kb:
            missing = int(args.size_kb * 1024) - len(TEMPLATE.format(**fields, padding=""))
            padding = pad_line * max(0, missing // len(pad_line))
        (folder / f"msg_{nr:0{width}d}.xml").write_text(TEMPLATE.format(**fields, padding=padding), encoding="utf-8")
    print(f"{args.count} Dateien unter {args.out} erzeugt.")


if __name__ == "__main__":
    main()
