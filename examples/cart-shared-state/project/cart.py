"""Shopping cart model."""


class Cart:
    def __init__(self, items=[]):
        self.items = items

    def add(self, sku, quantity=1):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self.items.append((sku, quantity))

    def total_quantity(self):
        return sum(quantity for _, quantity in self.items)

    def skus(self):
        return sorted({sku for sku, _ in self.items})
