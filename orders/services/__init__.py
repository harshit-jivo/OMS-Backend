"""Order business rules, with no HTTP in them (one deliberate exception below).

Plan item 3.2. The target shape is the one `payments` already has: models hold
data, serializers hold shape, views parse and respond, and the rules live here
where they can be read and tested without a request.

`scheme_engine` and `scheme_rules` predate this package and were moved in
from the top of the `orders` app, so every order rule now sits together:

    order_flow.py       which status an order moves to next
    order_items.py      what is shipped, and the free goods a line earns
    rate_approval.py    who must approve an off-price line
    order_status.py     UpdateOrderStatusView's branch dispatch — a pure,
                        mechanical extraction that (unlike everything else
                        here) returns DRF Response objects directly; see its
                        own docstring for why
    order_templates.py  whether an order duplicates a saved template
    stock_check.py      whether there is enough on hand
    scheme_engine.py    computes scheme proposals
    scheme_rules.py     scheme predicates (see the Punjab question in
                        sap_sync/tests.py before changing anything here)
"""
