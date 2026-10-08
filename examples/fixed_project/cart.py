"""修复默认容器共享问题。"""


def add_item(item, cart=None):
    if cart is None:
        cart = []
    cart.append(item)
    return cart
