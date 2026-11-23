import random

import pytest

from engine import CHOP, DOLL, VISITOR, DOLLS_BY_COUNT, Game, Phase


def make(n, seed=0):
    g = Game(1, 1, rng=random.Random(seed))
    for i in range(n):
        g.add_player(i, f"P{i}")
    g.start()
    return g


@pytest.mark.parametrize("n", range(4, 21))
def test_role_counts(n):
    g = make(n)
    assert len(g.alive(CHOP)) == 2
    assert len(g.alive(DOLL)) == DOLLS_BY_COUNT[n]
    assert len(g.alive(VISITOR)) == n - 2 - DOLLS_BY_COUNT[n]
    assert len(g.items_left) == 6


def test_min_players():
    g = Game(1, 1)
    for i in range(3):
        g.add_player(i, "x")
    with pytest.raises(ValueError):
        g.start()


def test_double_detain_single_victim_and_check():
    g = make(8)
    g.begin_night()
    c1, c2 = [p.id for p in g.alive(CHOP)]
    victim = next(p for p in g.alive(VISITOR)).id
    g.chop_act(c1, "detain", victim)
    g.chop_act(c2, "detain", victim)
    res = g.resolve_night()
    assert [p.id for p in res.detained] == [victim]


def test_check_returns_role_and_is_final():
    g = make(6)
    g.begin_night()
    c1 = g.alive(CHOP)[0].id
    t = g.alive(DOLL)[0]
    assert g.chop_act(c1, "check", t.id) == DOLL
    with pytest.raises(ValueError):
        g.chop_act(c1, "detain", t.id)


def test_no_votes_no_theft_and_tie_steals_one():
    g = make(6)
    g.begin_night()
    assert g.resolve_night().stolen is None
    g.begin_night()
    d1, d2 = [p.id for p in g.alive(DOLL)]
    g.doll_vote(d1, g.items_left[0])
    g.doll_vote(d2, g.items_left[1])
    assert g.resolve_night().stolen is not None
    assert len(g.items_left) == 5


def test_chop_wins_when_last_doll_and_last_item_same_night():
    g = make(4)
    g.items_left = g.items_left[:1]
    g.begin_night()
    doll = g.alive(DOLL)[0]
    g.doll_vote(doll.id, g.items_left[0])
    g.chop_act(g.alive(CHOP)[0].id, "detain", doll.id)
    res = g.resolve_night()
    assert res.winner == CHOP


def test_dolls_win_by_stealing_everything():
    g = make(6)
    for _ in range(6):
        g.begin_night()
        for d in g.alive(DOLL):
            g.doll_vote(d.id, g.items_left[0])
        res = g.resolve_night()
    assert res.winner == DOLL and not g.items_left


def test_random_games_terminate():
    for seed in range(300):
        rng = random.Random(seed)
        g = make(rng.randint(4, 20), seed)
        for _ in range(50):
            g.begin_night()
            for c in g.alive(CHOP):
                t = rng.choice([p for p in g.alive() if p.id != c.id])
                g.chop_act(c.id, rng.choice(["check", "detain"]), t.id)
            for d in g.alive(DOLL):
                g.doll_vote(d.id, rng.choice(g.items_left))
            assert g.night_complete()
            if g.resolve_night().winner:
                break
        else:
            pytest.fail("игра не завершилась")
        assert g.winner() in (CHOP, DOLL)
