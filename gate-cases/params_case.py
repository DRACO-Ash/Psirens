"""Red case for python:S107, parameter cap 13.

Fourteen parameters. This is the rule that failed the 1.5.0 upload on
`_elset_dict` at eighteen, and the fix there was to group related arguments
into a value object rather than widen the signature. Do not "fix" this one.
"""


def deliberately_too_many_parameters(a, b, c, d, e, f, g, h, i, j, k, m, n, p):
    return (a, b, c, d, e, f, g, h, i, j, k, m, n, p)
