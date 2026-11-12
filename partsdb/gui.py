"""Qt interface. Workflow rules live in inventory.py; this file only presents them."""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, QStandardPaths, QStringListModel, Qt, QTimer
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton,
    QRadioButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from partsdb.barcode import EOT, FS, GS, RS, BarcodeError, ScanLabel, parse_scan
from partsdb.bom import BomError, parse_bom_file
from partsdb.inventory import CANCELLED, COMMITTED, COMPLETE, PLANNING, Inventory, Line, MoveResult, OpenLine, WorkflowError

_PICTURES = {GS: "␝", RS: "␞", EOT: "␄", FS: "␜"}
_GREEN = QColor(46, 160, 67, 60)
_AMBER = QColor(210, 153, 34, 70)
_RED = QColor(248, 81, 73, 70)
_GREY = QColor(128, 128, 128, 50)


class ScanLineEdit(QLineEdit):
    """Input field for a scanner acting as a keyboard.

    A stock QLineEdit discards control characters, but they separate the fields
    of a 2D label. Scanners typically send them as Ctrl+], Ctrl+^ and Ctrl+D, so
    they're kept here, shown as visible control-picture glyphs that the parser
    understands.
    """

    @staticmethod
    def _control_char(event: QKeyEvent) -> str | None:
        text = event.text()
        if len(text) == 1 and text in _PICTURES:
            return text
        modifiers = event.modifiers()
        # AltGr reports as Ctrl+Alt on Windows, and it types ordinary characters.
        if modifiers & Qt.KeyboardModifier.ControlModifier and not modifiers & Qt.KeyboardModifier.AltModifier:
            key = event.key()
            if key == Qt.Key.Key_BracketRight:
                return GS
            if key in (Qt.Key.Key_AsciiCircum, Qt.Key.Key_6):
                return RS
            if key == Qt.Key.Key_D:
                return EOT
            if key == Qt.Key.Key_Backslash:
                return FS
        return None

    def event(self, event: QEvent) -> bool:
        # Claim these before Qt can treat them as window shortcuts.
        if event.type() == QEvent.Type.ShortcutOverride and self._control_char(event):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        char = self._control_char(event)
        if char:
            self.insert(_PICTURES[char])
            return
        super().keyPressEvent(event)


def _show_error(parent: QWidget, error: Exception | str) -> None:
    QMessageBox.warning(parent, "PartsDB", str(error))


@contextmanager
def _filling(table: QTableWidget):
    """Fill a table in one go, then size its columns once.

    Resizing columns to their contents as each cell is set re-measures the
    whole table every time, which took seconds for a few hundred rows.
    """
    table.setUpdatesEnabled(False)
    table.blockSignals(True)
    try:
        yield
    finally:
        table.blockSignals(False)
        table.resizeColumnsToContents()
        table.setUpdatesEnabled(True)


def _selected_rows(view: QTableWidget | QListWidget) -> list[int]:
    if isinstance(view, QListWidget):
        rows = sorted(view.row(item) for item in view.selectedItems())
    else:
        rows = sorted({index.row() for index in view.selectionModel().selectedRows()})
    if not rows and view.currentRow() >= 0:
        rows = [view.currentRow()]
    return rows


def _cell_text(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    return item.text() if item else ""


def _row_text(table: QTableWidget, row: int) -> str:
    columns = [c for c in range(table.columnCount()) if not table.isColumnHidden(c)]
    return "\t".join(_cell_text(table, row, c) for c in columns)


def _copy_actions(view: QTableWidget | QListWidget, row: int, column: int = 0) -> list[tuple[str, str]]:
    """Menu entries for a right-click on (row, column), as (label, text to copy).

    If the clicked row is part of a multi-row selection, entries cover every
    selected row, one per line, so a list of MPNs can be copied in one go.
    """
    rows = _selected_rows(view)
    if row not in rows:
        rows = [row]
    if isinstance(view, QListWidget):
        text = "\n".join(view.item(r).text() for r in rows)
        return [("Copy" if len(rows) == 1 else f"Copy {len(rows)} lines", text)]
    header = view.horizontalHeaderItem(column)
    name = header.text() if header else "cell"
    if len(rows) == 1:
        value = _cell_text(view, row, column)
        shown = value if len(value) <= 40 else value[:37] + "..."
        actions = [(f"Copy {name}: {shown}", value)] if value else []
        return actions + [("Copy row", _row_text(view, row))]
    return [
        (f"Copy {name} ({len(rows)} rows)", "\n".join(_cell_text(view, r, column) for r in rows)),
        (f"Copy {len(rows)} rows", "\n".join(_row_text(view, r) for r in rows)),
    ]


def _copy_selection(view: QTableWidget | QListWidget) -> str:
    """What Ctrl+C copies: the current column of the selected rows, or the selected lines."""
    rows = _selected_rows(view)
    if isinstance(view, QListWidget):
        return "\n".join(view.item(r).text() for r in rows)
    column = max(view.currentColumn(), 0)
    return "\n".join(_cell_text(view, r, column) for r in rows)


def _make_copyable(view: QTableWidget | QListWidget) -> None:
    """Right-click to copy a field or row, and Ctrl+C to copy the clicked column."""

    def show_menu(pos) -> None:
        index = view.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(view)
        for label, text in _copy_actions(view, index.row(), index.column()):
            menu.addAction(label, lambda text=text: QApplication.clipboard().setText(text))
        menu.exec(view.viewport().mapToGlobal(pos))

    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    view.customContextMenuRequested.connect(show_menu)
    shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), view)
    shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
    shortcut.activated.connect(lambda: QApplication.clipboard().setText(_copy_selection(view)))


def _item(value, editable: bool = False, color: QColor | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem("" if value is None else str(value))
    flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
    if editable:
        flags |= Qt.ItemFlag.ItemIsEditable
    item.setFlags(flags)
    if isinstance(value, int):
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if color is not None:
        item.setBackground(color)
    return item


class ImportDialog(QDialog):
    def __init__(self, parent: QWidget, default_name: str, row_count: int, warnings: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Import BOM")
        form = QFormLayout(self)
        self.name = QLineEdit(default_name)
        self.boards = QSpinBox(minimum=1, maximum=100000)
        form.addRow("Project name", self.name)
        form.addRow("Boards to build", self.boards)
        form.addRow("Lines found", QLabel(str(row_count)))
        if warnings:
            label = QLabel("\n".join(warnings))
            label.setWordWrap(True)
            form.addRow("Warnings", label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class LocationCombo(QComboBox):
    """Pick a storage location from the list, so the same place is always spelled the same way.

    Typing filters the list, ignoring case. The last entry adds a new location.
    Leaving it empty means no location.
    """

    ADD_TEXT = "Add new location..."
    _ADD = "__add__"

    def __init__(self, db: Inventory, current: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.lineEdit().setPlaceholderText("Type to filter, or pick from the list")
        self._names = QStringListModel(self)
        completer = QCompleter(self._names, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(completer)
        self._last_text = current
        self.currentTextChanged.connect(self._remember_text)
        self.activated.connect(self._on_activated)
        self.reload(current)

    def reload(self, select: str = "") -> None:
        names = self.db.list_locations()
        self.blockSignals(True)
        self.clear()
        self.addItems(names)
        self.insertSeparator(self.count())
        self.addItem(self.ADD_TEXT, self._ADD)
        self._names.setStringList(names)
        self.setCurrentIndex(-1)
        self.setEditText(select)
        self._last_text = select
        self.blockSignals(False)

    def _remember_text(self, text: str) -> None:
        if text != self.ADD_TEXT:
            self._last_text = text

    def _on_activated(self, index: int) -> None:
        if self.itemData(index) != self._ADD:
            return
        typed = self._last_text.strip()
        suggestion = "" if self.db.find_location(typed) else typed
        name = self.ask_new_name(suggestion)
        if not name or not name.strip():
            previous = self._last_text  # clearing the selection below overwrites it
            self.setCurrentIndex(-1)
            self.setEditText(previous)
            return
        self.reload(self.db.add_location(name))

    def ask_new_name(self, suggestion: str) -> str | None:
        name, ok = QInputDialog.getText(self, "New location", "Location name:", text=suggestion)
        return name if ok else None

    def confirm_add(self, name: str) -> bool:
        answer = QMessageBox.question(
            self, "New location", f"\"{name}\" isn't in the locations list yet. Add it?"
        )
        return answer == QMessageBox.StandardButton.Yes

    def resolve(self) -> str | None:
        """The chosen location in its stored spelling, "" for none, or None if the
        user typed a name that isn't in the list and declined to add it."""
        text = " ".join(self.currentText().split())
        if not text:
            return ""
        existing = self.db.find_location(text)
        if existing:
            return existing
        if self.confirm_add(text):
            name = self.db.add_location(text)
            self.reload(name)
            return name
        return None


class LocationDialog(QDialog):
    """Set the location of one or more parts."""

    def __init__(self, db: Inventory, title: str, current: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Set location")
        form = QFormLayout(self)
        form.addRow(QLabel(title))
        self.combo = LocationCombo(db, current)
        self.combo.setMinimumWidth(280)
        form.addRow("Location", self.combo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.value: str | None = None

    def on_ok(self) -> None:
        self.value = self.combo.resolve()
        if self.value is not None:
            self.accept()


class UseStockDialog(QDialog):
    """Pick which waiting project line gets parts from free stock, and how many."""

    def __init__(self, part, lines: list[OpenLine], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Use stock for a project")
        self.part = part
        self.lines = lines
        form = QFormLayout(self)
        intro = QLabel(
            f"{part.available} of {part.mpn} are free in stock. Reserving some for a project means "
            "it stops waiting for that many to be delivered."
        )
        intro.setWordWrap(True)
        form.addRow(intro)
        self.line = QComboBox()
        for line in lines:
            self.line.addItem(f"{line.project_name}: {line.refs}, waiting on {line.pending}", line.line_id)
        self.qty = QSpinBox(minimum=1)
        self.line.currentIndexChanged.connect(self._update_qty)
        form.addRow("Project line", self.line)
        form.addRow("Quantity", self.qty)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._update_qty()

    def _update_qty(self) -> None:
        line = self.lines[self.line.currentIndex()]
        most = min(line.pending, self.part.available)
        self.qty.setMaximum(most)
        self.qty.setValue(most)

    def choice(self) -> tuple[int, int]:
        return self.line.currentData(), self.qty.value()


class MergeDialog(QDialog):
    """Pick the part that a wrongly numbered part should be folded into."""

    def __init__(self, db: Inventory, part, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Merge part")
        form = QFormLayout(self)
        intro = QLabel(
            f"Merge {part.mpn} into another part. Its stock ({part.on_hand}), reservations, supplier "
            f"numbers and history move to the part you pick, and {part.mpn} is deleted."
        )
        intro.setWordWrap(True)
        form.addRow(intro)
        close = [mpn for mpn, _ in db.similar_mpns(part.mpn)]
        others = [other.mpn for other in db.list_parts() if other.id != part.id]
        self.names = [mpn for mpn in close if mpn in others] + [mpn for mpn in others if mpn not in close]
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.addItems(self.names)
        completer = QCompleter(self.names, self.combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.combo.setCompleter(completer)
        self.combo.setMinimumWidth(280)
        form.addRow("Merge into", self.combo)
        self.error = QLabel("")
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.choice: str | None = None

    def on_ok(self) -> None:
        typed = self.combo.currentText().strip().upper()
        match = next((name for name in self.names if name.upper() == typed), None)
        if match is None:
            self.error.setText("Pick a part from the list.")
            return
        self.choice = match
        self.accept()


class AddPartDialog(QDialog):
    """Add a part by scanning its bag or typing its details."""

    def __init__(self, db: Inventory, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add part")
        self.supplier: str | None = None
        form = QFormLayout(self)
        self.scan = ScanLineEdit()
        self.scan.setPlaceholderText("Scan a bag here to fill in the fields below")
        self.status = QLabel("Stock starts at 0. Use Scan in or Adjust stock to add parts.")
        self.status.setWordWrap(True)
        self.mpn = QLineEdit()
        self.manufacturer = QLineEdit()
        self.description = QLineEdit()
        self.location = LocationCombo(db)
        self.supplier_pn = QLineEdit()
        self.supplier_label = QLabel("Supplier PN")
        form.addRow("Scan", self.scan)
        form.addRow(self.status)
        form.addRow("MPN", self.mpn)
        form.addRow("Manufacturer", self.manufacturer)
        form.addRow("Description", self.description)
        form.addRow("Location", self.location)
        form.addRow(self.supplier_label, self.supplier_pn)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.on_ok)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self.scan.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # A scanner ends with Enter. In the scan field that means "read this",
        # not "press OK" on a half-filled form.
        if self.focusWidget() is self.scan and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.on_scan()
            return
        super().keyPressEvent(event)

    def on_scan(self) -> None:
        text = self.scan.text()
        self.scan.clear()
        if not text.strip():
            return
        try:
            label = parse_scan(text)
        except BarcodeError as error:
            self.status.setText(str(error))
            return
        if not label.mpn:
            self.status.setText("That label has no manufacturer part number. Type it in below.")
            return
        self.mpn.setText(label.mpn)
        if label.manufacturer:
            self.manufacturer.setText(label.manufacturer)
        self.supplier = label.supplier if label.supplier_pn else None
        self.supplier_pn.setText(label.supplier_pn or "")
        self.supplier_label.setText({"digikey": "DigiKey PN", "mouser": "Mouser PN"}.get(self.supplier, "Supplier PN"))
        self.status.setText(f"Read {label.mpn}. Stock starts at 0; use Scan in or Adjust stock to add parts.")
        self.location.setFocus()

    def on_ok(self) -> None:
        if not self.mpn.text().strip():
            self.status.setText("Scan a bag or type an MPN first.")
            return
        location = self.location.resolve()
        if location is None:
            self.location.setFocus()
            return
        self.location.setEditText(location)
        self.accept()

    def values(self) -> dict:
        supplier_pn = self.supplier_pn.text().strip()
        return {
            "mpn": self.mpn.text().strip(),
            "manufacturer": self.manufacturer.text().strip(),
            "description": self.description.text().strip(),
            "location": " ".join(self.location.currentText().split()),
            "supplier": self.supplier if supplier_pn else None,
            "supplier_pn": supplier_pn or None,
        }


class SubstituteDialog(QDialog):
    """Ask where a part no BOM is waiting for should go."""

    def __init__(self, parent: QWidget, mpn: str, open_lines: list[OpenLine]):
        super().__init__(parent)
        self.setWindowTitle("Part not expected")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"No project is waiting for {mpn}."))
        self.to_stock = QRadioButton("Add to free stock")
        self.to_stock.setChecked(True)
        self.substitute = QRadioButton("Use as a substitute for:")
        group = QButtonGroup(self)
        group.addButton(self.to_stock)
        group.addButton(self.substitute)
        self.lines = QComboBox()
        for line in open_lines:
            self.lines.addItem(f"{line.project_name}: {line.mpn} ({line.refs}), waiting on {line.pending}", line.line_id)
        self.lines.setEnabled(False)
        self.substitute.toggled.connect(self.lines.setEnabled)
        for widget in (self.to_stock, self.substitute, self.lines):
            layout.addWidget(widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def substitute_line(self) -> int | None:
        return self.lines.currentData() if self.substitute.isChecked() else None


class ProjectsTab(QWidget):
    COLUMNS = ["DNP", "References", "Value", "Footprint", "MPN", "Manufacturer", "Need", "Free in stock",
               "Use from stock", "To order", "Reserved", "Pending", "Used", "Status"]
    (C_DNP, C_REFS, C_VALUE, C_FOOTPRINT, C_MPN, C_MFR, C_NEED, C_FREE,
     C_FROM_STOCK, C_ORDER, C_RESERVED, C_PENDING, C_USED, C_STATUS) = range(14)

    def __init__(self, db: Inventory):
        super().__init__()
        self.db = db
        self._loading = False

        self.projects = QListWidget()
        _make_copyable(self.projects)
        self.projects.currentRowChanged.connect(lambda _: self.load_lines())
        import_button = QPushButton("Import KiCad BOM...")
        import_button.clicked.connect(self.import_bom)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(import_button)
        left_layout.addWidget(self.projects)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        _make_copyable(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.on_item_changed)

        self.export_button = QPushButton("Export order CSV...")
        self.export_button.clicked.connect(self.export_csv)
        self.commit_button = QPushButton("Mark as ordered and reserve parts")
        self.commit_button.clicked.connect(self.commit)
        self.fill_button = QPushButton("Use free stock for waiting lines...")
        self.fill_button.setToolTip("Stop waiting on deliveries for parts that are already free in stock")
        self.fill_button.clicked.connect(self.fill_from_stock)
        self.complete_button = QPushButton("Complete project")
        self.complete_button.clicked.connect(lambda: self.finish(COMPLETE))
        self.cancel_button = QPushButton("Cancel project")
        self.cancel_button.clicked.connect(lambda: self.finish(CANCELLED))
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete)
        buttons = QHBoxLayout()
        for button in (self.export_button, self.commit_button, self.fill_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        for button in (self.complete_button, self.cancel_button, self.delete_button):
            buttons.addWidget(button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.summary)
        right_layout.addWidget(self.table, 1)
        right_layout.addLayout(buttons)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1000])
        QVBoxLayout(self).addWidget(splitter)
        self.refresh()

    def current_project(self):
        item = self.projects.currentItem()
        if item is None:
            return None
        try:
            return self.db.get_project(item.data(Qt.ItemDataRole.UserRole))
        except WorkflowError:  # just deleted; the list hasn't been rebuilt yet
            return None

    def refresh(self, select_id: int | None = None) -> None:
        current = self.current_project()
        select_id = select_id or (current.id if current else None)
        self.projects.blockSignals(True)
        self.projects.clear()
        row_to_select = 0
        for row, project in enumerate(self.db.list_projects()):
            self.projects.addItem(f"{project.name}  [{project.status}]")
            self.projects.item(row).setData(Qt.ItemDataRole.UserRole, project.id)
            if project.id == select_id:
                row_to_select = row
        if self.projects.count():
            self.projects.setCurrentRow(row_to_select)
        self.projects.blockSignals(False)
        self.load_lines()

    def load_lines(self) -> None:
        project = self.current_project()
        self._loading = True
        lines = self.db.get_lines(project.id) if project else []
        planning = bool(project) and project.status == PLANNING
        with _filling(self.table):
            self.table.setRowCount(0)
            self.table.setRowCount(len(lines))
            for row, line in enumerate(lines):
                self._fill_row(row, line, planning)
        self._loading = False
        for column in (self.C_FROM_STOCK, self.C_ORDER):
            self.table.setColumnHidden(column, not planning)
        for column in (self.C_RESERVED, self.C_PENDING, self.C_USED):
            self.table.setColumnHidden(column, planning)

        active = bool(project) and project.status in (PLANNING, COMMITTED)
        self.export_button.setEnabled(active)
        self.commit_button.setEnabled(planning)
        self.complete_button.setEnabled(bool(project) and project.status == COMMITTED)
        self.fill_button.setEnabled(bool(project) and project.status == COMMITTED and any(line.pending for line in lines))
        self.cancel_button.setEnabled(active)
        self.delete_button.setEnabled(bool(project))
        self.summary.setText(self._summary(project, lines))

    def _fill_row(self, row: int, line: Line, planning: bool) -> None:
        status, color = self._status(line, planning)
        dnp = _item("")
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        dnp.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable if planning else flags)
        dnp.setCheckState(Qt.CheckState.Checked if line.dnp else Qt.CheckState.Unchecked)
        dnp.setData(Qt.ItemDataRole.UserRole, line.id)
        self.table.setItem(row, self.C_DNP, dnp)
        cells = {
            self.C_REFS: _item(line.refs),
            self.C_VALUE: _item(line.value),
            self.C_FOOTPRINT: _item(line.footprint),
            self.C_MPN: _item(line.mpn, editable=planning, color=_RED if not line.mpn and not line.dnp else None),
            self.C_MFR: _item(line.manufacturer),
            self.C_NEED: _item(line.qty_required),
            self.C_FREE: _item(line.available, color=_RED if line.available < 0 else None),
            self.C_FROM_STOCK: _item(line.from_stock, editable=planning and not line.dnp),
            self.C_ORDER: _item(line.to_order if planning else 0),
            self.C_RESERVED: _item(line.reserved),
            self.C_PENDING: _item(line.pending),
            self.C_USED: _item(line.used),
            self.C_STATUS: _item(status, color=color),
        }
        for column, item in cells.items():
            self.table.setItem(row, column, item)

    @staticmethod
    def _status(line: Line, planning: bool) -> tuple[str, QColor | None]:
        if line.dnp:
            return "DNP", _GREY
        if planning:
            if not line.mpn:
                return "Needs MPN", _RED
            return (f"Order {line.to_order}", _AMBER) if line.to_order else ("From stock", _GREEN)
        suffix = f" (substitute: {', '.join(line.substitutes)})" if line.substitutes else ""
        if line.pending:
            return f"Waiting on {line.pending}{suffix}", _AMBER
        if line.reserved:
            return f"Ready to build{suffix}", _GREEN
        return (f"Used{suffix}", _GREY) if line.used else ("Closed", _GREY)

    @staticmethod
    def _summary(project, lines: list[Line]) -> str:
        if project is None:
            return "Import a KiCad BOM to start a project."
        active = [line for line in lines if not line.dnp]
        boards = f"{project.boards} board{'s' if project.boards != 1 else ''}"
        if project.status == PLANNING:
            to_order = sum(1 for line in active if line.to_order)
            return (f"<b>{project.name}</b>, {boards}. {len(active)} lines, {to_order} to order. Adjust "
                    "<i>Use from stock</i>, export the order, then reserve once you've bought the parts.")
        waiting = sum(1 for line in active if line.pending)
        return f"<b>{project.name}</b>, {boards}, {project.status}. {len(active) - waiting} of {len(active)} lines have all parts in."

    def on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        line_id = self.table.item(item.row(), self.C_DNP).data(Qt.ItemDataRole.UserRole)
        try:
            if item.column() == self.C_DNP:
                self.db.set_line_dnp(line_id, item.checkState() == Qt.CheckState.Checked)
            elif item.column() == self.C_MPN:
                self.db.set_line_mpn(line_id, item.text())
            elif item.column() == self.C_FROM_STOCK:
                text = item.text().strip()
                if not text.isdigit():
                    _show_error(self, "Use from stock must be a whole number.")
                else:
                    self.db.set_line_from_stock(line_id, int(text))
        except WorkflowError as error:
            _show_error(self, error)
        # The table can't be rebuilt from inside its own change signal.
        QTimer.singleShot(0, self.load_lines)

    def import_bom(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import KiCad BOM", "", "BOM files (*.csv *.tsv *.txt);;All files (*)")
        if not path:
            return
        try:
            bom = parse_bom_file(path)
        except (BomError, OSError) as error:
            _show_error(self, error)
            return
        dialog = ImportDialog(self, Path(path).stem, len(bom.rows), bom.warnings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            project = self.db.create_project(dialog.name.text(), bom.rows, dialog.boards.value(), path)
        except WorkflowError as error:
            _show_error(self, error)
            return
        self.refresh(project.id)

    def export_csv(self) -> None:
        project = self.current_project()
        if project is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export order", f"{project.name}-order.csv", "CSV (*.csv)")
        if path:
            try:
                count = self.db.export_order_csv(project.id, path)
            except OSError as error:
                _show_error(self, error)
                return
            QMessageBox.information(self, "PartsDB", f"Wrote {count} lines to order.")

    def commit(self) -> None:
        project = self.current_project()
        if project is None:
            return
        answer = QMessageBox.question(
            self, "Reserve parts",
            "Reserve in-stock parts and mark the rest as pending delivery?\nThe BOM can't be edited after this.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.db.commit_project(project.id)
        except WorkflowError as error:
            _show_error(self, error)
        self.load_lines()
        self.refresh(project.id)

    def fill_from_stock(self) -> None:
        project = self.current_project()
        if project is None:
            return
        try:
            plan = self.db.plan_fill_from_stock(project.id)
        except WorkflowError as error:
            _show_error(self, error)
            return
        if not plan.filled:
            waiting = len({line.line_id for line in plan.short})
            self.notify(f"Nothing to do: none of the {waiting} waiting lines have parts free in stock."
                        if waiting else "No lines are waiting on deliveries.")
            return
        if not self.confirm_fill(project, plan):
            return
        try:
            self.db.fill_from_stock(project.id)
        except WorkflowError as error:
            _show_error(self, error)
            return
        self.refresh(project.id)

    @staticmethod
    def _describe_fill(plan, limit: int = 15) -> str:
        lines = [f"  {fill.mpn} ({fill.refs}): {fill.qty}" for fill in plan.filled[:limit]]
        if len(plan.filled) > limit:
            lines.append(f"  ...and {len(plan.filled) - limit} more")
        text = (f"Reserve {plan.total} parts from free stock for {len(plan.filled)} lines that are "
                "waiting on deliveries:\n" + "\n".join(lines))
        if plan.short:
            text += (f"\n\n{len(plan.short)} lines don't have enough free stock and will keep waiting "
                     f"on {sum(line.qty for line in plan.short)} parts.")
        return text + "\n\nUndo last can reverse this."

    def confirm_fill(self, project, plan) -> bool:
        answer = QMessageBox.question(self, "Use free stock", self._describe_fill(plan))
        return answer == QMessageBox.StandardButton.Yes

    def notify(self, text: str) -> None:
        QMessageBox.information(self, "PartsDB", text)

    def finish(self, status: str) -> None:
        project = self.current_project()
        if project is None:
            return
        word = "Complete" if status == COMPLETE else "Cancel"
        answer = QMessageBox.question(
            self, f"{word} project",
            f"{word} {project.name}?\nUnused reserved parts return to free stock, and anything still pending is dropped.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            summary = self.db.finish_project(project.id, status)
        except WorkflowError as error:
            _show_error(self, error)
            return
        QMessageBox.information(
            self, "PartsDB",
            f"{summary.released} reserved parts returned to free stock. {summary.pending_dropped} pending parts dropped.",
        )
        self.refresh(project.id)

    def delete(self) -> None:
        project = self.current_project()
        if project is None:
            return
        text = f"Delete {project.name}? This can't be undone."
        if project.status == COMMITTED:
            text += "\nIts reserved parts return to free stock and anything still pending is dropped."
        if project.status != PLANNING:
            text += "\nStock counts don't change, and its scans stay in History without a project."
        if QMessageBox.question(self, "Delete project", text) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.db.delete_project(project.id)
        except WorkflowError as error:
            _show_error(self, error)
        self.refresh()


class ScanTab(QWidget):
    def __init__(self, db: Inventory):
        super().__init__()
        self.db = db
        self.scan_in_mode = QRadioButton("Scan in (parts arrived)")
        self.scan_in_mode.setChecked(True)
        self.scan_out_mode = QRadioButton("Scan out (parts used)")
        self.scan_in_mode.toggled.connect(self._update_mode)
        self.project = QComboBox()
        self.undo_button = QPushButton("Undo last")
        self.undo_button.clicked.connect(self.undo)
        top = QHBoxLayout()
        for widget in (self.scan_in_mode, self.scan_out_mode):
            top.addWidget(widget)
        top.addWidget(QLabel("Project:"))
        top.addWidget(self.project, 1)
        top.addWidget(self.undo_button)

        self.input = ScanLineEdit()
        self.input.setPlaceholderText("Scan a bag, or type a part number and press Enter")
        self.input.returnPressed.connect(self.on_scan)
        self.last_raw = QLabel()
        self.last_raw.setTextFormat(Qt.TextFormat.PlainText)
        self.last_raw.setWordWrap(True)
        self.log = QListWidget()
        self.log.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        _make_copyable(self.log)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.input)
        layout.addWidget(self.last_raw)
        layout.addWidget(self.log, 1)
        self.refresh()

    def refresh(self) -> None:
        selected = self.project.currentData()
        self.project.clear()
        self.project.addItem("(no project)", None)
        for project in self.db.list_projects((COMMITTED,)):
            self.project.addItem(project.name, project.id)
        index = self.project.findData(selected)
        if index >= 0:
            self.project.setCurrentIndex(index)
        self._update_mode()

    def _update_mode(self) -> None:
        self.project.setEnabled(self.scan_out_mode.isChecked())
        self.input.setFocus()

    def _log(self, text: str) -> None:
        self.log.insertItem(0, text)

    def on_scan(self) -> None:
        text = self.input.text()
        self.input.clear()
        self.input.setFocus()
        if not text.strip():
            return
        shown = "".join(_PICTURES.get(char, char) for char in text)
        self.last_raw.setText(f"Last scan as received: {shown}")
        try:
            label = parse_scan(text)
            if self.scan_in_mode.isChecked():
                self.scan_in(label)
            else:
                self.scan_out(label)
        except (BarcodeError, WorkflowError) as error:
            _show_error(self, error)
        self.input.setFocus()

    # Prompts are separate methods so tests can answer them.

    def ask_quantity(self, title: str, prompt: str, default: int) -> int | None:
        """Ask with a text field rather than a spin box: if someone scans the next
        bag here by accident, the letters fail validation instead of becoming a quantity."""
        while True:
            text, ok = QInputDialog.getText(self, title, prompt, text=str(default) if default else "")
            if not ok:
                return None
            if text.strip().isdigit() and int(text) > 0:
                return int(text)
            _show_error(self, "Enter a whole number greater than zero.")

    def ask_mpn(self, default: str) -> str | None:
        text, ok = QInputDialog.getText(self, "New part", "Manufacturer part number:", text=default)
        return text.strip() if ok and text.strip() else None

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def ask_did_you_mean(self, mpn: str, similar: list[tuple[str, str]]) -> str | None:
        """Returns the part number to use, or None to cancel the scan."""
        options = [f"{candidate}  ({note})" for candidate, note in similar] + [f"No, it really is {mpn}"]
        choice, ok = QInputDialog.getItem(
            self, "Did you mean...?",
            f"{mpn} isn't in the database, but these are very close.\n"
            "Part numbers that differ by a character are usually a typo or a misread.",
            options, 0, False,
        )
        if not ok:
            return None
        index = options.index(choice)
        return similar[index][0] if index < len(similar) else mpn

    def ask_substitute(self, mpn: str, open_lines: list[OpenLine]) -> tuple[bool, int | None]:
        dialog = SubstituteDialog(self, mpn, open_lines)
        return dialog.exec() == QDialog.DialogCode.Accepted, dialog.substitute_line()

    def scan_in(self, label: ScanLabel) -> None:
        part = self.db.find_part(mpn=label.mpn, supplier_pn=label.supplier_pn)
        if part is None and label.mpn:
            similar = self.db.similar_mpns(label.mpn)
            if similar:
                choice = self.ask_did_you_mean(label.mpn, similar)
                if choice is None:
                    return
                if choice != label.mpn:
                    label.mpn = choice
                    part = self.db.find_part(mpn=choice)
        mpn = part.mpn if part else label.mpn
        if not mpn:
            mpn = self.ask_mpn(label.supplier_pn or "")
            if not mpn:
                return

        if label.structured:
            previous = self.db.find_previous_scan(label.raw)
            if previous and not self.confirm(
                "Same label scanned before",
                f"This exact label was scanned in on {previous['created_at']}.\n"
                "Identical bags can share a label. Is this a different bag?",
            ):
                self._log(f"Skipped repeat scan of {mpn}")
                return

        qty = label.quantity
        if qty is None:
            qty = self.ask_quantity("Quantity", f"How many of {mpn} arrived?", 0)
            if qty is None:
                return

        substitute = None
        expected = self.db.expected_lines(part.id) if part else self.db.expected_lines_for_mpn(mpn)
        open_lines = self.db.open_lines()
        if not expected and open_lines:
            ok, substitute = self.ask_substitute(mpn, open_lines)
            if not ok:
                return

        result = self.db.receive(
            mpn, qty, manufacturer=label.manufacturer or "", supplier=label.supplier,
            supplier_pn=label.supplier_pn, raw_scan=label.raw if label.structured else None,
            substitute_for_line=substitute,
        )
        self._log(self._describe_in(result))

    @staticmethod
    def _describe_in(result: MoveResult) -> str:
        parts = [f"{p.qty} reserved for {p.project_name} ({p.refs})" for p in result.placements]
        if result.free_qty:
            parts.append(f"{result.free_qty} to free stock")
        return f"IN  {result.qty} x {result.part.mpn}: " + ", ".join(parts) + f". On hand: {result.part.on_hand}"

    def scan_out(self, label: ScanLabel) -> None:
        part = self.db.find_part(mpn=label.mpn, supplier_pn=label.supplier_pn)
        if part is None:
            raise WorkflowError(f"{label.mpn or label.supplier_pn} isn't in the database.")
        project_id = self.project.currentData()
        default = 0
        if project_id is not None:
            default = self.db.reserved_for(project_id, part.id)
            if self.db.project_line_for_part(project_id, part.id) is None and not self.confirm(
                "Not on this BOM", f"{part.mpn} isn't on this project's BOM. Scan it out anyway?"
            ):
                return
        # The label's quantity is what the bag held when new, so it's never the default here.
        qty = self.ask_quantity("Parts used", f"How many of {part.mpn} did you use, including any lost?", default)
        if qty is None:
            return
        result = self.db.consume(part.id, qty, project_id, raw_scan=label.raw if label.structured else None)
        self._log(f"OUT {result.qty} x {result.part.mpn}. On hand: {result.part.on_hand}")
        if result.warning:
            _show_error(self, result.warning)

    def undo(self) -> None:
        try:
            self._log(f"Undid {self.db.undo_last()}")
        except WorkflowError as error:
            _show_error(self, error)
        self.input.setFocus()


class InventoryTab(QWidget):
    COLUMNS = ["MPN", "Manufacturer", "Description", "Location", "DigiKey PN", "Mouser PN",
               "On hand", "Reserved", "Free", "On order"]
    EDITABLE = {1: "manufacturer", 2: "description"}
    C_LOCATION = 3

    def __init__(self, db: Inventory):
        super().__init__()
        self.db = db
        self._loading = False
        self.search = ScanLineEdit()
        self.search.setPlaceholderText("Search or scan: part number, description, manufacturer or location")
        # A scanner types a label one character at a time, so wait for a pause
        # rather than rebuilding the table for every character.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        self.search.returnPressed.connect(self.refresh)
        add_button = QPushButton("Add part...")
        add_button.clicked.connect(self.add_part)
        location_button = QPushButton("Set location...")
        location_button.clicked.connect(self.set_location)
        adjust_button = QPushButton("Adjust stock...")
        adjust_button.clicked.connect(self.adjust)
        use_button = QPushButton("Use for project...")
        use_button.setToolTip("Reserve free stock for a project line that's waiting on a delivery")
        use_button.clicked.connect(self.use_for_project)
        merge_button = QPushButton("Merge into...")
        merge_button.setToolTip("Fold a part entered under the wrong part number into the right one")
        merge_button.clicked.connect(self.merge)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(add_button)
        top.addWidget(location_button)
        top.addWidget(adjust_button)
        top.addWidget(use_button)
        top.addWidget(merge_button)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        _make_copyable(self.table)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.cellDoubleClicked.connect(
            lambda row, column: self.set_location() if column == self.C_LOCATION else None
        )
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        self.refresh()

    def refresh(self) -> None:
        self._loading = True
        self._search_timer.stop()
        parts = self.db.list_parts(self.search_term())
        with _filling(self.table):
            self.table.setRowCount(len(parts))
            for row, part in enumerate(parts):
                mpn = _item(part.mpn)
                mpn.setData(Qt.ItemDataRole.UserRole, part.id)
                values = [mpn, _item(part.manufacturer, True), _item(part.description, True), _item(part.location),
                          _item(part.digikey_pn), _item(part.mouser_pn), _item(part.on_hand), _item(part.reserved),
                          _item(part.available, color=_RED if part.available < 0 else None), _item(part.pending)]
                for column, item in enumerate(values):
                    self.table.setItem(row, column, item)
        self._loading = False

    def search_term(self) -> str:
        """The search box text, or the part number from a scanned bag label."""
        text = self.search.text()
        try:
            label = parse_scan(text)
        except BarcodeError:
            return text
        if label.structured:
            return label.mpn or label.supplier_pn or ""
        return text

    def on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() not in self.EDITABLE:
            return
        part_id = self.table.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        self.db.update_part(part_id, **{self.EDITABLE[item.column()]: item.text().strip()})

    def add_part(self) -> None:
        dialog = AddPartDialog(self.db, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.save_new_part(dialog.values())

    def save_new_part(self, values: dict) -> None:
        try:
            part = self.db.add_part(**values)
        except WorkflowError as error:
            _show_error(self, error)
            return
        self.search.clear()
        self.refresh()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == part.id:
                self.table.selectRow(row)

    def selected_part_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return [self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in rows]

    def set_location(self) -> None:
        part_ids = self.selected_part_ids()
        if not part_ids:
            _show_error(self, "Select one or more parts first.")
            return
        parts = [self.db.get_part(part_id) for part_id in part_ids]
        current = {part.location for part in parts}
        title = f"Where is {parts[0].mpn} kept?" if len(parts) == 1 else f"Where are these {len(parts)} parts kept?"
        location = self.ask_location(title, current.pop() if len(current) == 1 else "")
        if location is None:
            return
        for part_id in part_ids:
            self.db.update_part(part_id, location=location)
        self.refresh()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) in part_ids:
                self.table.selectionModel().select(
                    self.table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )

    def ask_location(self, title: str, current: str) -> str | None:
        dialog = LocationDialog(self.db, title, current, self)
        return dialog.value if dialog.exec() == QDialog.DialogCode.Accepted else None

    def use_for_project(self) -> None:
        part_ids = self.selected_part_ids()
        if len(part_ids) != 1:
            _show_error(self, "Select one part first.")
            return
        part = self.db.get_part(part_ids[0])
        lines = self.db.expected_lines(part.id)
        if not lines:
            _show_error(self, f"No project is waiting on {part.mpn}.")
            return
        if part.available <= 0:
            _show_error(self, f"None of {part.mpn} are free in stock. Adjust stock first if the count is wrong.")
            return
        choice = self.ask_use_stock(part, lines)
        if choice is None:
            return
        line_id, qty = choice
        try:
            self.db.use_stock_for_line(line_id, part.id, qty)
        except WorkflowError as error:
            _show_error(self, error)
            return
        self.refresh()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == part.id:
                self.table.selectRow(row)

    def ask_use_stock(self, part, lines: list[OpenLine]) -> tuple[int, int] | None:
        dialog = UseStockDialog(part, lines, self)
        return dialog.choice() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def merge(self) -> None:
        part_ids = self.selected_part_ids()
        if len(part_ids) != 1:
            _show_error(self, "Select the one part with the wrong part number first.")
            return
        part = self.db.get_part(part_ids[0])
        target_mpn = self.ask_merge_target(part)
        if not target_mpn:
            return
        target = self.db.find_part(mpn=target_mpn)
        if not self.confirm_merge(part, target):
            return
        try:
            merged = self.db.merge_parts(part.id, target.id)
        except WorkflowError as error:
            _show_error(self, error)
            return
        self.search.clear()
        self.refresh()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == merged.id:
                self.table.selectRow(row)

    def ask_merge_target(self, part) -> str | None:
        dialog = MergeDialog(self.db, part, self)
        return dialog.choice if dialog.exec() == QDialog.DialogCode.Accepted else None

    def confirm_merge(self, part, target) -> bool:
        answer = QMessageBox.question(
            self, "Merge part",
            f"Merge {part.mpn} into {target.mpn}?\n\n{target.mpn} will have {part.on_hand + target.on_hand} on hand, "
            f"and {part.mpn} will be deleted. This can't be undone.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def adjust(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            _show_error(self, "Select a part first.")
            return
        part = self.db.get_part(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        count, ok = QInputDialog.getInt(self, "Adjust stock", f"Actual count of {part.mpn}:", part.on_hand, 0, 100000000)
        if not ok:
            return
        self.db.adjust_stock(part.id, count, "manual count")
        self.refresh()


class HistoryTab(QWidget):
    COLUMNS = ["When (UTC)", "Action", "Part", "Change", "Project", "Note"]

    def __init__(self, db: Inventory):
        super().__init__()
        self.db = db
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        _make_copyable(self.table)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        QVBoxLayout(self).addWidget(self.table)
        self.refresh()

    def refresh(self) -> None:
        moves = self.db.list_moves()
        with _filling(self.table):
            self.table.setRowCount(len(moves))
            for row, move in enumerate(moves):
                action = move["kind"].replace("_", " ") + (" (undone)" if move["undone"] else "")
                values = [move["created_at"], action, move["mpn"], move["delta"], move["project"], move["note"]]
                for column, value in enumerate(values):
                    self.table.setItem(row, column, _item(value))


class MainWindow(QMainWindow):
    def __init__(self, db: Inventory):
        super().__init__()
        self.setWindowTitle("PartsDB")
        self.resize(1280, 760)
        self.projects_tab = ProjectsTab(db)
        self.scan_tab = ScanTab(db)
        self.inventory_tab = InventoryTab(db)
        self.history_tab = HistoryTab(db)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.projects_tab, "Projects")
        self.tabs.addTab(self.scan_tab, "Scan")
        self.tabs.addTab(self.inventory_tab, "Inventory")
        self.tabs.addTab(self.history_tab, "History")
        self.tabs.currentChanged.connect(lambda _: self.tabs.currentWidget().refresh())
        self.setCentralWidget(self.tabs)


DEFAULT_DB_PATH = Path.home() / "Dropbox" / "3DPrints" / "parts.db"


def default_db_path() -> Path:
    """In Dropbox, so the inventory survives losing this computer."""
    return DEFAULT_DB_PATH


def confirm_new_database(path: Path) -> bool:
    """Ask before creating the default database. On a new computer Dropbox may not
    have synced it yet, and an empty file created meanwhile would compete with it."""
    answer = QMessageBox.question(
        None, "PartsDB",
        f"No database found at:\n{path}\n\n"
        "If Dropbox is still syncing on this computer, choose No and wait for parts.db to appear.\n\n"
        "Create a new, empty database there?",
    )
    return answer == QMessageBox.StandardButton.Yes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PartsDB parts inventory")
    parser.add_argument("--db", help=f"database file (default: {DEFAULT_DB_PATH})")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("PartsDB")
    path = Path(args.db) if args.db else default_db_path()
    if not args.db and not path.exists():
        if not confirm_new_database(path):
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
    db = Inventory(path)
    window = MainWindow(db)
    window.statusBar().showMessage(f"Database: {path}")
    window.show()
    code = app.exec()
    db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
