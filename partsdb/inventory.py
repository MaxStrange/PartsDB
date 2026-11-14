"""Inventory database and the project workflow.

Stock model: ``parts.on_hand`` is the physical count. Allocations record how a
BOM line's need is being met, split by state:

* ``pending``  - ordered (or to be ordered), not here yet. Not part of on_hand.
* ``reserved`` - here, set aside for the project. Still part of on_hand.
* ``used``     - scanned out and gone. No longer part of on_hand.

A part's free stock is on_hand minus everything reserved across projects.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from partsdb.bom import BomRow

PLANNING = "planning"
COMMITTED = "committed"
COMPLETE = "complete"
CANCELLED = "cancelled"

_VERSION = 2

_SCHEMA = """
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE parts (
    id INTEGER PRIMARY KEY,
    mpn TEXT NOT NULL,
    mpn_key TEXT NOT NULL UNIQUE,
    manufacturer TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    on_hand INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE supplier_pns (
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    supplier TEXT NOT NULL,
    pn TEXT NOT NULL,
    pn_key TEXT NOT NULL,
    PRIMARY KEY (supplier, pn_key)
);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    boards INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'planning',
    source_file TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE bom_lines (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    refs TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    footprint TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    mpn TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    digikey_pn TEXT NOT NULL DEFAULT '',
    mouser_pn TEXT NOT NULL DEFAULT '',
    qty_required INTEGER NOT NULL,
    from_stock INTEGER NOT NULL DEFAULT 0,
    dnp INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE allocations (
    line_id INTEGER NOT NULL REFERENCES bom_lines(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'reserved', 'used')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (line_id, part_id, state)
);
CREATE TABLE stock_moves (
    id INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL REFERENCES parts(id),
    delta INTEGER NOT NULL,
    kind TEXT NOT NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    raw_scan TEXT,
    note TEXT NOT NULL DEFAULT '',
    alloc_deltas TEXT NOT NULL DEFAULT '[]',
    undone INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_allocations_part ON allocations(part_id, state);
CREATE INDEX idx_moves_raw ON stock_moves(raw_scan);
"""


class WorkflowError(ValueError):
    """The requested action isn't allowed in the current state. Message is user-facing."""


def part_key(text: str) -> str:
    return " ".join(text.split()).upper()


def alternates(mpn: str) -> list[str]:
    """The part numbers a BOM line accepts. A symbol can list deliberate
    alternates separated by ';', and any one of them fills the line."""
    return [part.strip() for part in mpn.split(";") if part.strip()]


def _alternate_keys(mpn: str) -> set[str]:
    return {part_key(part) for part in alternates(mpn)}


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, giving up early once it's past ``limit``."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _clean_name(text: str) -> str:
    return " ".join(text.split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Part:
    id: int
    mpn: str
    manufacturer: str
    description: str
    location: str
    on_hand: int
    reserved: int = 0
    pending: int = 0
    digikey_pn: str = ""
    mouser_pn: str = ""

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass
class Project:
    id: int
    name: str
    boards: int
    status: str
    source_file: str
    created_at: str


@dataclass
class Line:
    id: int
    project_id: int
    refs: str
    value: str
    footprint: str
    description: str
    mpn: str
    manufacturer: str
    digikey_pn: str
    mouser_pn: str
    qty_required: int
    from_stock: int
    dnp: bool
    available: int = 0
    reserved: int = 0
    pending: int = 0
    used: int = 0
    substitutes: list[str] = field(default_factory=list)

    @property
    def to_order(self) -> int:
        return 0 if self.dnp else self.qty_required - self.from_stock


@dataclass
class OpenLine:
    line_id: int
    project_id: int
    project_name: str
    refs: str
    mpn: str
    pending: int


@dataclass
class Placement:
    project_name: str
    refs: str
    qty: int


@dataclass
class MoveResult:
    move_id: int
    part: Part
    qty: int
    placements: list[Placement] = field(default_factory=list)
    free_qty: int = 0
    warning: str = ""


@dataclass
class BulkLine:
    line_id: int
    part_id: int  # the part reserved (or, for a line left short, the part still awaited)
    refs: str
    mpn: str
    qty: int


@dataclass
class BulkFill:
    filled: list[BulkLine] = field(default_factory=list)
    short: list[BulkLine] = field(default_factory=list)  # qty here is what's still waiting

    @property
    def total(self) -> int:
        return sum(line.qty for line in self.filled)


@dataclass
class FinishSummary:
    released: int
    pending_dropped: int


class Inventory:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.conn.executescript(_SCHEMA + f"PRAGMA user_version = {_VERSION};")
        elif version == 1:
            self._migrate_from_1()
        elif version > _VERSION:
            raise WorkflowError("This database was made by a newer version of PartsDB.")

    def _migrate_from_1(self) -> None:
        """Version 1 stored each part's location as free text. Turn the distinct
        values into the locations list, merging ones that differ only in case."""
        self.conn.executescript(
            """
            BEGIN;
            CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
            INSERT OR IGNORE INTO locations (name)
                SELECT DISTINCT TRIM(location) FROM parts WHERE TRIM(location) != '' ORDER BY id;
            ALTER TABLE parts ADD COLUMN location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL;
            UPDATE parts SET location_id = (SELECT id FROM locations WHERE name = TRIM(parts.location));
            ALTER TABLE parts DROP COLUMN location;
            PRAGMA user_version = 2;
            COMMIT;
            """
        )

    def close(self) -> None:
        self.conn.close()

    # ---- parts ----

    def get_part(self, part_id: int) -> Part:
        return self._parts_where("p.id = ?", (part_id,))[0]

    def list_parts(self, search: str = "") -> list[Part]:
        if not search.strip():
            return self._parts_where("1 = 1", ())
        like = f"%{search.strip()}%"
        return self._parts_where(
            "(p.mpn LIKE ? OR p.description LIKE ? OR p.manufacturer LIKE ? OR loc.name LIKE ? OR "
            "p.id IN (SELECT part_id FROM supplier_pns WHERE pn LIKE ?))",
            (like, like, like, like, like),
        )

    def _parts_where(self, where: str, params: tuple) -> list[Part]:
        rows = self.conn.execute(
            f"""
            SELECT p.id, p.mpn, p.manufacturer, p.description, COALESCE(loc.name, '') AS location, p.on_hand,
                COALESCE((SELECT SUM(qty) FROM allocations a WHERE a.part_id = p.id AND a.state = 'reserved'), 0) AS reserved,
                COALESCE((SELECT SUM(qty) FROM allocations a WHERE a.part_id = p.id AND a.state = 'pending'), 0) AS pending,
                COALESCE((SELECT pn FROM supplier_pns s WHERE s.part_id = p.id AND s.supplier = 'digikey' LIMIT 1), '') AS digikey_pn,
                COALESCE((SELECT pn FROM supplier_pns s WHERE s.part_id = p.id AND s.supplier = 'mouser' LIMIT 1), '') AS mouser_pn
            FROM parts p LEFT JOIN locations loc ON loc.id = p.location_id WHERE {where} ORDER BY p.mpn_key
            """,
            params,
        ).fetchall()
        return [Part(**dict(row)) for row in rows]

    def find_part(self, mpn: str | None = None, supplier_pn: str | None = None) -> Part | None:
        """Find by MPN first, then by supplier number. Plain scans can be either."""
        for text in (mpn, supplier_pn):
            if not text:
                continue
            row = self.conn.execute("SELECT id FROM parts WHERE mpn_key = ?", (part_key(text),)).fetchone()
            if row:
                return self.get_part(row["id"])
        for text in (supplier_pn, mpn):
            if not text:
                continue
            row = self.conn.execute("SELECT part_id FROM supplier_pns WHERE pn_key = ?", (part_key(text),)).fetchone()
            if row:
                return self.get_part(row["part_id"])
        return None

    def _get_or_create_part(self, mpn: str, manufacturer: str = "", description: str = "") -> int:
        key = part_key(mpn)
        if not key:
            raise WorkflowError("A part needs a manufacturer part number.")
        row = self.conn.execute("SELECT id, manufacturer, description FROM parts WHERE mpn_key = ?", (key,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE parts SET manufacturer = ?, description = ? WHERE id = ?",
                (row["manufacturer"] or manufacturer, row["description"] or description, row["id"]),
            )
            return row["id"]
        cursor = self.conn.execute(
            "INSERT INTO parts (mpn, mpn_key, manufacturer, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (mpn.strip(), key, manufacturer, description, _now()),
        )
        return cursor.lastrowid

    def _learn_supplier_pn(self, part_id: int, supplier: str | None, pn: str | None) -> None:
        if supplier and pn and pn.strip():
            self.conn.execute(
                "INSERT OR IGNORE INTO supplier_pns (part_id, supplier, pn, pn_key) VALUES (?, ?, ?, ?)",
                (part_id, supplier, pn.strip(), part_key(pn)),
            )

    def add_part(self, mpn: str, manufacturer: str = "", description: str = "", location: str = "",
                 supplier: str | None = None, supplier_pn: str | None = None) -> Part:
        with self.conn:
            part_id = self._get_or_create_part(mpn, manufacturer, description)
            self._learn_supplier_pn(part_id, supplier, supplier_pn)
            if location:
                self.conn.execute("UPDATE parts SET location_id = ? WHERE id = ?", (self._location_id(location), part_id))
        return self.get_part(part_id)

    def update_part(self, part_id: int, **fields: str) -> None:
        """Change manufacturer, description or location. A location must already be
        in the locations list; an empty one clears it."""
        allowed = {"manufacturer", "description", "location"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError(f"Editable fields are {sorted(allowed)}")
        with self.conn:
            if "location" in fields:
                location = fields.pop("location")
                location_id = self._location_id(location) if location.strip() else None
                self.conn.execute("UPDATE parts SET location_id = ? WHERE id = ?", (location_id, part_id))
            if fields:
                assignments = ", ".join(f"{name} = ?" for name in fields)
                self.conn.execute(f"UPDATE parts SET {assignments} WHERE id = ?", (*fields.values(), part_id))

    def merge_parts(self, from_id: int, into_id: int) -> Part:
        """Fold one part into another, for when the same part ended up under two
        part numbers (a typo, say). Stock, reservations, supplier numbers and
        history all move to ``into_id``, and ``from_id`` is deleted."""
        if from_id == into_id:
            raise WorkflowError("Pick a different part to merge into.")
        source, target = self.get_part(from_id), self.get_part(into_id)
        with self.conn:
            self.conn.execute(
                """
                UPDATE parts SET on_hand = on_hand + ?,
                    manufacturer = CASE manufacturer WHEN '' THEN ? ELSE manufacturer END,
                    description = CASE description WHEN '' THEN ? ELSE description END,
                    location_id = COALESCE(location_id, (SELECT location_id FROM parts WHERE id = ?))
                WHERE id = ?
                """,
                (source.on_hand, source.manufacturer, source.description, from_id, into_id),
            )
            for alloc in self.conn.execute(
                "SELECT line_id, state, qty FROM allocations WHERE part_id = ?", (from_id,)
            ).fetchall():
                self._alloc(alloc["line_id"], into_id, alloc["state"], alloc["qty"])
            self.conn.execute("DELETE FROM allocations WHERE part_id = ?", (from_id,))
            # A supplier number is unique on its own, so it can simply change owner.
            self.conn.execute("UPDATE supplier_pns SET part_id = ? WHERE part_id = ?", (into_id, from_id))
            # Undo replays each move's allocation changes, which name the part.
            for move in self.conn.execute(
                "SELECT id, alloc_deltas FROM stock_moves WHERE alloc_deltas LIKE '%' || ? || '%'", (from_id,)
            ).fetchall():
                deltas = [[line, into_id if part == from_id else part, state, delta]
                          for line, part, state, delta in json.loads(move["alloc_deltas"])]
                self.conn.execute("UPDATE stock_moves SET alloc_deltas = ? WHERE id = ?", (json.dumps(deltas), move["id"]))
            self.conn.execute("UPDATE stock_moves SET part_id = ? WHERE part_id = ?", (into_id, from_id))
            self.conn.execute("DELETE FROM parts WHERE id = ?", (from_id,))
        return self.get_part(target.id)

    def similar_mpns(self, mpn: str, max_distance: int = 2) -> list[tuple[str, str]]:
        """Known part numbers within a character or two of ``mpn``, closest first, as
        (mpn, note) pairs. Catches typos and misreads before they become new parts."""
        key = part_key(mpn)
        if len(key) < 4:
            return []
        limit = 1 if len(key) < 7 else max_distance
        waiting: dict[str, str] = {}
        for line in self.open_lines():
            for alt in alternates(line.mpn):
                waiting.setdefault(part_key(alt), f"{line.project_name} is waiting on {line.pending} for {line.refs}")
        rows = self.conn.execute("SELECT mpn, on_hand FROM parts").fetchall()
        candidates = {part_key(row["mpn"]): row["mpn"] for row in rows}
        on_hand = {part_key(row["mpn"]): row["on_hand"] for row in rows}
        candidates.update({k: k for k in waiting if k not in candidates})
        matches = []
        for candidate_key, candidate in candidates.items():
            if candidate_key == key or abs(len(candidate_key) - len(key)) > limit:
                continue
            distance = _edit_distance(key, candidate_key, limit)
            if distance <= limit:
                matches.append((distance, candidate_key not in waiting, candidate, waiting.get(candidate_key, f"{on_hand.get(candidate_key, 0)} on hand")))
        return [(candidate, note) for *_, candidate, note in sorted(matches)]

    # ---- locations ----

    def list_locations(self) -> list[str]:
        return [row["name"] for row in self.conn.execute("SELECT name FROM locations ORDER BY name COLLATE NOCASE")]

    def find_location(self, name: str) -> str | None:
        """The stored spelling of a location, matched ignoring case and extra spaces."""
        row = self.conn.execute("SELECT name FROM locations WHERE name = ?", (_clean_name(name),)).fetchone()
        return row["name"] if row else None

    def add_location(self, name: str) -> str:
        """Add a location, or return the existing one if it only differs in case or spacing."""
        cleaned = _clean_name(name)
        if not cleaned:
            raise WorkflowError("A location needs a name.")
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO locations (name) VALUES (?)", (cleaned,))
        return self.find_location(cleaned)

    def _location_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM locations WHERE name = ?", (_clean_name(name),)).fetchone()
        if row is None:
            raise WorkflowError(f"\"{name.strip()}\" isn't in the locations list. Add it first.")
        return row["id"]

    def adjust_stock(self, part_id: int, new_on_hand: int, note: str = "") -> None:
        if new_on_hand < 0:
            raise WorkflowError("Stock can't be negative.")
        part = self.get_part(part_id)
        with self.conn:
            self.conn.execute("UPDATE parts SET on_hand = ? WHERE id = ?", (new_on_hand, part_id))
            self._record_move(part_id, new_on_hand - part.on_hand, "adjust", note=note)

    # ---- projects and planning ----

    def create_project(self, name: str, rows: list[BomRow], boards: int = 1, source_file: str = "") -> Project:
        name = name.strip()
        if not name:
            raise WorkflowError("The project needs a name.")
        if boards < 1:
            raise WorkflowError("Board count must be at least 1.")
        if self.conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
            raise WorkflowError(f'A project named "{name}" already exists.')
        with self.conn:
            project_id = self.conn.execute(
                "INSERT INTO projects (name, boards, source_file, created_at) VALUES (?, ?, ?, ?)",
                (name, boards, source_file, _now()),
            ).lastrowid
            for row in rows:
                required = row.qty * boards
                self.conn.execute(
                    """
                    INSERT INTO bom_lines (project_id, refs, value, footprint, description, mpn, manufacturer,
                        digikey_pn, mouser_pn, qty_required, from_stock, dnp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id, ", ".join(row.refs), row.value, row.footprint, row.description, row.mpn,
                        row.manufacturer, row.digikey_pn, row.mouser_pn, required,
                        0 if row.dnp else self._suggest_from_stock(row.mpn, required), int(row.dnp),
                    ),
                )
        return self.get_project(project_id)

    def _suggest_from_stock(self, mpn: str, required: int) -> int:
        return max(0, min(required, self._free_for(mpn)))

    def _alternate_parts(self, mpn: str) -> list[Part]:
        """Parts in the database for each of a line's alternates, in the order listed."""
        found = [self.find_part(mpn=alt) for alt in alternates(mpn)]
        return [part for part in found if part is not None]

    def _free_for(self, mpn: str) -> int:
        return sum(max(part.available, 0) for part in self._alternate_parts(mpn))

    def get_project(self, project_id: int) -> Project:
        row = self.conn.execute(
            "SELECT id, name, boards, status, source_file, created_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError("That project no longer exists.")
        return Project(**dict(row))

    def list_projects(self, statuses: tuple[str, ...] | None = None) -> list[Project]:
        rows = self.conn.execute(
            "SELECT id, name, boards, status, source_file, created_at FROM projects ORDER BY created_at DESC, id DESC"
        ).fetchall()
        projects = [Project(**dict(row)) for row in rows]
        return [p for p in projects if statuses is None or p.status in statuses]

    def get_lines(self, project_id: int) -> list[Line]:
        rows = self.conn.execute("SELECT * FROM bom_lines WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()
        lines = []
        for row in rows:
            data = dict(row)
            data["dnp"] = bool(data["dnp"])
            line = Line(**data)
            line.available = self._free_for(line.mpn)
            accepted = _alternate_keys(line.mpn)
            for alloc in self.conn.execute(
                """
                SELECT a.state, a.qty, p.mpn, p.mpn_key FROM allocations a JOIN parts p ON p.id = a.part_id
                WHERE a.line_id = ?
                """,
                (line.id,),
            ):
                setattr(line, alloc["state"], getattr(line, alloc["state"]) + alloc["qty"])
                if alloc["mpn_key"] not in accepted and alloc["mpn"] not in line.substitutes:
                    line.substitutes.append(alloc["mpn"])
            lines.append(line)
        return lines

    def _get_line(self, line_id: int) -> Line:
        row = self.conn.execute("SELECT project_id FROM bom_lines WHERE id = ?", (line_id,)).fetchone()
        return next(line for line in self.get_lines(row["project_id"]) if line.id == line_id)

    def _require_planning(self, project_id: int) -> None:
        if self.get_project(project_id).status != PLANNING:
            raise WorkflowError("This BOM is locked because its parts have already been reserved.")

    def set_line_mpn(self, line_id: int, mpn: str) -> None:
        line = self._get_line(line_id)
        self._require_planning(line.project_id)
        mpn = mpn.strip()
        from_stock = 0 if line.dnp else self._suggest_from_stock(mpn, line.qty_required)
        with self.conn:
            self.conn.execute("UPDATE bom_lines SET mpn = ?, from_stock = ? WHERE id = ?", (mpn, from_stock, line_id))

    def set_line_from_stock(self, line_id: int, qty: int) -> int:
        """Set how much of a line to take from stock; returns the value after clamping."""
        line = self._get_line(line_id)
        self._require_planning(line.project_id)
        clamped = 0 if line.dnp else max(0, min(qty, line.qty_required, line.available))
        with self.conn:
            self.conn.execute("UPDATE bom_lines SET from_stock = ? WHERE id = ?", (clamped, line_id))
        return clamped

    def set_line_dnp(self, line_id: int, dnp: bool) -> None:
        """Mark a line Do Not Place, or undo that.

        While planning this only changes what gets ordered. Once parts are reserved,
        marking a line DNP releases its reserved parts to free stock and stops waiting
        for any still on order, as if that line alone were cancelled. Unmarking it
        reserves free stock again and waits for the rest. Parts already used stay used.
        """
        line = self._get_line(line_id)
        status = self.get_project(line.project_id).status
        if status == PLANNING:
            from_stock = 0 if dnp else self._suggest_from_stock(line.mpn, line.qty_required)
            with self.conn:
                self.conn.execute("UPDATE bom_lines SET dnp = ?, from_stock = ? WHERE id = ?", (int(dnp), from_stock, line_id))
            return
        if status != COMMITTED:
            raise WorkflowError("This project is closed.")
        if dnp == line.dnp:
            return
        if not dnp and not line.mpn:
            raise WorkflowError("This line needs an MPN before it can be fitted.")
        with self.conn:
            if dnp:
                self.conn.execute(
                    "DELETE FROM allocations WHERE line_id = ? AND state IN ('reserved', 'pending')", (line_id,)
                )
            else:
                needed = max(line.qty_required - line.used, 0)
                self._reserve_or_await(line_id, line.mpn, needed, min(needed, self._free_for(line.mpn)))
            self.conn.execute("UPDATE bom_lines SET dnp = ? WHERE id = ?", (int(dnp), line_id))

    def _reserve_or_await(self, line_id: int, mpn: str, needed: int, from_stock: int) -> int:
        """Reserve ``from_stock`` of a line's parts from free stock, taking each listed
        alternate in turn, and await the rest of ``needed`` as the first alternate.
        Returns the id of the awaited part."""
        part_id = self._get_or_create_part(alternates(mpn)[0])
        left = from_stock
        for part in self._alternate_parts(mpn):
            take = min(left, max(self.get_part(part.id).available, 0))
            self._alloc(line_id, part.id, "reserved", take)
            left -= take
        self._alloc(line_id, part_id, "pending", needed - from_stock)
        return part_id

    def export_order_csv(self, project_id: int, path: str | Path) -> int:
        """Write the parts still to buy, for upload to a distributor. Returns the line count."""
        project = self.get_project(project_id)
        rows = []
        for line in self.get_lines(project_id):
            qty = line.to_order if project.status == PLANNING else line.pending
            if qty > 0:
                # Distributor upload tools can't read "A;B", so send the first choice. Supplier
                # numbers listed the same way can't be paired with it reliably, so they're left out.
                mpn = (alternates(line.mpn) or [""])[0]
                digikey = "" if ";" in line.digikey_pn else line.digikey_pn
                mouser = "" if ";" in line.mouser_pn else line.mouser_pn
                rows.append([qty, mpn, line.manufacturer, digikey, mouser, line.refs, line.value, line.footprint])
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Quantity", "Manufacturer Part Number", "Manufacturer", "Digi-Key Part Number",
                             "Mouser Part Number", "Reference", "Value", "Footprint"])
            writer.writerows(rows)
        return len(rows)

    def delete_project(self, project_id: int) -> None:
        """Remove a project in any state. Its reservations and pending parts go with it,
        so reserved parts become free again. On-hand counts don't change, and its stock
        moves stay in History without a project."""
        self.get_project(project_id)
        with self.conn:
            self.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def commit_project(self, project_id: int) -> None:
        """Step 6: reserve what's in stock, and mark the rest as pending delivery."""
        self._require_planning(project_id)
        lines = [line for line in self.get_lines(project_id) if not line.dnp]
        missing = [line.refs or line.value or f"line {line.id}" for line in lines if not line.mpn]
        if missing:
            raise WorkflowError("These lines need an MPN or a DNP mark first: " + "; ".join(missing))
        with self.conn:
            for line in lines:
                choices = alternates(line.mpn)
                part_id = self._get_or_create_part(choices[0], line.manufacturer, line.description)
                if len(choices) == 1:
                    self._learn_supplier_pn(part_id, "digikey", line.digikey_pn)
                    self._learn_supplier_pn(part_id, "mouser", line.mouser_pn)
                # Stock can change after planning, and earlier lines in this loop
                # have already reserved theirs, so check against the current count.
                free = self._free_for(line.mpn)
                if line.from_stock > free:
                    raise WorkflowError(
                        f"{line.mpn}: only {free} free in stock now, but line {line.refs} takes {line.from_stock}. "
                        "Lower \"Use from stock\" and try again."
                    )
                self._reserve_or_await(line.id, line.mpn, line.qty_required, line.from_stock)
            self.conn.execute("UPDATE projects SET status = ? WHERE id = ?", (COMMITTED, project_id))

    def finish_project(self, project_id: int, status: str = COMPLETE) -> FinishSummary:
        """Close a project: leftover reservations go back to free stock and undelivered parts are dropped."""
        if status not in (COMPLETE, CANCELLED):
            raise ValueError(status)
        if self.get_project(project_id).status not in (PLANNING, COMMITTED):
            raise WorkflowError("This project is already closed.")
        with self.conn:
            totals = dict(self.conn.execute(
                """
                SELECT a.state, SUM(a.qty) FROM allocations a JOIN bom_lines l ON l.id = a.line_id
                WHERE l.project_id = ? GROUP BY a.state
                """,
                (project_id,),
            ).fetchall())
            self.conn.execute(
                """
                DELETE FROM allocations WHERE state IN ('reserved', 'pending')
                AND line_id IN (SELECT id FROM bom_lines WHERE project_id = ?)
                """,
                (project_id,),
            )
            self.conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
        return FinishSummary(released=totals.get("reserved", 0), pending_dropped=totals.get("pending", 0))

    # ---- allocations and the move log ----

    def _alloc(self, line_id: int, part_id: int, state: str, delta: int, log: list | None = None) -> None:
        if delta == 0:
            return
        row = self.conn.execute(
            "SELECT qty FROM allocations WHERE line_id = ? AND part_id = ? AND state = ?", (line_id, part_id, state)
        ).fetchone()
        new_qty = (row["qty"] if row else 0) + delta
        if new_qty < 0:
            raise WorkflowError("That would take an allocation below zero.")
        if new_qty == 0:
            self.conn.execute(
                "DELETE FROM allocations WHERE line_id = ? AND part_id = ? AND state = ?", (line_id, part_id, state)
            )
        else:
            self.conn.execute(
                """
                INSERT INTO allocations (line_id, part_id, state, qty) VALUES (?, ?, ?, ?)
                ON CONFLICT (line_id, part_id, state) DO UPDATE SET qty = excluded.qty
                """,
                (line_id, part_id, state, new_qty),
            )
        if log is not None:
            log.append([line_id, part_id, state, delta])

    def _record_move(self, part_id: int, delta: int, kind: str, project_id: int | None = None,
                     raw_scan: str | None = None, note: str = "", alloc_log: list | None = None) -> int:
        return self.conn.execute(
            """
            INSERT INTO stock_moves (part_id, delta, kind, project_id, raw_scan, note, alloc_deltas, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (part_id, delta, kind, project_id, raw_scan, note, json.dumps(alloc_log or []), _now()),
        ).lastrowid

    def list_moves(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT m.id, m.created_at, m.kind, m.delta, m.note, m.undone,
                CASE m.kind WHEN 'bulk_from_stock' THEN '(several)' ELSE p.mpn END AS mpn, pr.name AS project
            FROM stock_moves m JOIN parts p ON p.id = m.part_id LEFT JOIN projects pr ON pr.id = m.project_id
            ORDER BY m.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def find_previous_scan(self, raw: str) -> sqlite3.Row | None:
        """Return the last scan-in of this exact label. Identical bags have identical labels, so this only warns."""
        return self.conn.execute(
            "SELECT id, created_at, delta FROM stock_moves WHERE raw_scan = ? AND kind = 'scan_in' AND undone = 0 "
            "ORDER BY id DESC LIMIT 1",
            (raw,),
        ).fetchone()

    def undo_last(self) -> str:
        """Reverse the most recent stock move. Returns a description of what was undone."""
        move = self.conn.execute(
            "SELECT m.*, p.mpn FROM stock_moves m JOIN parts p ON p.id = m.part_id WHERE m.undone = 0 ORDER BY m.id DESC LIMIT 1"
        ).fetchone()
        if move is None:
            raise WorkflowError("Nothing to undo.")
        deltas = json.loads(move["alloc_deltas"])
        with self.conn:
            for line_id, part_id, state, delta in deltas:
                status = self.conn.execute(
                    "SELECT pr.status FROM bom_lines l JOIN projects pr ON pr.id = l.project_id WHERE l.id = ?", (line_id,)
                ).fetchone()
                if status is None or status["status"] != COMMITTED:
                    raise WorkflowError("Can't undo: the project this affected has been closed or deleted.")
                self._alloc(line_id, part_id, state, -delta)
            on_hand = self.get_part(move["part_id"]).on_hand - move["delta"]
            if on_hand < 0:
                raise WorkflowError("Can't undo: those parts have already been used.")
            self.conn.execute("UPDATE parts SET on_hand = ? WHERE id = ?", (on_hand, move["part_id"]))
            self.conn.execute("UPDATE stock_moves SET undone = 1 WHERE id = ?", (move["id"],))
        if move["kind"] == "bulk_from_stock":
            lines = {line_id for line_id, _, state, _ in deltas if state == "reserved"}
            return f"reserving stock for {len(lines)} waiting lines"
        if move["kind"] == "from_stock":
            qty = sum(delta for _, _, state, delta in deltas if state == "reserved")
            return f'reserving {qty} x {move["mpn"]} from stock'
        return f'{move["kind"].replace("_", " ")} of {abs(move["delta"])} x {move["mpn"]}'

    # ---- scanning in ----

    def open_lines(self) -> list[OpenLine]:
        """Every BOM line still waiting on parts, oldest project first."""
        return self._open_lines_where("1 = 1", ())

    def expected_lines(self, part_id: int) -> list[OpenLine]:
        """Lines waiting on this part, including lines that list it as an alternate."""
        direct = {line.line_id for line in self._open_lines_where("a.part_id = ?", (part_id,))}
        key = part_key(self.get_part(part_id).mpn)
        return [line for line in self.open_lines() if line.line_id in direct or key in _alternate_keys(line.mpn)]

    def expected_lines_for_mpn(self, mpn: str) -> list[OpenLine]:
        """Lines waiting on a part number that isn't in the database yet."""
        key = part_key(mpn)
        return [line for line in self.open_lines() if key in _alternate_keys(line.mpn)]

    def _take_pending(self, line_id: int, part_id: int, qty: int, log: list) -> None:
        """Reduce a line's pending quantity by ``qty``, from this part's pending first.
        The part that arrived can differ from the one awaited, when it's an alternate
        or a substitute."""
        rows = self.conn.execute(
            "SELECT part_id, qty FROM allocations WHERE line_id = ? AND state = 'pending' ORDER BY part_id = ? DESC",
            (line_id, part_id),
        ).fetchall()
        for row in rows:
            take = min(qty, row["qty"])
            self._alloc(line_id, row["part_id"], "pending", -take, log)
            qty -= take

    def plan_fill_from_stock(self, project_id: int) -> BulkFill:
        """What fill_from_stock would do, without changing anything."""
        if self.get_project(project_id).status != COMMITTED:
            raise WorkflowError("Only a project with parts on order has lines waiting.")
        rows = self.conn.execute(
            """
            SELECT a.line_id, a.part_id, a.qty, l.refs, p.mpn
            FROM allocations a JOIN bom_lines l ON l.id = a.line_id JOIN parts p ON p.id = a.part_id
            WHERE l.project_id = ? AND a.state = 'pending' ORDER BY l.id
            """,
            (project_id,),
        ).fetchall()
        line_mpns = {
            row["id"]: row["mpn"]
            for row in self.conn.execute("SELECT id, mpn FROM bom_lines WHERE project_id = ?", (project_id,))
        }
        free: dict[int, int] = {}
        plan = BulkFill()
        for row in rows:
            # The awaited part first, then any alternates the line lists.
            candidates = [self.get_part(row["part_id"])] + [
                part for part in self._alternate_parts(line_mpns[row["line_id"]]) if part.id != row["part_id"]
            ]
            waiting = row["qty"]
            for part in candidates:
                if part.id not in free:
                    free[part.id] = max(part.available, 0)
                take = min(waiting, free[part.id])
                if take:
                    free[part.id] -= take
                    waiting -= take
                    plan.filled.append(BulkLine(row["line_id"], part.id, row["refs"], part.mpn, take))
            if waiting:
                plan.short.append(BulkLine(row["line_id"], row["part_id"], row["refs"], row["mpn"], waiting))
        return plan

    def fill_from_stock(self, project_id: int) -> BulkFill:
        """Reserve free stock for every line of the project that's waiting on a delivery,
        as far as stock allows. Recorded as one move, so Undo reverses all of it."""
        plan = self.plan_fill_from_stock(project_id)
        if not plan.filled:
            return plan
        log: list = []
        with self.conn:
            for fill in plan.filled:
                self._take_pending(fill.line_id, fill.part_id, fill.qty, log)
                self._alloc(fill.line_id, fill.part_id, "reserved", fill.qty, log)
            self._record_move(
                plan.filled[0].part_id, 0, "bulk_from_stock", project_id,
                note=f"Reserved free stock for {len(plan.filled)} waiting lines ({plan.total} parts)", alloc_log=log,
            )
        return plan

    def use_stock_for_line(self, line_id: int, part_id: int, qty: int) -> MoveResult:
        """Stop waiting for a delivery and reserve parts already in free stock instead.

        Moves ``qty`` of the line's pending quantity for this part to reserved. On-hand
        doesn't change: the parts are already here, they're just set aside now.
        """
        if qty <= 0:
            raise WorkflowError("Quantity must be at least 1.")
        line = next((l for l in self.expected_lines(part_id) if l.line_id == line_id), None)
        if line is None:
            raise WorkflowError("That line isn't waiting on this part any more.")
        part = self.get_part(part_id)
        if qty > line.pending:
            raise WorkflowError(f"{line.project_name} {line.refs} is only waiting on {line.pending}.")
        if qty > part.available:
            raise WorkflowError(f"Only {part.available} of {part.mpn} are free in stock.")
        log: list = []
        with self.conn:
            self._take_pending(line_id, part_id, qty, log)
            self._alloc(line_id, part_id, "reserved", qty, log)
            move_id = self._record_move(
                part_id, 0, "from_stock", line.project_id,
                note=f"Reserved {qty} from stock for {line.refs} instead of waiting for delivery", alloc_log=log,
            )
        return MoveResult(move_id, self.get_part(part_id), qty, [Placement(line.project_name, line.refs, qty)])

    def _open_lines_where(self, where: str, params: tuple) -> list[OpenLine]:
        rows = self.conn.execute(
            f"""
            SELECT l.id AS line_id, pr.id AS project_id, pr.name AS project_name, l.refs, l.mpn, SUM(a.qty) AS pending
            FROM allocations a JOIN bom_lines l ON l.id = a.line_id JOIN projects pr ON pr.id = l.project_id
            WHERE a.state = 'pending' AND pr.status = 'committed' AND {where}
            GROUP BY l.id ORDER BY pr.created_at, pr.id, l.id
            """,
            params,
        ).fetchall()
        return [OpenLine(**dict(row)) for row in rows]

    def receive(self, mpn: str, qty: int, *, manufacturer: str = "", description: str = "",
                supplier: str | None = None, supplier_pn: str | None = None, raw_scan: str | None = None,
                substitute_for_line: int | None = None) -> MoveResult:
        """Step 7: put a delivered bag into stock.

        Pending lines for the part are filled oldest project first, and anything
        left over becomes free stock. With ``substitute_for_line``, the bag fills
        that line's pending quantity instead, whatever part the line asked for.
        """
        if qty <= 0:
            raise WorkflowError("Quantity must be at least 1.")
        with self.conn:
            existing = self.find_part(mpn=mpn, supplier_pn=supplier_pn)
            part_id = existing.id if existing else self._get_or_create_part(mpn, manufacturer, description)
            if existing:
                self._get_or_create_part(existing.mpn, manufacturer, description)
            self._learn_supplier_pn(part_id, supplier, supplier_pn)
            self.conn.execute("UPDATE parts SET on_hand = on_hand + ? WHERE id = ?", (qty, part_id))

            log: list = []
            placements: list[Placement] = []
            remaining = qty
            if substitute_for_line is not None:
                targets = [line for line in self.open_lines() if line.line_id == substitute_for_line]
            else:
                targets = self.expected_lines(part_id)
            for target in targets:
                if remaining == 0:
                    break
                wanted = min(remaining, target.pending)
                self._take_pending(target.line_id, part_id, wanted, log)
                self._alloc(target.line_id, part_id, "reserved", wanted, log)
                placements.append(Placement(target.project_name, target.refs, wanted))
                remaining -= wanted
            note = "substitute" if substitute_for_line is not None else ""
            move_id = self._record_move(part_id, qty, "scan_in", raw_scan=raw_scan, note=note, alloc_log=log)
        return MoveResult(move_id, self.get_part(part_id), qty, placements, free_qty=remaining)

    # ---- scanning out ----

    def project_line_for_part(self, project_id: int, part_id: int) -> int | None:
        """The line in this project that this part belongs to, if any."""
        key = part_key(self.get_part(part_id).mpn)
        rows = self.conn.execute(
            """
            SELECT l.id, l.mpn, MAX(a.state = 'reserved') AS reserved, COUNT(a.part_id) AS allocated
            FROM bom_lines l LEFT JOIN allocations a ON a.line_id = l.id AND a.part_id = ?
            WHERE l.project_id = ? GROUP BY l.id ORDER BY l.id
            """,
            (part_id, project_id),
        ).fetchall()
        # Prefer a line holding this part reserved, then any line it's allocated to,
        # then a line that lists it as one of its part numbers.
        for test in (lambda r: r["reserved"], lambda r: r["allocated"], lambda r: key in _alternate_keys(r["mpn"])):
            match = next((row["id"] for row in rows if test(row)), None)
            if match is not None:
                return match
        return None

    def reserved_for(self, project_id: int, part_id: int) -> int:
        return self.conn.execute(
            """
            SELECT COALESCE(SUM(a.qty), 0) FROM allocations a JOIN bom_lines l ON l.id = a.line_id
            WHERE l.project_id = ? AND a.part_id = ? AND a.state = 'reserved'
            """,
            (project_id, part_id),
        ).fetchone()[0]

    def consume(self, part_id: int, qty: int, project_id: int | None = None, raw_scan: str | None = None) -> MoveResult:
        """Step 8: record parts leaving stock, including any lost along the way.

        Reservations are used first. Going past them draws on free stock, and a
        warning is returned if that eats into another project's reservation.
        """
        if qty <= 0:
            raise WorkflowError("Quantity must be at least 1.")
        part = self.get_part(part_id)
        if qty > part.on_hand:
            raise WorkflowError(f"{part.mpn}: only {part.on_hand} on hand, can't use {qty}. Adjust the stock count if it's wrong.")
        project = None
        if project_id is not None:
            project = self.get_project(project_id)
            if project.status != COMMITTED:
                raise WorkflowError("Parts can only be scanned out against a project with reserved parts.")

        log: list = []
        placements: list[Placement] = []
        with self.conn:
            remaining = qty
            if project is not None:
                rows = self.conn.execute(
                    """
                    SELECT a.line_id, a.qty, l.refs FROM allocations a JOIN bom_lines l ON l.id = a.line_id
                    WHERE l.project_id = ? AND a.part_id = ? AND a.state = 'reserved' ORDER BY l.id
                    """,
                    (project_id, part_id),
                ).fetchall()
                for row in rows:
                    take = min(remaining, row["qty"])
                    if take:
                        self._alloc(row["line_id"], part_id, "reserved", -take, log)
                        self._alloc(row["line_id"], part_id, "used", take, log)
                        placements.append(Placement(project.name, row["refs"], take))
                        remaining -= take
                if remaining:
                    line_id = self.project_line_for_part(project_id, part_id)
                    if line_id is not None:
                        self._alloc(line_id, part_id, "used", remaining, log)
            self.conn.execute("UPDATE parts SET on_hand = on_hand - ? WHERE id = ?", (qty, part_id))
            move_id = self._record_move(part_id, -qty, "scan_out", project_id, raw_scan, alloc_log=log)
        updated = self.get_part(part_id)
        warning = ""
        if updated.available < 0:
            warning = (f"{updated.mpn}: other projects now have {-updated.available} more reserved "
                       "than is left in stock.")
        return MoveResult(move_id, updated, qty, placements, free_qty=remaining, warning=warning)
