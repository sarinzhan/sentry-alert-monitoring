"""Flow and step identity — the ONLY flow data that lives in code.

These slugs are stable identifiers used by tests (via @pytest.mark.flow) and by
the testops DB/UI. Titles, descriptions, ordering and Sentry signal patterns are
NOT here — they are edited in the UI and stored in testops.db.

Slugs are append-only: renaming one orphans its DB metadata and test links.
"""


class Flow:
    IDENTIFICATION_PERSONIFICATION = "identification-personification"


class Step:
    RESOLVE = "resolve"
    SUBMIT_DOCUMENTS = "submit-documents"
    AML_CHECK = "aml-check"
    PHOTO_VALIDATION = "photo-validation"
    PERSONIFICATION = "personification"
