"""Drive the real widgets offscreen, with the prompts answered by the tests."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from conftest import stock
from partsdb.barcode import EOT, GS, RS, parse_scan
from partsdb.gui import MainWindow, ProjectsTab, ScanLineEdit, ScanTab

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
