from .models import Assessment, PullRequest

REVIEW_DEPTH = {
    "low": "✅ **Low risk** - a quick look is enough.",
    "medium": "⚠️ **Medium risk** - check the risky parts carefully.",
    "high": "🚨 **High risk** - needs an in-depth review before merging.",
}


def with_assessment(pr: PullRequest, assessment: Assessment) -> PullRequest:
    """Puts how carefully to review on top of the PR, and the findings the author kept at the bottom."""
    body = f"> {REVIEW_DEPTH[assessment.risk]} {assessment.risk_rationale}\n\n{pr.body}"
    if assessment.kept:
        kept = "\n".join(f"- {finding}" for finding in assessment.kept)
        body += f"\n\n## Review findings kept as they are\n\n{kept}"
    return PullRequest(title=pr.title, body=body)


CUT_NOTE = "\n\n[...] cut to fit the host's description limit"


def fit(body: str, limit: int) -> str:
    """Cuts the body at a line break so it fits the host's limit, instead of failing to create the PR."""
    if len(body) <= limit:
        return body
    keep = body[: limit - len(CUT_NOTE)]
    if "\n" in keep:
        keep = keep[: keep.rindex("\n")]
    return keep.rstrip() + CUT_NOTE
