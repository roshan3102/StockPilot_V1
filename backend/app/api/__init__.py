"""HTTP layer: routes, dependencies and error handling. Thin by design.

A route validates input, calls ONE service function, and shapes the result with a Pydantic
model. It contains no business rules and does not import `app.domain` (a test enforces this).
"""
