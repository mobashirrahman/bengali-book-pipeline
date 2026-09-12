"""Bengali/Bangla book-digitization pipeline built on the pdf-craft fork.

`book` and `publish` are wired into this package's CLI (`python -m
pdf_craft_tool <command> --help`). cluster/cluster_worker, gold_server,
review_server, benchmark, and external_eval are separate, independently
runnable modules: `python -m pdf_craft_tool.<module> --help`.
"""
