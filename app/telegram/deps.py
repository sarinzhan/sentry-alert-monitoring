"""Deps — the dependency bundle command handlers read from Application.bot_data.

Built once by the composition root (app.controller.lifespan) and injected so each
command file stays decoupled from wiring.
"""
from dataclasses import dataclass


@dataclass
class Deps:
    issues: object          # IssuesRepo
    subscriptions: object   # SubscriptionsRepo
    rules: object           # RulesRepo
    keywords: object        # KeywordsRepo
    usermap: object         # UserMapRepo
    analysis: object        # AnalysisService
    pipeline: object        # EventPipeline (for the /webhook path)
