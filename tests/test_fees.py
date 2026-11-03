import pytest
from fees import side_fee, round_trip_fees


@pytest.mark.parametrize("t,exp", [
    (5, 0.09),          # 5*2%=0.10, -10% = 0.09
    (20, 0.36),         # 0.40*0.9
    (47.50, 0.855),     # 0.95 (2% == flat at boundary) *0.9
    (100, 0.855),       # flat 0.95*0.9
    (190, 0.855),       # flat 0.95*0.9
    (200, 0.9),         # 200*0.5%=1.00*0.9
    (1000, 4.5),        # 5.00*0.9
])
def test_side(t, exp):
    assert side_fee(t) == pytest.approx(exp)


def test_no_discount():
    assert side_fee(100, discount=0) == pytest.approx(0.95)


def test_round_trip():
    assert round_trip_fees(47.50) == pytest.approx(1.71)
    assert round_trip_fees(100, 300) == pytest.approx(0.855 + 1.35)
