import csv

import pytest

from conftest import stock
from partsdb.bom import BomRow
from partsdb.inventory import CANCELLED, COMMITTED, Inventory, WorkflowError


def line_for(db, project, mpn):
    return next(line for line in db.get_lines(project.id) if line.mpn == mpn)


def test_import_suggests_using_stock(db, make_project):
    stock(db, "ABC", 3)
    project = make_project("p", [("ABC", 10), ("DEF", 2)])
    abc, defn = db.get_lines(project.id)
    assert (abc.available, abc.from_stock, abc.to_order) == (3, 3, 7)
    assert (defn.available, defn.from_stock, defn.to_order) == (0, 0, 2)


def test_board_count_multiplies_need(db, make_project):
    project = make_project("p", [("ABC", 4)], boards=3)
    assert db.get_lines(project.id)[0].qty_required == 12


def test_edit_from_stock_is_clamped(db, make_project):
    stock(db, "ABC", 3)
    project = make_project("p", [("ABC", 10)])
    line = db.get_lines(project.id)[0]
    assert db.set_line_from_stock(line.id, 99) == 3
    assert db.set_line_from_stock(line.id, -1) == 0
    assert db.set_line_from_stock(line.id, 2) == 2
    assert db.get_lines(project.id)[0].to_order == 8


def test_editing_mpn_rechecks_stock(db, make_project):
    stock(db, "ABC", 5)
    project = make_project("p", [("WRONG", 2)])
    line = db.get_lines(project.id)[0]
    db.set_line_mpn(line.id, "abc")
    assert db.get_lines(project.id)[0].from_stock == 2


def test_duplicate_project_name_rejected(db, make_project):
    make_project("p", [])
    with pytest.raises(WorkflowError, match="already exists"):
        make_project("p", [])


def test_partial_stock_splits_into_reserved_and_pending(db, make_project):
    stock(db, "ABC", 3)
    project = make_project("p", [("ABC", 10)])
    db.commit_project(project.id)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (3, 7)
    assert db.get_project(project.id).status == COMMITTED


def test_commit_requires_mpn_unless_dnp(db):
    rows = [BomRow(refs=["R1"], qty=1, value="10k"), BomRow(refs=["R2"], qty=1, value="1k", dnp=True)]
    project = db.create_project("p", rows)
    with pytest.raises(WorkflowError, match="R1"):
        db.commit_project(project.id)
    assert db.get_project(project.id).status == "planning"
    line = db.get_lines(project.id)[0]
    db.set_line_dnp(line.id, True)
    db.commit_project(project.id)


def test_dnp_lines_are_not_allocated(db):
    project = db.create_project("p", [BomRow(refs=["R1"], qty=5, mpn="ABC", dnp=True)])
    db.commit_project(project.id)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending, line.to_order) == (0, 0, 0)


def test_reservations_reduce_free_stock_for_other_projects(db, make_project):
    part = stock(db, "ABC", 10)
    first = make_project("first", [("ABC", 8)])
    db.commit_project(first.id)
    assert db.get_part(part.id).available == 2
    assert db.get_part(part.id).on_hand == 10
    second = make_project("second", [("ABC", 5)])
    line = db.get_lines(second.id)[0]
    assert (line.available, line.from_stock, line.to_order) == (2, 2, 3)


def test_commit_rejects_stock_claimed_since_planning(db, make_project):
    stock(db, "ABC", 10)
    first = make_project("first", [("ABC", 8)])
    second = make_project("second", [("ABC", 8)])
    db.commit_project(first.id)
    with pytest.raises(WorkflowError, match="only 2 free"):
        db.commit_project(second.id)
    assert db.get_project(second.id).status == "planning"
    assert db.get_lines(second.id)[0].reserved == 0


def test_same_mpn_on_two_lines_cannot_double_claim(db, make_project):
    stock(db, "ABC", 5)
    project = make_project("p", [("ABC", 5), ("DEF", 5)])
    second = line_for(db, project, "DEF")
    db.set_line_mpn(second.id, "ABC")
    with pytest.raises(WorkflowError, match="only 0 free"):
        db.commit_project(project.id)


def test_receive_moves_pending_to_reserved_and_extra_to_free(db, make_project):
    project = make_project("p", [("ABC", 7)])
    db.commit_project(project.id)
    result = db.receive("ABC", 100)
    assert result.placements[0].qty == 7
    assert result.free_qty == 93
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (7, 0)
    part = result.part
    assert (part.on_hand, part.reserved, part.available) == (100, 7, 93)


def test_partial_delivery_leaves_rest_pending(db, make_project):
    project = make_project("p", [("ABC", 10)])
    db.commit_project(project.id)
    db.receive("ABC", 4)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (4, 6)


def test_receive_fills_oldest_project_first(db, make_project):
    old = make_project("old", [("ABC", 5)])
    new = make_project("new", [("ABC", 5)])
    db.commit_project(old.id)
    db.commit_project(new.id)
    result = db.receive("ABC", 7)
    assert [(p.project_name, p.qty) for p in result.placements] == [("old", 5), ("new", 2)]


def test_receive_unknown_part_is_free_stock(db):
    result = db.receive("NEW", 10, supplier="digikey", supplier_pn="NEW-ND", manufacturer="Acme")
    assert result.placements == []
    assert result.free_qty == 10
    assert result.part.manufacturer == "Acme"
    assert result.part.digikey_pn == "NEW-ND"


def test_substitute_fills_line_and_is_recorded(db, make_project):
    project = make_project("p", [("ABC", 10)])
    db.commit_project(project.id)
    line_id = db.get_lines(project.id)[0].id
    result = db.receive("ALT", 25, substitute_for_line=line_id)
    assert result.free_qty == 15
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (10, 0)
    assert line.substitutes == ["ALT"]
    assert result.part.reserved == 10
    assert db.find_part(mpn="ABC").pending == 0


def test_partial_substitute_leaves_original_pending(db, make_project):
    project = make_project("p", [("ABC", 10)])
    db.commit_project(project.id)
    line_id = db.get_lines(project.id)[0].id
    db.receive("ALT", 4, substitute_for_line=line_id)
    assert db.find_part(mpn="ABC").pending == 6
    db.receive("ABC", 6)
    assert db.get_lines(project.id)[0].pending == 0


def test_find_part_by_supplier_number(db):
    db.receive("ABC", 1, supplier="digikey", supplier_pn="ABC-ND")
    assert db.find_part(supplier_pn="abc-nd").mpn == "ABC"
    assert db.find_part(mpn="abc-nd").mpn == "ABC"
    assert db.find_part(mpn="nothing") is None


def test_previous_scan_lookup(db):
    db.receive("ABC", 5, raw_scan="LABEL")
    assert db.find_previous_scan("LABEL")["delta"] == 5
    assert db.find_previous_scan("OTHER") is None
    db.undo_last()
    assert db.find_previous_scan("LABEL") is None


def test_consume_uses_reservation(db, make_project):
    part = stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 4)])
    db.commit_project(project.id)
    db.consume(part.id, 4, project.id)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.used) == (0, 4)
    updated = db.get_part(part.id)
    assert (updated.on_hand, updated.reserved, updated.available) == (6, 0, 6)


def test_consume_lost_parts_draws_on_free_stock(db, make_project):
    part = stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 4)])
    db.commit_project(project.id)
    result = db.consume(part.id, 6, project.id)
    assert result.warning == ""
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.used) == (0, 6)
    assert db.get_part(part.id).available == 4


def test_consume_into_other_projects_reservation_warns(db, make_project):
    part = stock(db, "ABC", 10)
    first = make_project("first", [("ABC", 5)])
    second = make_project("second", [("ABC", 5)])
    db.commit_project(first.id)
    db.commit_project(second.id)
    result = db.consume(part.id, 7, first.id)
    assert "2 more reserved" in result.warning
    assert db.get_part(part.id).available == -2


def test_consume_more_than_on_hand_rejected(db):
    part = stock(db, "ABC", 2)
    with pytest.raises(WorkflowError, match="only 2 on hand"):
        db.consume(part.id, 3)
    assert db.get_part(part.id).on_hand == 2


def test_consume_without_project(db):
    part = stock(db, "ABC", 5)
    db.consume(part.id, 2)
    assert db.get_part(part.id).on_hand == 3


def test_complete_releases_leftovers_and_keeps_used(db, make_project):
    part = stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 5), ("DEF", 3)])
    db.commit_project(project.id)
    db.consume(part.id, 4, project.id)
    summary = db.finish_project(project.id)
    assert (summary.released, summary.pending_dropped) == (1, 3)
    updated = db.get_part(part.id)
    assert (updated.on_hand, updated.reserved, updated.available) == (6, 0, 6)
    assert db.find_part(mpn="DEF").pending == 0
    assert db.get_lines(project.id)[0].used == 4


def test_cancel_returns_everything_and_closed_project_stays_closed(db, make_project):
    part = stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)
    db.finish_project(project.id, CANCELLED)
    assert db.get_part(part.id).available == 10
    with pytest.raises(WorkflowError, match="already closed"):
        db.finish_project(project.id)
    with pytest.raises(WorkflowError):
        db.consume(part.id, 1, project.id)


def test_locked_after_commit(db, make_project):
    project = make_project("p", [("ABC", 1)])
    db.commit_project(project.id)
    line = db.get_lines(project.id)[0]
    for action in (lambda: db.set_line_mpn(line.id, "X"), lambda: db.set_line_from_stock(line.id, 0),
                   lambda: db.set_line_dnp(line.id, True)):
        with pytest.raises(WorkflowError, match="locked"):
            action()


def test_undo_receive_restores_allocations(db, make_project):
    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)
    db.receive("ABC", 8)
    assert "8 x ABC" in db.undo_last()
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (0, 5)
    assert db.find_part(mpn="ABC").on_hand == 0


def test_undo_consume_restores_reservation(db, make_project):
    part = stock(db, "ABC", 5)
    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)
    db.consume(part.id, 3, project.id)
    db.undo_last()
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.used) == (5, 0)
    assert db.get_part(part.id).on_hand == 5


def test_undo_refused_after_project_closed_and_leaves_data_alone(db, make_project):
    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)
    db.receive("ABC", 5)
    db.finish_project(project.id)
    with pytest.raises(WorkflowError, match="closed"):
        db.undo_last()
    assert db.find_part(mpn="ABC").on_hand == 5


def test_undo_with_nothing_to_undo(db):
    with pytest.raises(WorkflowError, match="Nothing"):
        db.undo_last()


def test_export_lists_only_parts_to_buy(db, make_project, tmp_path):
    stock(db, "ABC", 10)
    project = make_project("p", [("ABC", 5), ("DEF", 3)])
    path = tmp_path / "order.csv"
    assert db.export_order_csv(project.id, path) == 1
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["Manufacturer Part Number"] == "DEF"
    assert rows[0]["Quantity"] == "3"
    db.commit_project(project.id)
    db.receive("DEF", 1)
    assert db.export_order_csv(project.id, path) == 1
    with open(path, newline="", encoding="utf-8") as handle:
        assert next(csv.DictReader(handle))["Quantity"] == "2"


def test_search(db):
    db.add_part("ABC", description="op amp", location="Drawer 3")
    db.add_part("DEF")
    assert [p.mpn for p in db.list_parts("drawer")] == ["ABC"]
    assert len(db.list_parts()) == 2


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "parts.db"
    first = Inventory(path)
    first.receive("ABC", 5)
    first.close()
    second = Inventory(path)
    assert second.find_part(mpn="ABC").on_hand == 5
    second.close()


def test_delete_committed_project_frees_reservations_and_keeps_history(db, make_project):
    part = db.add_part("ABC")
    db.adjust_stock(part.id, 10, "count")
    project = make_project("p", [("ABC", 6)])
    db.set_line_from_stock(db.get_lines(project.id)[0].id, 4)
    db.commit_project(project.id)
    db.consume(part.id, 3, project.id)
    moves_before = len(db.list_moves())
    db.delete_project(project.id)
    assert db.get_part(part.id).on_hand == 7
    assert db.get_part(part.id).available == 7
    assert db.expected_lines(part.id) == []
    assert len(db.list_moves()) == moves_before
    with pytest.raises(WorkflowError, match="deleted"):
        db.undo_last()


def test_delete_closed_project(db, make_project):
    project = make_project("p", [("ABC", 1)])
    db.commit_project(project.id)
    db.finish_project(project.id)
    db.delete_project(project.id)
    assert project.id not in [p.id for p in db.list_projects()]
