"""Single-file test app for the reusable SelfHeal agent. Run it yourself."""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import math
import operator
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def calculate(expression: str) -> float | int | None:
    """Arithmetic only. Zero division is meant to return None.

    Three intentional test bugs: subtraction, remainder and zero division.
    AI may change this function's body; all app/control/check code is protected.
    """
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 256:
        raise ValueError("Enter an arithmetic expression of at most 256 characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError("Invalid arithmetic expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError("Expression is too complex")
    # Fixed operator mappings: subtraction and remainder were incorrect.
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def evaluate(node):
        # Evaluate constants.
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        # Evaluate unary + and -.
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            number = evaluate(node.operand)
            if number is None:
                return None
            return number if isinstance(node.op, ast.UAdd) else -number
        # Evaluate binary operations.
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            left = evaluate(node.left)
            right = evaluate(node.right)
            # Propagate None from sub‑expressions (e.g., division by zero).
            if left is None or right is None:
                return None
            # Guard against division or modulo by zero.
            if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
                return None
            # Exponent limits.
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("Exponent must be between -12 and 12")
            value = binary[type(node.op)](left, right)
            # After computing, ensure the result is a supported numeric value.
            if isinstance(value, complex) or not math.isfinite(value) or abs(value) > 1e12:
                raise ValueError("Result is outside the supported numeric range")
            return value
        raise ValueError("Only numbers, parentheses and + - * / % ** are allowed")

    return evaluate(tree.body)


def calculator_health(module):
    """Trusted contract: this callable is retained by the agent across reloads."""
    from selfheal import HealthCheckResult, HealthReport

    cases = (("addition", "2 + 3", 5), ("subtraction", "8 - 3", 5),
             ("negative subtraction", "3 - 8", -5), ("multiply", "6 * 7", 42),
             ("precedence", "2 + 3 * 4", 14), ("parentheses", "(2 + 3) * 4", 20),
             ("division", "8 / 2", 4), ("remainder", "10 % 3", 1),
             ("unary", "-4 + 2", -2), ("power", "2 ** 3", 8),
             ("decimals", "0.5 + 0.25", 0.75), ("zero division", "10 / 0", None),
             ("nested zero division", "2 + (10 / 0)", None))
    checks = []
    for name, expression, expected in cases:
        try:
            value = module.calculate(expression)
            ok = value is None if expected is None else type(value) in (int, float) and math.isclose(value, expected)
            details = f"{expression}: expected {expected!r}, got {value!r}"
        except Exception as exc:
            ok, details = False, f"{expression}: raised {type(exc).__name__}"
        checks.append(HealthCheckResult(name, ok, details))
    for expression in ("__import__('os')", "1.__class__", "[1, 2]", "True + 1", "", "2 ** 99"):
        try:
            module.calculate(expression)
        except ValueError:
            ok = True
        except Exception:
            ok = False
        else:
            ok = False
        checks.append(HealthCheckResult("reject invalid expression", ok, repr(expression)))
    return HealthReport(tuple(checks))


PAGE = """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Live calculator</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#111827;color:#e5e7eb;font:16px system-ui;display:grid;min-height:100dvh;place-items:center;padding:24px}
main{width:min(100%,520px);background:#1f2937;padding:30px;border-radius:18px}h1{font-size:28px;margin:12px 0}p{color:#b8c2d1;line-height:1.6}
form{display:flex;gap:10px}input{min-width:0;width:100%;padding:14px;border:1px solid #64748b;border-radius:8px;background:#111827;color:white;font:inherit}
button{padding:12px 16px;border:0;border-radius:8px;background:#a7f3d0;color:#064e3b;font-weight:700;cursor:pointer}output{display:block;overflow-wrap:anywhere;font-size:28px;min-height:60px;padding:20px 0}
small{color:#a7f3d0}code{color:#d1fae5}.examples{display:flex;flex-wrap:wrap;gap:8px}.examples button{background:#374151;color:#e5e7eb;font-weight:400}
@media(max-width:380px){main{padding:20px}form{flex-direction:column}}
</style><main><small id=\"state\">Connecting...</small><h1>Live calculator</h1>
<p>Keep this page open while SelfHeal repairs the running app.</p>
<form id=\"form\"><input id=\"expression\" aria-label=\"Arithmetic expression\" value=\"8 - 3\" autocomplete=\"off\"><button>Calculate</button></form>
<output id=\"result\" aria-live=\"polite\">Ready</output><div class=\"examples\">
<button type=\"button\" data-expression=\"8 - 3\">8 - 3</button><button type=\"button\" data-expression=\"10 % 3\">10 % 3</button>
<button type=\"button\" data-expression=\"10 / 0\">10 / 0</button><button type=\"button\" data-expression=\"6 * 7\">6 * 7</button></div>
<p>This sample starts with deliberate bugs. Use the terminal heal command, then calculate again.</p></main>
<script>
const expression=document.getElementById('expression'), result=document.getElementById('result');
async function calculate(){result.textContent='Calculating...';try{const r=await fetch('/api/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expression:expression.value})});
const data=await r.json();result.textContent=r.ok?(data.result===null?'Undefined (safe zero division)':String(data.result)):data.error;}catch(e){result.textContent='Application connection unavailable';}}
document.getElementById('form').addEventListener('submit',e=>{e.preventDefault();calculate();});
document.querySelectorAll('[data-expression]').forEach(b=>b.addEventListener('click',()=>{expression.value=b.dataset.expression;calculate();}));
async function status(){try{const r=await fetch('/api/status'),s=await r.json();const a=s.agent;document.getElementById('state').textContent='PID '+s.pid+' · '+(s.busy||a.active_target?'Healing':a.paused?'Agent paused':'Running')+(s.healing_disabled?' · restart required':'');}catch(e){document.getElementById('state').textContent='Application offline';}}
status();setInterval(status,2000);
</script></html>"""


def main() -> int:
    from selfheal import (AgentConfig, LiveControlServer, LiveTarget, SelfHealAgent,
                          SelfHealConfig, SelfHealRuntime, TerminalReporter)
    from selfheal.providers import provider_from_env

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--control-port", type=int, default=8766)
    parser.add_argument("--session", type=Path, default=Path(".selfheal/live.json"))
    parser.add_argument("--interval", type=float, default=300, help="Background check interval in seconds")
    parser.add_argument("--auto-apply", action="store_true", help="Allow scheduled verified patches as well as explicit commands")
    parser.add_argument("--scan-healthy", action="store_true", help="Also ask AI to inspect when local health checks pass")
    args = parser.parse_args()
    # Import a separate module so reloading never re-runs __main__ or restarts the server.
    module = importlib.import_module(Path(__file__).stem)
    runtime = SelfHealRuntime(SelfHealConfig(Path(__file__).parent, run_tests=False),
                              provider=provider_from_env(max_output_tokens=8192), event_sink=TerminalReporter())
    target = LiveTarget("calculator", module.__name__, "calculate", calculator_health,
                        instructions="Fix arithmetic bugs. Preserve safe AST evaluation and numeric limits. "
                        "Any division by zero, even nested, must return None for the complete expression. "
                        "Invalid expressions must raise ValueError. Only change the calculate function body.",
                        editable_symbols=("calculate",), retry_safe=True)
    agent = SelfHealAgent(runtime, (target,), config=AgentConfig(
        interval_seconds=args.interval, auto_apply=args.auto_apply, scan_when_healthy=args.scan_healthy))
    control = LiveControlServer(runtime, (target,), port=args.control_port, session_file=args.session, agent=agent)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload, content_type="application/json; charset=utf-8"):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/":
                self.reply(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                # Show state only; the local control credential is never sent to the browser.
                self.reply(200, control.status())
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/api/calculate":
                self.reply(404, {"error": "Not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or type(data.get("expression")) is not str:
                    raise ValueError("Supply an expression string")
                # Manual errors do not trigger AI here. Background checks and CLI own repairs.
                value = runtime.invoke(module_name=target.module_name, symbol_name=target.symbol_name,
                                       args=(data["expression"],))
                self.reply(200, {"result": value, "pid": os.getpid()})
            except (ValueError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception as exc:
                self.reply(500, {"error": f"{type(exc).__name__}: run the terminal heal command"})

    try:
        with control, agent, ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
            print(f"Calculator: http://127.0.0.1:{server.server_address[1]} | PID: {os.getpid()}", flush=True)
            print(f"Session: {control.session_file}", flush=True)
            print("Another terminal: python -m selfheal heal --target calculator", flush=True)
            print("Ctrl+C stops the app after accepted repairs finish.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nStopping the calculator and heal agent...", flush=True)
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Setup stopped: {exc}")
        raise SystemExit(2)
