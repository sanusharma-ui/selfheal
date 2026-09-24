from selfheal.registry import FunctionRegistry


def test_dynamic_handle_resolves_current_function():
    handle = FunctionRegistry().handle("json", "dumps")
    assert handle({"ok": True}) == '{"ok": true}'
