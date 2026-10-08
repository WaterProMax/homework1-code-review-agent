"""明确要求调用方提供分母，并处理分母为零的情况。"""


def ratio(total, count):
    if count == 0:
        raise ValueError('count must not be zero')
    return total / count
