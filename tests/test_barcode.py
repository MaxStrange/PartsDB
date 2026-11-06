"""Labels here are synthetic, built from the published field layout. Add real scans as they're collected."""

import pytest

from partsdb.barcode import EOT, GS, RS, BarcodeError, parse_scan

DIGIKEY = (
    f"[)>{RS}06{GS}PRMCF0402FT10K0CT-ND{GS}1PRMCF0402FT10K0{GS}30PRMCF0402FT10K0CT-ND{GS}KPO123"
    f"{GS}1K87654321{GS}10K99887766{GS}11K1{GS}4LCN{GS}Q100{GS}11ZPICK{GS}12Z123456{GS}13Z654321"
    f"{GS}20Z000000{RS}{EOT}"
)
MOUSER = f">[)>06{GS}KPO555{GS}14K001{GS}1PLM358DR{GS}Q25{GS}11K123456789{GS}4LMY{GS}1VTexas Instruments"


def test_digikey_label():
    label = parse_scan(DIGIKEY)
    assert label.structured
    assert label.supplier == "digikey"
    assert label.mpn == "RMCF0402FT10K0"
    assert label.supplier_pn == "RMCF0402FT10K0CT-ND"
    assert label.quantity == 100
    assert label.sales_order == "87654321"
    assert label.purchase_order == "PO123"


def test_customer_reference_in_p_does_not_replace_digikey_number():
    label = parse_scan(f"[)>{RS}06{GS}PMY-REF{GS}1PABC{GS}30PABC-ND{GS}Q5{RS}{EOT}")
    assert label.supplier_pn == "ABC-ND"


def test_mouser_label_without_record_separator():
    label = parse_scan(MOUSER)
    assert label.supplier == "mouser"
    assert label.mpn == "LM358DR"
    assert label.quantity == 25
    assert label.manufacturer == "Texas Instruments"


def test_control_picture_glyphs_are_understood():
    glyphs = DIGIKEY.replace(GS, "␝").replace(RS, "␞").replace(EOT, "␄")
    assert parse_scan(glyphs).mpn == "RMCF0402FT10K0"


def test_missing_separators_is_an_error_not_a_guess():
    with pytest.raises(BarcodeError, match="separators"):
        parse_scan("[)>06PABC1PABCQ100")


def test_older_labels_with_blank_trailing_separators():
    label = parse_scan(f"[)>{RS}06{GS}1PABC{GS}Q7{GS}{RS}{GS}{RS}{EOT}")
    assert label.mpn == "ABC"
    assert label.quantity == 7


def test_plain_text_is_a_part_number():
    label = parse_scan("  ADA-1234 \n")
    assert not label.structured
    assert label.mpn == "ADA-1234"
    assert label.quantity is None


def test_non_numeric_quantity_is_unknown():
    assert parse_scan(f"[)>06{GS}1PABC{GS}Qabc").quantity is None


def test_empty_scan():
    with pytest.raises(BarcodeError):
        parse_scan("   ")
