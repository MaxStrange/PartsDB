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
    db.add_location("Drawer 3")
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


def test_locations_ignore_case_and_spacing(db):
    assert db.add_location("  Drawer   3 ") == "Drawer 3"
    assert db.add_location("drawer 3") == "Drawer 3"
    db.add_location("bin A")
    assert db.list_locations() == ["bin A", "Drawer 3"]
    assert db.find_location("DRAWER 3") == "Drawer 3"
    assert db.find_location("Drawer 4") is None
    with pytest.raises(WorkflowError):
        db.add_location("   ")


def test_part_location_must_be_in_the_list(db):
    part = db.add_part("ABC")
    with pytest.raises(WorkflowError, match="locations list"):
        db.update_part(part.id, location="HelloWorld caps")
    with pytest.raises(WorkflowError, match="locations list"):
        db.add_part("DEF", location="nowhere")
    db.add_location("Drawer 3")
    db.update_part(part.id, location="drawer 3", description="op amp")
    part = db.get_part(part.id)
    assert (part.location, part.description) == ("Drawer 3", "op amp")
    db.update_part(part.id, location="")
    assert db.get_part(part.id).location == ""


def test_version_1_database_is_upgraded(tmp_path):
    import sqlite3

    from partsdb.inventory import _SCHEMA

    v1_schema = _SCHEMA.split("CREATE TABLE parts", 1)[1].replace(
        "location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL", "location TEXT NOT NULL DEFAULT ''"
    )
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE parts" + v1_schema + "PRAGMA user_version = 1;")
    for mpn, location in [("A", "Drawer 3"), ("B", "drawer 3 "), ("C", ""), ("D", "Bin 1")]:
        conn.execute("INSERT INTO parts (mpn, mpn_key, location, created_at) VALUES (?, ?, ?, '')", (mpn, mpn, location))
    conn.commit()
    conn.close()

    db = Inventory(path)
    assert db.list_locations() == ["Bin 1", "Drawer 3"]
    assert [p.location for p in db.list_parts()] == ["Drawer 3", "Drawer 3", "", "Bin 1"]
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
    db.close()
    Inventory(path).close()  # opening again leaves it alone


def test_merge_parts_moves_stock_reservations_and_history(db, make_project):
    project = make_project("p", [("E3V3HBPC1A", 1)])
    db.commit_project(project.id)
    right = db.find_part(mpn="E3V3HBPC1A")
    line = db.get_lines(project.id)[0]
    # The typo'd bag was used as a substitute for the right part's line.
    result = db.receive("E3VHBPC1A", 4, supplier="digikey", supplier_pn="TYPO-ND", substitute_for_line=line.id)
    wrong = result.part
    db.add_location("Drawer 3")
    db.update_part(wrong.id, location="Drawer 3", description="TVS diode")
    assert db.get_lines(project.id)[0].substitutes == ["E3VHBPC1A"]

    merged = db.merge_parts(wrong.id, right.id)

    assert (merged.on_hand, merged.reserved, merged.available) == (4, 1, 3)
    assert (merged.location, merged.description) == ("Drawer 3", "TVS diode")
    assert db.find_part(mpn="E3VHBPC1A") is None
    assert db.find_part(supplier_pn="TYPO-ND").id == right.id
    line = db.get_lines(project.id)[0]
    assert line.substitutes == []
    assert line.reserved == 1 and line.pending == 0
    assert [m["mpn"] for m in db.list_moves()] == ["E3V3HBPC1A"]
    # Undo still works after the merge, against the merged part.
    db.undo_last()
    assert db.get_part(right.id).on_hand == 0
    assert db.get_lines(project.id)[0].pending == 1


def test_merge_combines_allocations_on_the_same_line(db, make_project):
    project = make_project("p", [("ABCDEF", 4)])
    db.commit_project(project.id)
    right = db.find_part(mpn="ABCDEF")
    line = db.get_lines(project.id)[0]
    db.receive("ABCDEF", 1)
    wrong = db.receive("ABCDEX", 2, substitute_for_line=line.id).part
    db.merge_parts(wrong.id, right.id)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (3, 1)


def test_merge_into_itself_is_refused(db):
    part = db.add_part("ABC")
    with pytest.raises(WorkflowError):
        db.merge_parts(part.id, part.id)


def test_similar_mpns_finds_typos_and_prefers_waiting_lines(db, make_project):
    db.add_part("E3V3HBPC1B")
    project = make_project("p", [("E3V3HBPC1A", 2)])
    db.commit_project(project.id)
    similar = db.similar_mpns("e3vhbpc1a")
    assert [mpn for mpn, _ in similar] == ["E3V3HBPC1A", "E3V3HBPC1B"]
    assert "waiting on 2" in similar[0][1]
    assert db.similar_mpns("E3V3HBPC1A") == [("E3V3HBPC1B", "0 on hand")]  # exact match isn't listed
    assert db.similar_mpns("TOTALLYDIFFERENT") == []
    db.add_part("LM358")
    assert db.similar_mpns("LM35") == [("LM358", "0 on hand")]
    assert db.similar_mpns("LM3") == []  # too short to guess


def test_use_stock_for_line_stops_waiting(db, make_project):
    project = make_project("p", [("ABC", 5)])
    db.commit_project(project.id)  # nothing in stock yet, so all 5 pending
    part = db.find_part(mpn="ABC")
    db.adjust_stock(part.id, 3, "entered in Inventory")
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (0, 5)

    result = db.use_stock_for_line(line.id, part.id, 2)

    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (2, 3)
    part = db.get_part(part.id)
    assert (part.on_hand, part.reserved, part.available, part.pending) == (3, 2, 1, 3)
    assert result.placements[0].qty == 2
    move = db.list_moves()[0]
    assert (move["kind"], move["delta"], move["project"]) == ("from_stock", 0, "p")
    assert "reserving 2 x ABC from stock" in db.undo_last()
    assert db.get_lines(project.id)[0].pending == 5


def test_use_stock_for_line_limits(db, make_project):
    project = make_project("p", [("ABC", 2)])
    db.commit_project(project.id)
    part = db.find_part(mpn="ABC")
    line = db.get_lines(project.id)[0]
    with pytest.raises(WorkflowError, match="free in stock"):
        db.use_stock_for_line(line.id, part.id, 1)
    db.adjust_stock(part.id, 10)
    with pytest.raises(WorkflowError, match="only waiting on 2"):
        db.use_stock_for_line(line.id, part.id, 3)
    with pytest.raises(WorkflowError, match="at least 1"):
        db.use_stock_for_line(line.id, part.id, 0)
    db.use_stock_for_line(line.id, part.id, 2)
    with pytest.raises(WorkflowError, match="isn't waiting"):
        db.use_stock_for_line(line.id, part.id, 1)
    # A later delivery now goes to free stock, since nothing is waiting.
    assert db.receive("ABC", 5).free_qty == 5


def test_fill_from_stock_fills_what_it_can_and_undoes_as_one(db, make_project):
    project = make_project("p", [("AAA", 4), ("BBB", 3), ("CCC", 2), ("AAA2", 1)])
    db.commit_project(project.id)  # nothing in stock: everything pending
    for mpn, qty in (("AAA", 10), ("BBB", 1)):
        db.adjust_stock(db.find_part(mpn=mpn).id, qty)
    other = make_project("other", [("BBB", 1)])
    db.set_line_from_stock(db.get_lines(other.id)[0].id, 1)
    db.commit_project(other.id)  # another project holds the only free BBB

    plan = db.plan_fill_from_stock(project.id)
    assert [(f.mpn, f.qty) for f in plan.filled] == [("AAA", 4)]
    assert sorted((s.mpn, s.qty) for s in plan.short) == [("AAA2", 1), ("BBB", 3), ("CCC", 2)]
    assert db.get_lines(project.id)[0].pending == 4  # the plan changed nothing

    db.fill_from_stock(project.id)
    by_mpn = {line.mpn: line for line in db.get_lines(project.id)}
    assert (by_mpn["AAA"].reserved, by_mpn["AAA"].pending) == (4, 0)
    assert by_mpn["BBB"].pending == 3
    move = db.list_moves()[0]
    assert (move["kind"], move["mpn"], move["project"]) == ("bulk_from_stock", "(several)", "p")
    assert "1 waiting lines" in db.undo_last()
    assert db.get_lines(project.id)[0].pending == 4


def test_fill_from_stock_partial_and_shared_part(db, make_project):
    project = make_project("p", [("AAA", 3), ("AAA", 3)])  # two lines sharing one part
    db.commit_project(project.id)
    db.adjust_stock(db.find_part(mpn="AAA").id, 4)
    plan = db.fill_from_stock(project.id)
    assert plan.total == 4 and plan.short[0].qty == 2
    assert [(line.reserved, line.pending) for line in db.get_lines(project.id)] == [(3, 0), (1, 2)]
    assert db.find_part(mpn="AAA").available == 0
    with pytest.raises(WorkflowError):
        db.plan_fill_from_stock(make_project("planning", [("AAA", 1)]).id)


ALT = "0527452096;59453-082110ECHLF"


def test_alternates_count_stock_of_either_part(db, make_project):
    db.adjust_stock(db.add_part("59453-082110ECHLF").id, 1)
    db.adjust_stock(db.add_part("0527452096").id, 1)
    project = make_project("p", [(ALT, 3)])
    line = db.get_lines(project.id)[0]
    assert (line.available, line.from_stock, line.to_order) == (2, 2, 1)
    db.commit_project(project.id)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending, line.substitutes) == (2, 1, [])
    # The remaining one is awaited as the first alternate.
    assert [l.line_id for l in db.expected_lines(db.find_part(mpn="0527452096").id)] == [line.id]


def test_any_alternate_fills_the_line_when_received(db, make_project):
    project = make_project("p", [(ALT, 2)])
    db.commit_project(project.id)
    assert db.find_part(mpn="59453-082110ECHLF") is None
    assert [l.refs for l in db.expected_lines_for_mpn("59453-082110echlf")] == ["R1"]
    result = db.receive("59453-082110ECHLF", 5)
    assert (result.placements[0].qty, result.free_qty) == (2, 3)
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending, line.substitutes) == (2, 0, [])
    assert db.find_part(mpn="0527452096").pending == 0
    part = db.find_part(mpn="59453-082110ECHLF")
    assert db.project_line_for_part(project.id, part.id) == line.id
    db.consume(part.id, 2, project.id)
    assert db.get_lines(project.id)[0].used == 2


def test_fill_from_stock_uses_alternates(db, make_project):
    project = make_project("p", [(ALT, 3)])
    db.commit_project(project.id)
    db.adjust_stock(db.add_part("59453-082110ECHLF").id, 2)
    plan = db.fill_from_stock(project.id)
    assert [(f.mpn, f.qty) for f in plan.filled] == [("59453-082110ECHLF", 2)]
    assert [(s.mpn, s.qty) for s in plan.short] == [("0527452096", 1)]
    line = db.get_lines(project.id)[0]
    assert (line.reserved, line.pending) == (2, 1)
    db.undo_last()
    assert db.get_lines(project.id)[0].pending == 3


def test_use_stock_for_line_with_an_alternate(db, make_project):
    project = make_project("p", [(ALT, 2)])
    db.commit_project(project.id)
    alt = db.add_part("59453-082110ECHLF")
    db.adjust_stock(alt.id, 5)
    line = db.expected_lines(alt.id)[0]
    db.use_stock_for_line(line.line_id, alt.id, 2)
    assert (db.get_lines(project.id)[0].reserved, db.get_lines(project.id)[0].pending) == (2, 0)


def test_order_export_sends_the_first_alternate(db, make_project, tmp_path):
    import csv as csv_module

    from partsdb.bom import BomRow

    project = db.create_project("p", [BomRow(refs=["J7"], qty=1, mpn=ALT, digikey_pn="X-ND;Y-ND")])
    path = tmp_path / "order.csv"
    db.export_order_csv(project.id, path)
    with open(path, newline="", encoding="utf-8") as handle:
        row = next(csv_module.DictReader(handle))
    assert (row["Manufacturer Part Number"], row["Digi-Key Part Number"]) == ("0527452096", "")


def test_similar_mpns_checks_each_alternate(db, make_project):
    project = make_project("p", [(ALT, 1)])
    db.commit_project(project.id)
    assert [mpn for mpn, _ in db.similar_mpns("59453-082110ECHL")] == ["59453-082110ECHLF"]
