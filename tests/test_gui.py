"""Drive the real widgets offscreen, with the prompts answered by the tests."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from conftest import stock
from partsdb.barcode import EOT, GS, RS, parse_scan
from partsdb.gui import AddPartDialog, InventoryTab, LocationCombo, MainWindow, ProjectsTab, ScanLineEdit, ScanTab

LABEL = f"[)>{RS}06{GS}1PABC{GS}30PABC-ND{GS}Q10{RS}{EOT}"


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def scan_tab(app, db):
    tab = ScanTab(db)
    tab.ask_quantity = lambda *a: pytest.fail("unexpected quantity prompt")
    tab.confirm = lambda *a: pytest.fail("unexpected confirm prompt")
    tab.ask_substitute = lambda *a: pytest.fail("unexpected substitute prompt")
    tab.ask_did_you_mean = lambda *a: pytest.fail("unexpected did-you-mean prompt")
    return tab


def do_scan(tab, text):
    tab.input.setText(text)
    tab.on_scan()


def test_scan_field_keeps_control_characters(app):
    field = ScanLineEdit()
    QTest.keyClicks(field, "[)>")
    QTest.keyClick(field, Qt.Key.Key_AsciiCircum, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(field, "06")
    QTest.keyClick(field, Qt.Key.Key_BracketRight, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(field, "1PABC")
    QTest.keyClick(field, Qt.Key.Key_BracketRight, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(field, "Q10")
    QTest.keyClick(field, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    label = parse_scan(field.text())
    assert (label.mpn, label.quantity) == ("ABC", 10)


def test_scan_field_keeps_raw_control_text(app):
    field = ScanLineEdit()
    QTest.keyClicks(field, "A")
    QTest.keyClick(field, Qt.Key.Key_BracketRight, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(field, "B")
    assert field.text() == "A␝B"


def test_scan_in_fills_expected_line_with_no_prompts(scan_tab, db, make_project):
    project = make_project("p", [("ABC", 4)])
    db.commit_project(project.id)
    do_scan(scan_tab, LABEL)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (4, 0)
    assert db.find_part(mpn="ABC").on_hand == 10
    assert "4 reserved for p" in scan_tab.log.item(0).text()
    assert "6 to free stock" in scan_tab.log.item(0).text()


def test_scan_in_with_nothing_open_goes_to_free_stock(scan_tab, db):
    do_scan(scan_tab, LABEL)
    assert db.find_part(mpn="ABC").available == 10


def test_repeat_label_can_be_refused_or_accepted(scan_tab, db):
    do_scan(scan_tab, LABEL)
    scan_tab.confirm = lambda *a: False
    do_scan(scan_tab, LABEL)
    assert db.find_part(mpn="ABC").on_hand == 10
    scan_tab.confirm = lambda *a: True
    do_scan(scan_tab, LABEL)
    assert db.find_part(mpn="ABC").on_hand == 20


def test_unexpected_part_can_be_a_substitute(scan_tab, db, make_project):
    project = make_project("p", [("WANTED", 4)])
    db.commit_project(project.id)
    line_id = db.get_lines(project.id)[0].id
    scan_tab.ask_substitute = lambda mpn, lines: (True, lines[0].line_id)
    do_scan(scan_tab, LABEL)
    line = db.get_lines(project.id)[0]
    assert line.id == line_id
    assert (line.reserved, line.pending, line.substitutes) == (4, 0, ["ABC"])


def test_cancelling_substitute_prompt_adds_nothing(scan_tab, db, make_project):
    db.commit_project(make_project("p", [("WANTED", 4)]).id)
    scan_tab.ask_substitute = lambda mpn, lines: (False, None)
    do_scan(scan_tab, LABEL)
    assert db.find_part(mpn="ABC") is None


def test_plain_scan_asks_for_quantity(scan_tab, db):
    scan_tab.ask_quantity = lambda *a: 3
    do_scan(scan_tab, "ADA-1234")
    assert db.find_part(mpn="ADA-1234").on_hand == 3


def test_scan_out_ignores_label_quantity_and_defaults_to_reserved(scan_tab, db, make_project):
    part = stock(db, "ABC", 50)
    project = make_project("p", [("ABC", 4)])
    db.commit_project(project.id)
    scan_tab.refresh()
    scan_tab.scan_out_mode.setChecked(True)
    scan_tab.project.setCurrentIndex(scan_tab.project.findData(project.id))
    seen = []
    scan_tab.ask_quantity = lambda title, prompt, default: seen.append(default) or 5
    do_scan(scan_tab, LABEL)
    assert seen == [4]
    assert db.get_part(part.id).on_hand == 45
    assert db.get_lines(project.id)[0].used == 5


def test_scan_out_part_not_on_bom_needs_confirmation(scan_tab, db, make_project):
    part = stock(db, "ABC", 5)
    project = make_project("p", [("OTHER", 1)])
    db.commit_project(project.id)
    scan_tab.refresh()
    scan_tab.scan_out_mode.setChecked(True)
    scan_tab.project.setCurrentIndex(scan_tab.project.findData(project.id))
    scan_tab.confirm = lambda *a: False
    do_scan(scan_tab, LABEL)
    assert db.get_part(part.id).on_hand == 5


def test_projects_tab_edits_write_through(app, db, make_project):
    stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 5)])
    tab = ProjectsTab(db)
    tab.refresh(project.id)
    assert tab.table.item(0, tab.C_STATUS).text() == "From stock"
    tab.table.item(0, tab.C_FROM_STOCK).setText("2")
    assert db.get_lines(project.id)[0].from_stock == 2
    QApplication.processEvents()
    tab.load_lines()
    assert tab.table.item(0, tab.C_STATUS).text() == "Order 3"
    assert tab.table.item(0, tab.C_ORDER).text() == "3"


def test_main_window_tabs_all_refresh(app, db, make_project):
    stock(db, "ABC", 1)
    project = make_project("p", [("ABC", 1)])
    db.commit_project(project.id)
    window = MainWindow(db)
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
    assert window.inventory_tab.table.rowCount() == 1
    assert window.history_tab.table.rowCount() == 1
    assert window.scan_tab.project.count() == 2


def test_delete_button_works_on_reserved_project(app, db, make_project, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)
    tab = ProjectsTab(db)
    tab.refresh(project.id)
    assert tab.delete_button.isEnabled()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda parent, title, text: asked.append(text) or QMessageBox.StandardButton.Yes)
    tab.delete_button.click()
    assert "return to free stock" in asked[0]
    assert db.list_projects() == []
    assert tab.projects.count() == 0
    assert not tab.delete_button.isEnabled()
    assert db.find_part("ABC").available == 10


def test_add_part_dialog_fills_fields_from_a_scan(app, db):
    db.add_location("Drawer 3")
    dialog = AddPartDialog(db)
    dialog.show()
    dialog.scan.setFocus()
    dialog.scan.setText(LABEL.replace(GS, "␝").replace(RS, "␞").replace(EOT, "␄"))
    QTest.keyClick(dialog.scan, Qt.Key.Key_Return)
    assert dialog.result() == 0 and dialog.isVisible()  # Enter from the scanner doesn't press OK
    assert dialog.mpn.text() == "ABC"
    assert dialog.supplier_pn.text() == "ABC-ND"
    assert dialog.supplier_label.text() == "DigiKey PN"
    assert dialog.scan.text() == ""
    dialog.location.setEditText("drawer 3")
    dialog.on_ok()
    assert dialog.result() == 1
    tab = InventoryTab(db)
    tab.save_new_part(dialog.values())
    dialog.close()
    part = db.find_part(supplier_pn="ABC-ND")
    assert (part.mpn, part.location, part.on_hand) == ("ABC", "Drawer 3", 0)
    assert tab.table.currentRow() == 0


def test_add_part_dialog_plain_text_and_bad_scan(app, db):
    dialog = AddPartDialog(db)
    dialog.scan.setText(f"[)>{RS}06 1PABC Q10")
    dialog.on_scan()
    assert "separators" in dialog.status.text()
    assert dialog.mpn.text() == ""
    dialog.on_ok()
    assert dialog.result() == 0
    dialog.scan.setText("LM358DR")
    dialog.on_scan()
    assert dialog.mpn.text() == "LM358DR"
    assert dialog.values()["supplier_pn"] is None


def test_location_combo_filters_ignoring_case(app, db):
    for name in ("Drawer 3", "HelloWorld drawer", "Bin A"):
        db.add_location(name)
    combo = LocationCombo(db)
    names = [combo.itemText(i) for i in range(combo.count()) if combo.itemText(i)]
    assert names == ["Bin A", "Drawer 3", "HelloWorld drawer", LocationCombo.ADD_TEXT]
    completer = combo.completer()
    completer.setCompletionPrefix("DRAW")
    matches = [completer.completionModel().index(i, 0).data() for i in range(completer.completionCount())]
    assert matches == ["Drawer 3", "HelloWorld drawer"]


def test_location_combo_add_new_entry(app, db):
    db.add_location("Drawer 3")
    combo = LocationCombo(db)
    combo.setEditText("shelf 2")
    asked = []
    combo.ask_new_name = lambda suggestion: asked.append(suggestion) or "Shelf 2"
    combo.activated.emit(combo.count() - 1)
    assert asked == ["shelf 2"]
    assert db.list_locations() == ["Drawer 3", "Shelf 2"]
    assert combo.currentText() == "Shelf 2"
    # Cancelling the name prompt puts back what was there.
    combo.ask_new_name = lambda suggestion: None
    combo.activated.emit(combo.count() - 1)
    assert combo.currentText() == "Shelf 2"


def test_location_combo_unknown_text_needs_confirmation(app, db):
    db.add_location("Drawer 3")
    combo = LocationCombo(db, "drawer  3")
    combo.confirm_add = lambda name: pytest.fail("existing location shouldn't prompt")
    assert combo.resolve() == "Drawer 3"
    combo.setEditText("HelloWorld caps")
    combo.confirm_add = lambda name: False
    assert combo.resolve() is None
    assert db.list_locations() == ["Drawer 3"]
    combo.confirm_add = lambda name: True
    assert combo.resolve() == "HelloWorld caps"
    combo.setEditText("  ")
    assert combo.resolve() == ""


def test_inventory_sets_location_for_selected_parts(app, db):
    db.add_location("Drawer 3")
    for mpn in ("A", "B", "C"):
        db.add_part(mpn)
    tab = InventoryTab(db)
    tab.table.selectRow(0)
    tab.table.selectionModel().select(
        tab.table.model().index(2, 0),
        tab.table.selectionModel().SelectionFlag.Select | tab.table.selectionModel().SelectionFlag.Rows,
    )
    asked = []
    tab.ask_location = lambda title, current: asked.append((title, current)) or "Drawer 3"
    tab.set_location()
    assert asked == [("Where are these 2 parts kept?", "")]
    assert [p.location for p in db.list_parts()] == ["Drawer 3", "", "Drawer 3"]
    assert tab.table.item(0, tab.C_LOCATION).text() == "Drawer 3"
    assert not tab.table.item(0, tab.C_LOCATION).flags() & Qt.ItemFlag.ItemIsEditable


def test_inventory_search_finds_a_scanned_label(app, db):
    for mpn in ("ABC", "DEF"):
        db.add_part(mpn)
    tab = InventoryTab(db)
    tab.search.setText(LABEL.replace(GS, "␝").replace(RS, "␞").replace(EOT, "␄"))
    assert tab.table.rowCount() == 2  # waits for typing to pause
    QTest.qWait(300)
    assert [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())] == ["ABC"]
    tab.search.setText("de")
    tab.search.returnPressed.emit()  # Enter searches straight away
    assert [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())] == ["DEF"]


def test_scan_in_typo_offers_the_close_match(scan_tab, db, make_project):
    project = make_project("p", [("E3V3HBPC1A", 1)])
    db.commit_project(project.id)
    asked = []
    scan_tab.ask_did_you_mean = lambda mpn, similar: asked.append((mpn, similar)) or similar[0][0]
    scan_tab.ask_quantity = lambda *a: 4
    do_scan(scan_tab, "E3VHBPC1A")
    assert asked[0][0] == "E3VHBPC1A"
    assert db.find_part(mpn="E3VHBPC1A") is None
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.substitutes) == (1, [])
    assert db.find_part(mpn="E3V3HBPC1A").on_hand == 4


def test_scan_in_typo_can_be_kept_or_cancelled(scan_tab, db):
    db.add_part("E3V3HBPC1A")
    scan_tab.ask_did_you_mean = lambda mpn, similar: None
    do_scan(scan_tab, "E3VHBPC1A")
    assert db.find_part(mpn="E3VHBPC1A") is None
    scan_tab.ask_did_you_mean = lambda mpn, similar: mpn
    scan_tab.ask_quantity = lambda *a: 2
    do_scan(scan_tab, "E3VHBPC1A")
    assert db.find_part(mpn="E3VHBPC1A").on_hand == 2


def test_inventory_merge_button(app, db, make_project):
    project = make_project("p", [("E3V3HBPC1A", 1)])
    db.commit_project(project.id)
    wrong = db.receive("E3VHBPC1A", 4, substitute_for_line=db.get_lines(project.id)[0].id).part
    tab = InventoryTab(db)
    row = next(r for r in range(tab.table.rowCount()) if tab.table.item(r, 0).text() == "E3VHBPC1A")
    tab.table.selectRow(row)
    tab.ask_merge_target = lambda part: "E3V3HBPC1A"
    tab.confirm_merge = lambda part, target: True
    tab.merge()
    assert [tab.table.item(r, 0).text() for r in range(tab.table.rowCount())] == ["E3V3HBPC1A"]
    assert tab.table.currentRow() == 0
    assert db.find_part(mpn="E3V3HBPC1A").on_hand == 4
    assert db.find_part(mpn="E3VHBPC1A") is None and wrong.id != tab.table.item(0, 0).data(Qt.ItemDataRole.UserRole)


def test_merge_dialog_lists_close_matches_first(app, db):
    for mpn in ("AAAA1", "E3V3HBPC1A", "ZZZZ9"):
        db.add_part(mpn)
    wrong = db.add_part("E3VHBPC1A")
    from partsdb.gui import MergeDialog

    dialog = MergeDialog(db, wrong)
    assert dialog.names == ["E3V3HBPC1A", "AAAA1", "ZZZZ9"]
    assert dialog.combo.currentText() == "E3V3HBPC1A"
    dialog.combo.setEditText("nonsense")
    dialog.on_ok()
    assert dialog.choice is None and "from the list" in dialog.error.text()
    dialog.combo.setEditText("zzzz9")
    dialog.on_ok()
    assert dialog.choice == "ZZZZ9"


def _inventory_with(db, *mpns):
    for mpn in mpns:
        db.add_part(mpn)
    tab = InventoryTab(db)
    tab.show()
    return tab


def test_copy_menu_for_one_row(app, db):
    from partsdb.gui import _copy_actions

    tab = _inventory_with(db, "ABC", "DEF")
    tab.table.selectRow(1)
    actions = _copy_actions(tab.table, 1, 0)
    assert actions[0] == ("Copy MPN: DEF", "DEF")
    label, row_text = actions[1]
    assert label == "Copy row" and row_text.split("\t")[0] == "DEF"
    assert len(row_text.split("\t")) == len(tab.COLUMNS)


def test_copy_menu_for_several_rows_and_unselected_click(app, db):
    from partsdb.gui import _copy_actions

    tab = _inventory_with(db, "ABC", "DEF", "GHI")
    model = tab.table.selectionModel()
    tab.table.selectRow(0)
    model.select(tab.table.model().index(2, 0), model.SelectionFlag.Select | model.SelectionFlag.Rows)
    assert _copy_actions(tab.table, 2, 0)[0] == ("Copy MPN (2 rows)", "ABC\nGHI")
    # Right-clicking a row outside the selection copies just that row.
    assert _copy_actions(tab.table, 1, 0)[0] == ("Copy MPN: DEF", "DEF")


def test_ctrl_c_copies_the_clicked_column(app, db):
    tab = _inventory_with(db, "ABC", "DEF")
    db_part = db.find_part(mpn="DEF")
    db.add_location("Drawer 3")
    db.update_part(db_part.id, location="Drawer 3")
    tab.refresh()
    tab.table.setCurrentCell(1, tab.C_LOCATION)
    tab.table.setFocus()
    QApplication.clipboard().clear()
    QTest.keyClick(tab.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "Drawer 3"


def test_copy_from_lists_and_hidden_columns(app, db, make_project, scan_tab):
    from partsdb.gui import _copy_actions, _copy_selection

    scan_tab._log("first line")
    scan_tab._log("second line")
    scan_tab.log.setCurrentRow(0)
    assert _copy_selection(scan_tab.log) == "second line"
    assert _copy_actions(scan_tab.log, 1) == [("Copy", "first line")]

    project = make_project("p", [("ABC", 1)])
    tab = ProjectsTab(db)
    tab.refresh(project.id)
    row_text = _copy_actions(tab.table, 0, 0)[-1][1]
    visible = [c for c in range(tab.table.columnCount()) if not tab.table.isColumnHidden(c)]
    assert len(row_text.split("\t")) == len(visible) < tab.table.columnCount()


def test_inventory_use_for_project(app, db, make_project, monkeypatch):
    from partsdb.gui import UseStockDialog

    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)
    part = db.find_part(mpn="ABC")
    db.adjust_stock(part.id, 3)
    tab = InventoryTab(db)
    tab.table.selectRow(0)
    seen = []

    def answer(part, lines):
        dialog = UseStockDialog(part, lines)
        seen.append((dialog.qty.maximum(), dialog.qty.value(), dialog.line.currentText()))
        return dialog.choice()

    tab.ask_use_stock = answer
    tab.use_for_project()
    assert seen == [(3, 3, "p: R1, waiting on 5")]
    assert (db.get_lines(project.id)[0].reserved, db.get_lines(project.id)[0].pending) == (3, 2)
    assert tab.table.item(0, tab.COLUMNS.index("On order")).text() == "2"

    errors = []
    monkeypatch.setattr("partsdb.gui._show_error", lambda parent, error: errors.append(str(error)))
    tab.use_for_project()  # no free stock left
    assert "None of ABC are free" in errors[-1]
    db.add_part("XYZ")
    tab.refresh()
    tab.table.selectRow(1)
    tab.use_for_project()
    assert "No project is waiting on XYZ" in errors[-1]


def test_projects_tab_fill_from_stock(app, db, make_project):
    project = make_project("p", [("AAA", 4), ("BBB", 2)])
    db.commit_project(project.id)
    db.adjust_stock(db.find_part(mpn="AAA").id, 10)
    tab = ProjectsTab(db)
    tab.refresh(project.id)
    assert tab.fill_button.isEnabled()
    shown = []
    tab.confirm_fill = lambda project, plan: shown.append(tab._describe_fill(plan)) or True
    tab.fill_button.click()
    assert "Reserve 4 parts from free stock for 1 lines" in shown[0]
    assert "keep waiting on 2 parts" in shown[0]
    assert [line.pending for line in db.get_lines(project.id)] == [0, 2]
    notes = []
    tab.notify = notes.append
    tab.fill_button.click()
    assert "none of the 1 waiting lines" in notes[0]
    db.receive("BBB", 2)
    tab.refresh(project.id)
    assert not tab.fill_button.isEnabled()


def test_scanning_an_alternate_needs_no_substitute_prompt(scan_tab, db, make_project):
    project = make_project("p", [("0527452096;59453-082110ECHLF", 1)])
    db.commit_project(project.id)
    scan_tab.ask_quantity = lambda *a: 1
    do_scan(scan_tab, "59453-082110ECHLF")
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending, line.substitutes) == (1, 0, [])
