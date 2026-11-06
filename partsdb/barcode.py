"""Parse the 2D barcodes printed on distributor bags.

DigiKey and Mouser labels follow ISO/IEC 15434 with ANSI MH10.8.2 data
identifiers: a ``[)>`` header, a format number, then fields split by the GS
control character. Each field starts with an identifier such as ``1P``.

Anything that doesn't start with the header is treated as plain text, which is
how hand-made labels and typed part numbers get in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GS = "\x1d"
RS = "\x1e"
EOT = "\x04"
FS = "\x1c"

# The scan input field shows control characters as Unicode "control picture"
# glyphs, and some scanners send those glyphs directly.
CONTROL_PICTURES = {"␝": GS, "␞": RS, "␄": EOT, "␜": FS}

_HEADER = re.compile(r"^\s*>?\[\)>\x1e?(\d{2})?")
_FIELD = re.compile(r"^(\d{0,3}[A-Z])(.*)$", re.DOTALL)
_DIGIKEY_ONLY = {"30P", "11Z", "12Z", "13Z", "20Z"}
_MOUSER_ONLY = {"14K", "1V"}


class BarcodeError(ValueError):
    """The scan looks like a 2D label but can't be read safely."""


@dataclass
class ScanLabel:
    raw: str
    structured: bool
    mpn: str | None = None
    supplier: str | None = None
    supplier_pn: str | None = None
    quantity: int | None = None
    manufacturer: str | None = None
    sales_order: str | None = None
    purchase_order: str | None = None
    invoice: str | None = None
    fields: dict[str, str] = field(default_factory=dict)


def normalize_controls(text: str) -> str:
    for picture, char in CONTROL_PICTURES.items():
        text = text.replace(picture, char)
    return text


def parse_scan(text: str) -> ScanLabel:
    """Turn raw scanner input into a ScanLabel.

    Raises BarcodeError for a 2D label with no separators, which means the
    scanner is dropping control characters. Guessing where fields end there
    would produce wrong part numbers and quantities.
    """
    raw = normalize_controls(text)
    header = _HEADER.match(raw)
    if not header:
        plain = raw.strip()
        if not plain:
            raise BarcodeError("Empty scan.")
        return ScanLabel(raw=plain, structured=False, mpn=plain)

    body = raw[header.end():].strip("\r\n" + EOT + RS + " ")
    if GS not in body:
        raise BarcodeError(
            "This is a 2D label, but its field separators are missing. The scanner "
            "is probably not sending control characters; check its keyboard settings."
        )

    fields: dict[str, str] = {}
    for chunk in re.split(f"[{GS}{RS}]", body):
        match = _FIELD.match(chunk)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()

    label = ScanLabel(raw=raw.strip(), structured=True, fields=fields)
    label.mpn = fields.get("1P") or None
    label.manufacturer = fields.get("1V") or None
    label.sales_order = fields.get("1K") or None
    label.purchase_order = fields.get("K") or None
    label.invoice = fields.get("11K") or None
    quantity = fields.get("Q", "")
    label.quantity = int(quantity) if quantity.isdigit() else None

    if _DIGIKEY_ONLY & fields.keys():
        label.supplier = "digikey"
        # When an order sets a customer reference it lands in P and 30P keeps the
        # DigiKey number, so 30P wins.
        label.supplier_pn = fields.get("30P") or fields.get("P") or None
    elif _MOUSER_ONLY & fields.keys():
        label.supplier = "mouser"
    return label
