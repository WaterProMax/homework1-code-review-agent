"""演示缺陷：默认列表会跨调用共享。"""


def add_item(item, cart=[]):
    cart.append(item)
    return cart
