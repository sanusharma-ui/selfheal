# Deeper checks and method targets

This release candidate extends detection through developer-owned scenarios and
bounded generated cases. It does not discover every bug or infer business rules
without a trusted contract. Final native, regression and wheel validation is
pending; see [the release guide](RELEASING.md).

## Methods and properties

Use dotted names for definitions directly owned by a class in the target module:

| Target | Invocation through `agent.bind(...)` |
| --- | --- |
| `Pricing.quote` (instance method) | `quote(instance, price)` |
| `Pricing.quote` (static method) | `quote(price)` |
| `Pricing.quote` (class method) | `quote(price)`; the class is bound automatically |
| `Pricing.total` or `Pricing.total.fget` | `total(instance)` |
| `Pricing.total.fset` | `set_total(instance, value)` |
| `Pricing.total.fdel` | `delete_total(instance)` |
| `Pricing.__call__` | `call(instance, ...)` |

Nested classes and async methods follow the same naming convention. All
participating calls must use runtime/agent dispatch, including property access;
ordinary `instance.total` reads bypass coordination. Existing class instances,
descriptors and function objects are retained. Code using zero-argument `super()`
retains its original class cell. Class attributes, inheritance, signatures and
decorators remain protected by the source scope check.

Inherited methods must be registered on their defining class/module. Custom
metaclasses, arbitrary decorator closures, nested function closures, generators,
async generators and native functions remain outside live activation support.
These cases need an application-specific restart/replacement strategy; this SDK
does not currently implement that strategy or multi-file patch transactions.

## Trusted scenarios and generated cases

Install the optional extra in the application's environment:

```shell
python -m pip install "selfheal-runtime[verification,exploration]"
```

Keep health definitions outside the editable functions. For an application module
with `Cart.add(price, quantity)`, `Cart.remove(price, quantity)` and `Cart.total()`:

```python
from hypothesis import strategies as st
from selfheal import HealthSuite, PropertyCheck, ScenarioCheck, LiveTarget


def quantity_cases():
    return st.tuples(st.integers(0, 10_000), st.integers(1, 50))


def quantity_contract(module, case):
    price, quantity = case
    cart = module.Cart()  # fresh, disposable state for EVERY evaluation
    cart.add(price, quantity)
    return cart.total() == price * quantity


def add_remove_contract(module):
    cart = module.Cart()
    cart.add(100, 3)
    cart.remove(100, 1)
    return cart.total() == 200


health = HealthSuite(
    ScenarioCheck("add/remove workflow", add_remove_contract),
    PropertyCheck("quantity arithmetic", quantity_cases, quantity_contract,
                  examples=((100, 3),), max_examples=100, max_evaluations=1000),
)
target = LiveTarget(
    "cart", "myapp.cart", "Cart.add", health,
    editable_symbols=("Cart.add", "Cart.remove", "Cart.total"),
    retry_safe=True,
    context_files=("myapp/pricing_rules.py",),
)
```

These health callbacks integrate with existing `inspect`, `ainspect` and agent
scans. The convenience checks use synchronous predicates; existing async health
callbacks remain supported separately. To explore workflow sequences, supply a
strategy producing bounded lists of actions and a predicate that constructs a
fresh object, executes those actions and checks trusted invariants after each.
Concurrency and cancellation scenarios require application-specific controls;
there is no automatic race-condition detector.

`PropertyCheck` uses deterministic generation and shrinking with no Hypothesis
example database. It retains up to eight discovered counterexamples in memory
and replays them before subsequent searches, including candidate and activation
checks. Explicit `examples` are also always replayed. Counterexamples are not
persisted across application restarts. `max_evaluations` caps predicate calls,
including shrinking/replays; exhaustion is inconclusive and refuses acceptance.
The runtime's shared repair deadline still applies.

The default report omits input values. If synthetic examples are safe to send to
the configured AI provider, pass a trusted `describe_case` callback to supply a
bounded explanation. Do not expose real user payloads. A passing report means
no counterexample was found within the configured search, not proof of correctness.

## Read-only context and repair evidence

`context_files` accepts at most eight explicitly selected relative `.py` paths
inside `project_root`, with a combined 16 KB limit. It is forwarded through agent
scans and local heal/improve commands. Direct `inspect`/`ainspect` calls accept
the same keyword. No dependency code is imported to collect this context.

Selected files are sent to the provider as read-only context, checked for known
secrets, and compared again before activation/commit. Their presence does not
expand `editable_symbols` or authorize a second file to change. A cause outside
the allowed scope should yield no proposal rather than an exception-suppressing
patch. The runtime still permits only one candidate per repair transaction.

Baseline contracts expose incorrect behavior; candidate checks run on a
serialized object graph in a disposable process. Pending function code changes
are applied there, preserving relationships between isolated class instances,
descriptors, helpers and aliases. The running process is unchanged until approved
activation. Final live checks still gate commit, with source/function restoration
on failure. External side effects and arbitrary memory changes are not rolled back.

References: [Hypothesis property testing](https://hypothesis.readthedocs.io/en/latest/tutorial/introduction.html),
[stateful testing](https://hypothesis.readthedocs.io/en/latest/stateful.html),
[Python function and method objects](https://docs.python.org/3.13/reference/datamodel.html).
