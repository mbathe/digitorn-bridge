"""Unit tests for calculator."""

from src.calculator import add, subtract, multiply, divide, power, factorial


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 5) == -5


def test_multiply():
    assert multiply(3, 4) == 12
    assert multiply(0, 100) == 0


def test_divide():
    assert divide(10, 2) == 5
    # Test division by zero returns None
    assert divide(5, 0) is None
    assert divide(0, 0) is None


def test_power():
    # These tests will FAIL because of the off-by-one bug
    assert power(2, 3) == 8
    assert power(5, 2) == 25


def test_factorial():
    assert factorial(5) == 120
    assert factorial(0) == 1
