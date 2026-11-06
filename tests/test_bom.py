import pytest

from partsdb.bom import BomError, parse_bom_file, parse_bom_text


def test_grouped_export():
    text = (
        '"Reference","Value","Footprint","Qty","DNP","MPN","Manufacturer"\n'
        '"R1,R2,R3","10k","R_0402","3","","RC0402-10K","Yageo"\n'
        '"C1","100n","C_0402","1","DNP","CL05-100N","Samsung"\n'
    )
    bom = parse_bom_text(text)
    assert not bom.warnings
    assert bom.rows[0].refs == ["R1", "R2", "R3"]
    assert bom.rows[0].qty == 3
    assert bom.rows[0].mpn == "RC0402-10K"
    assert not bom.rows[0].dnp
    assert bom.rows[1].dnp


def test_real_kicad_cli_default_output():
    # Header and row shape from kicad-cli 9 `sch export bom` with its default settings.
    text = (
        '"Refs","Value","Footprint","Qty","DNP"\n'
        '"C26","CC0402_10UF_6.3V_20%_X5R","Capacitors SMD:CAPC1005X70N","1",""\n'
        '"C29","CC0402_10UF_6.3V_20%_X5R","Capacitors SMD:CAPC1005X70N","1",""\n'
    )
    bom = parse_bom_text(text)
    assert bom.rows[0].refs == ["C26", "C29"]
    assert bom.rows[0].qty == 2


def test_reference_ranges_expand():
    bom = parse_bom_text('Reference,Qty,MPN\n"R1-R3,R7",4,ABC\n')
    assert bom.rows[0].refs == ["R1", "R2", "R3", "R7"]
    assert not bom.warnings


def test_hyphenated_reference_is_not_a_range():
    assert parse_bom_text("Reference,MPN\nU1-A,ABC\n").rows[0].refs == ["U1-A"]


def test_ungrouped_rows_merge_by_mpn_and_refs_sort_naturally():
    text = "Reference,Value,MPN\nR10,10k,RC-10K\nR2,10k,rc-10k\nR1,10k,RC-10K\n"
    bom = parse_bom_text(text)
    assert len(bom.rows) == 1
    assert bom.rows[0].qty == 3
    assert bom.rows[0].refs == ["R1", "R2", "R10"]


def test_rows_without_mpn_merge_by_value_and_footprint():
    text = "Reference,Value,Footprint\nR1,10k,0402\nR2,10k,0402\nR3,10k,0603\n"
    bom = parse_bom_text(text)
    assert [row.qty for row in bom.rows] == [2, 1]
    assert any("MPN" in warning for warning in bom.warnings)


def test_preamble_above_header_is_skipped():
    text = (
        'Source:,"C:\\board.kicad_sch"\nDate:,"2026-01-01"\nTool:,"Eeschema"\n\n'
        "Ref,Qnty,Value,Cmp name,Footprint,Description,Mfr. No.\n"
        '"U1","1","LM358","LM358","SOIC-8","Op amp","LM358DR"\n'
    )
    bom = parse_bom_text(text)
    assert bom.rows[0].refs == ["U1"]
    assert bom.rows[0].mpn == "LM358DR"
    assert bom.rows[0].description == "Op amp"


def test_semicolon_delimited():
    assert parse_bom_text("Reference;Value;MPN\nR1;10k;ABC\n").rows[0].mpn == "ABC"


def test_supplier_columns():
    row = parse_bom_text("Reference,MPN,DigiKey PN,Mouser Part Number\nR1,ABC,ABC-ND,123-ABC\n").rows[0]
    assert (row.digikey_pn, row.mouser_pn) == ("ABC-ND", "123-ABC")


def test_quantity_reference_mismatch_warns_and_keeps_quantity():
    bom = parse_bom_text("Reference,Qty,MPN\n\"R1,R2\",3,ABC\n")
    assert bom.rows[0].qty == 3
    assert "3" in bom.warnings[0]


def test_bad_quantity_row_is_skipped_with_warning():
    bom = parse_bom_text("Reference,Qty,MPN\nR1,lots,ABC\nR2,1,DEF\n")
    assert [row.mpn for row in bom.rows] == ["DEF"]
    assert "lots" in bom.warnings[0]


def test_footer_rows_are_ignored():
    assert len(parse_bom_text("Reference,Value,MPN\nR1,10k,ABC\n,,\n").rows) == 1


def test_no_header_is_an_error():
    with pytest.raises(BomError):
        parse_bom_text("a,b,c\n1,2,3\n")


def test_file_with_bom_marker_and_non_utf8(tmp_path):
    utf = tmp_path / "utf.csv"
    utf.write_bytes("\ufeffReference,MPN\nR1,ABC\n".encode("utf-8"))
    assert parse_bom_file(utf).rows[0].mpn == "ABC"
    latin = tmp_path / "latin.csv"
    latin.write_bytes("Reference,Value,MPN\nR1,10\xb5F,ABC\n".encode("cp1252"))
    assert parse_bom_file(latin).rows[0].value == "10\u00b5F"
