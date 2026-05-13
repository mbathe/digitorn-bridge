"""Cron module: hosts background sweepers in a worker process.

When the cron module is loaded by a Digitorn worker (declared in
``workers.workers[].modules``), it acquires a file-based leader lock
and runs the activation sweep loop that would otherwise live in the
daemon's lifespan. Single-leader semantics ensure that even if two
workers list ``cron`` in their modules, only one actually runs the
sweep -- the other stays idle until the leader releases / dies.

See ``module.py`` for the implementation.
"""
