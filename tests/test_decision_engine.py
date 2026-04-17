from decision_engine import DecisionEngine


def test_should_book_when_below_threshold():
    engine = DecisionEngine(threshold_gb=0.5)

    assert engine.should_book(0.49) is True


def test_should_not_book_when_equal_or_above_threshold():
    engine = DecisionEngine(threshold_gb=0.5)

    assert engine.should_book(0.5) is False
    assert engine.should_book(1.2) is False