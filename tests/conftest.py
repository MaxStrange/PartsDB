import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from partsdb.bom import BomRow
from partsdb.inventory import Inventory


@pytest.fixture
def db():
    inventory = Inventory()
    yield inventory
    inventory.close()


@pytest.fixture
def make_project(db):
    """Create a project from (mpn, qty) pairs."""

    def _make(name, parts, boards=1):
        rows = [BomRow(refs=[f"R{i}"], qty=qty, mpn=mpn) for i, (mpn, qty) in enumerate(parts, start=1)]
        return db.create_project(name, rows, boards)

    return _make


def stock(db, mpn, qty):
    """Put parts into free stock directly."""
    part = db.add_part(mpn)
    db.adjust_stock(part.id, qty)
    return db.get_part(part.id)
