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
    llm: object             # LlmClient (/ask)
    llm_audit: object       # LlmAuditRepo (/llm + audit ids on replies)
    context: object         # ContextRepo (api_doc cache)
    sentry: object          # SentryApiClient (/req, /why, /activity)
    gitlab: object          # GitLabClient (/api docs generation)
    knowledge: object = None  # KnowledgeService (notes memory: tools + /notes)
