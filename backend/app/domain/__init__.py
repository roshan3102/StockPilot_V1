"""Pure, deterministic business rules. No database, no network, no LLM, no clock.

Everything here takes plain values in and returns plain values out, so it can be unit
tested without any setup. Money is integer cents; quantities are integer units.
"""
