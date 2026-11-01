"""Checkout tests. One fails and prints a fake credential, to show that secrets are redacted in bug reports."""


def apply_shipping(subtotal):
    return subtotal + (0 if subtotal >= 50 else 5)


def test_free_shipping_at_threshold():
    assert apply_shipping(50) == 50


def test_shipping_charged_below_threshold():
    assert apply_shipping(49) == 54


def test_payment_gateway_call():
    print("calling gateway with api_key=sk-demo-0123456789abcdefghij and user buyer@shop-demo.io")
    status = 502  # a stand-in for a real gateway response
    assert status == 200, f"gateway returned {status}"
