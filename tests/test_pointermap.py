"""Learning where focused items are from the pointer, for the D-pad hand-back."""

from __future__ import annotations

from resources.lib.pointermap import PointerMap

ROW = ("Home", "5100", "")


def test_exact_spot_is_the_average_of_where_it_was_hovered():
    m = PointerMap()
    m.note(ROW, (2, 0), 500, 300)
    m.note(ROW, (2, 0), 540, 320)
    assert m.where(ROW, (2, 0)) == (520, 310)


def test_unseen_items_are_worked_out_from_the_pitch():
    m = PointerMap()
    m.note(ROW, (0, 0), 100, 300)
    assert m.where(ROW, (3, 0)) is None  # one item: no idea how far apart they are
    m.note(ROW, (1, 0), 400, 300)        # a horizontal row, 300 apart
    assert m.where(ROW, (3, 0)) == (1000, 300)
    assert m.where(ROW, (9, 0)) == (1919, 300)  # off screen: kept on it


def test_panels_learn_rows_and_columns_separately():
    grid = ("Videos", "50", "")
    m = PointerMap()
    m.note(grid, (0, 0), 200, 200)
    m.note(grid, (0, 1), 450, 200)
    m.note(grid, (1, 0), 200, 560)
    assert m.where(grid, (2, 3)) == (950, 920)


def test_places_are_separate_and_plain_controls_have_one_spot():
    m = PointerMap()
    m.note(("Home", "802", ""), None, 60, 1000)
    assert m.where(("Home", "802", ""), None) == (60, 1000)
    assert m.where(("Home", "804", ""), None) is None
    assert m.where(("Home", "5100", "5001:2"), (0, 0)) is None
    m.note(ROW, (0, 0), 100, 300)
    assert m.where(("Home", "5100", "5001:2"), (0, 0)) is None  # another scroll


def test_survives_a_round_trip_and_forgets_on_bad_data():
    m = PointerMap()
    m.note(ROW, (0, 0), 100, 300)
    m.note(ROW, (1, 0), 400, 300)
    m.note(("Home", "802", ""), None, 60, 1000)
    copy = PointerMap()
    copy.load(m.dump())
    assert copy.where(ROW, (2, 0)) == (700, 300)
    assert copy.where(("Home", "802", ""), None) == (60, 1000)
    copy.load({"spots": "nonsense"})
    assert copy.where(ROW, (0, 0)) is None
