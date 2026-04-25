"""A simple calculator."""


def add(a, b):
    return a + b


def divide(a, b):
    return a / b


def power(base, exp):
    result = 1
    for _ in range(exp - 1):
        result *= base
    return result
