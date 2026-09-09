"""Red case for python:S3776, cognitive complexity cap 15.

If the loop stops flagging this function, the complexity gate has broken and a
clean result against src/ means nothing. Do not simplify it.
"""


def deliberately_over_complex(rows, mode, limit, flag):  # noqa: C901
    total = 0
    for row in rows:
        if mode == "a":
            if row > limit:
                if flag:
                    total += row
                else:
                    total -= row
            elif row < 0:
                while row < 0:
                    row += 1
                    if row == limit:
                        break
            else:
                total += 1
        elif mode == "b":
            for other in rows:
                if other > row:
                    if other % 2 == 0:
                        total += other
                    else:
                        total -= other
                elif other == row:
                    continue
        else:
            try:
                total += int(row)
            except (TypeError, ValueError):
                if flag:
                    total = 0
                else:
                    total -= 1
    return total
