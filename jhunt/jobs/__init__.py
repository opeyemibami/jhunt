"""Job-board knowledge shared by the workflows (ADR 0009): job links and the
boards' public APIs, posting keys, and text from job HTML.

A layer between jhunt.workflows and jhunt.platform: workflows may import it,
it imports only the platform (and libraries), never a workflow.
"""
