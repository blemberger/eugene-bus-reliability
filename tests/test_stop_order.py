from eugene_bus_reliability.stop_order import merge_patterns


def test_one_pattern():
    order, places = merge_patterns([["a", "b", "c"]])
    assert order == ["a", "b", "c"]
    assert places == [[0, 1, 2]]


def test_short_trip_starting_part_way():
    order, places = merge_patterns([["a", "b", "c", "d"], ["c", "d"]])
    assert order == ["a", "b", "c", "d"]
    assert places[1] == [2, 3]


def test_loop_end_matches_the_end_not_the_start():
    # a loop from the station S and back; a short trip starting part way ends at S too
    order, places = merge_patterns([["S", "a", "b", "c", "S"], ["b", "c", "S"]])
    assert order == ["S", "a", "b", "c", "S"]
    assert places[1] == [2, 3, 4]


def test_stops_only_some_trips_make_go_between_their_neighbours():
    order, places = merge_patterns([["a", "b", "c", "d"], ["x", "a", "b", "y", "d", "z"]])
    assert order == ["x", "a", "b", "y", "c", "d", "z"]
    assert places[0] == [1, 2, 4, 5]
    assert places[1] == [0, 1, 2, 3, 5, 6]


def test_nothing_in_common_goes_after():
    order, places = merge_patterns([["a", "b"], ["x", "y"]])
    assert order == ["a", "b", "x", "y"]
    assert places == [[0, 1], [2, 3]]
