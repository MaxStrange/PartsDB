"""Read BOM CSV files exported from KiCad.

Column layout depends on the user's export settings, so columns are found by
name, and any preamble above the header row is skipped.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

_ALIASES = {
    "refs": {"reference", "references", "ref", "refs", "designator", "designators", "refdes"},
    "qty": {"qty", "qnty", "quantity"},
    "value": {"value", "val"},
    "footprint": {"footprint", "package"},
    "description": {"description", "desc"},
    "mpn": {
        "mpn", "manufacturerpartnumber", "manufacturerpn", "manufacturerpartno", "manfpartnumber",
        "mfrpartnumber", "mfrpn", "mfrno", "mfgpartnumber", "mfgpn", "partnumber",
    },
    "manufacturer": {"manufacturer", "manufacturername", "mfr", "mfg"},
    "digikey_pn": {"digikey", "digikeypn", "digikeypartnumber", "digikeypartno", "digikeyno", "dkpn"},
    "mouser_pn": {"mouser", "mouserpn", "mouserpartnumber", "mouserpartno", "mouserno"},
    "dnp": {"dnp", "donotpopulate", "donotplace"},
    "excluded": {"excludefrombom", "excludedfrombom", "inbom"},
}
_FALSEY = {"", "0", "no", "n", "false"}


@dataclass
class BomRow:
    refs: list[str]
    qty: int
    value: str = ""
    footprint: str = ""
    description: str = ""
    mpn: str = ""
    manufacturer: str = ""
    digikey_pn: str = ""
    mouser_pn: str = ""
    dnp: bool = False


@dataclass
class BomFile:
    rows: list[BomRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BomError(ValueError):
    """The file can't be understood as a BOM."""


def _column_key(name: str) -> str | None:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    for key, aliases in _ALIASES.items():
        if cleaned in aliases:
            return key
    return None


def _natural(ref: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", ref)]


def _split_refs(text: str) -> list[str]:
    """Split a reference cell, expanding KiCad's ranges: "R1-R3,R7" is R1, R2, R3, R7."""
    refs: list[str] = []
    for token in re.split(r"[,;\s]+", text):
        match = re.fullmatch(r"([A-Za-z_]+)(\d+)-\1(\d+)", token)
        if match and int(match.group(2)) < int(match.group(3)):
            refs.extend(f"{match.group(1)}{n}" for n in range(int(match.group(2)), int(match.group(3)) + 1))
        elif token:
            refs.append(token)
    return refs


def parse_bom_text(text: str) -> BomFile:
    text = text.lstrip("﻿")
    # A preamble can confuse the sniffer, and the delimiter barely varies in
    # practice, so pick whichever delimiter finds a usable header row.
    columns: dict[str, int] | None = None
    table: list[list[str]] = []
    start = 0
    for delimiter in (",", ";", "\t"):
        table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        for index, row in enumerate(table):
            found: dict[str, int] = {}
            for col, name in enumerate(row):
                key = _column_key(name)
                if key and key not in found:
                    found[key] = col
            if "refs" in found:
                columns, start = found, index + 1
                break
        if columns is not None:
            break
    if columns is None:
        raise BomError("No header row with a Reference column was found.")

    result = BomFile()
    if "mpn" not in columns:
        result.warnings.append("No MPN column found. Add MPNs in the app, or add an MPN field to your KiCad symbols.")

    def cell(row: list[str], key: str) -> str:
        col = columns.get(key)
        return row[col].strip() if col is not None and col < len(row) else ""

    merged: dict[tuple, BomRow] = {}
    joined: list[str] = []
    excluded_count = 0
    in_bom_column = "excluded" in columns and re.sub(r"[^a-z]", "", table[start - 1][columns["excluded"]].lower()) == "inbom"
    for number, row in enumerate(table[start:], start=start + 1):
        refs = _split_refs(cell(row, "refs"))
        qty_text = cell(row, "qty")
        if not refs and not qty_text:
            continue
        flag = cell(row, "excluded").lower()
        # KiCad's "Exclude from BOM" column reads "Excluded from BOM" for excluded symbols.
        # Those aren't bought or fitted, so they don't belong on a project at all.
        if (flag in _FALSEY) == in_bom_column:
            excluded_count += 1
            continue
        if qty_text and not qty_text.isdigit():
            result.warnings.append(f"Row {number}: quantity {qty_text!r} isn't a whole number; skipped.")
            continue
        qty = int(qty_text) if qty_text else len(refs)
        if qty_text and refs and qty != len(refs):
            result.warnings.append(f"Row {number}: quantity {qty} but {len(refs)} references; using {qty}.")

        value = cell(row, "value")
        parsed = BomRow(
            refs=refs,
            qty=qty,
            value=value,
            footprint=cell(row, "footprint"),
            description=cell(row, "description"),
            mpn=cell(row, "mpn"),
            manufacturer=cell(row, "manufacturer"),
            digikey_pn=cell(row, "digikey_pn"),
            mouser_pn=cell(row, "mouser_pn"),
            dnp=cell(row, "dnp").lower() not in _FALSEY or value.upper() == "DNP",
        )
        if "," in parsed.mpn:
            joined.append(f"{', '.join(refs)} ({parsed.mpn})")
            continue
        # One line per distinct part, whether or not the export grouped rows.
        if parsed.mpn:
            key = ("mpn", parsed.mpn.upper(), parsed.dnp)
        else:
            key = ("value", parsed.value, parsed.footprint, parsed.dnp)
        if key in merged:
            merged[key].refs.extend(refs)
            merged[key].qty += qty
        else:
            merged[key] = parsed

    if excluded_count:
        result.warnings.append(f"Skipped {excluded_count} rows marked \"Exclude from BOM\".")
    if joined:
        raise BomError(
            "Some rows have several part numbers joined by commas. KiCad does this when it groups "
            "symbols with different MPNs into one row, so it isn't known which reference uses which part. "
            "Export again grouped by MPN (or ungrouped) and import that. Affected rows: " + "; ".join(joined)
            + ". To list deliberate alternates in one symbol, separate them with ';' instead."
        )
    for bom_row in merged.values():
        bom_row.refs.sort(key=_natural)
    result.rows = list(merged.values())
    return result


def parse_bom_file(path: str | Path) -> BomFile:
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return parse_bom_text(text)
