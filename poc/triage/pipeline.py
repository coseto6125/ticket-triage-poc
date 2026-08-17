"""一封工單從進來到有結果的完整流程。

順序不能換：個資先在本機抽出來換成假名，之後才有東西可以送出去。任何一步失敗
都走 router.degraded，一律轉真人。
"""

from pathlib import Path

from triage import gemini, pii, prompt, reply, report, router, validate
from triage.ingest import Ticket


class Pipeline:
    """把分類器、抽取層與規則接起來。"""

    def __init__(self, classifier: gemini.Classifier, transport: pii.Transport) -> None:
        self.classifier = classifier
        self.transport = transport

    def run_one(self, ticket: Ticket) -> report.Row:
        fields = {
            "sender": ticket.customer_name,
            "subject": ticket.subject,
            "body": ticket.body,
        }
        try:
            entities = self.transport.extract(ticket.ticket_id, fields)
        except pii.ExtractionUnavailable as exc:
            return self._degraded(ticket, pii.Pseudonymizer([]), f"個資抽取層無法處理：{exc}")

        masked = pii.Pseudonymizer(entities)
        safe = {key: masked.apply(value) for key, value in fields.items()}
        # 送出去的東西只能來自這裡，原值不在這條路徑上
        text = prompt.ticket_text(
            ticket.ticket_id, ticket.channel, safe["sender"], safe["subject"], safe["body"]
        )
        if leaked := masked.leaked(text):
            return self._degraded(ticket, masked, f"假名化後仍偵測到原值：{'、'.join(leaked)}")

        try:
            answer = validate.parse(self.classifier.classify(ticket.ticket_id, text))
        except (gemini.ClassifierUnavailable, validate.InvalidAnswer) as exc:
            return self._degraded(ticket, masked, str(exc))

        residual = tuple(v for v in answer.residual_pii if not masked.covers(v))
        decision = router.decide(answer, residual)
        mode, draft, hits = reply.render(answer, decision, safe["sender"], masked)
        return report.Row(
            ticket=ticket,
            decision=decision,
            category=answer.scenarios[0],
            scenarios=answer.scenarios,
            is_complaint=answer.is_complaint,
            confidence=answer.confidence,
            pii_count=masked.entity_count,
            money_mentioned=answer.money_mentioned,
            draft=draft,
            reply_mode=mode,
            banned_hits=hits,
        )

    def _degraded(self, ticket: Ticket, masked: pii.Pseudonymizer, detail: str) -> report.Row:
        decision = router.degraded(detail)
        sender = masked.apply(ticket.customer_name)
        _, draft, _ = reply.render(None, decision, sender, masked)
        return report.Row(
            ticket=ticket,
            decision=decision,
            category="",
            scenarios=(),
            is_complaint=False,
            confidence=0.0,
            pii_count=masked.entity_count,
            money_mentioned="",
            draft=draft,
            reply_mode=router.ACK_ONLY,
            banned_hits=(),
        )

    def run(self, tickets: list[Ticket]) -> list[report.Row]:
        return [self.run_one(ticket) for ticket in tickets]


def build(*, refresh: bool = False, annotations: Path | None = None) -> Pipeline:
    """組出預設管線。"""
    return Pipeline(gemini.Classifier(refresh=refresh), pii.MockTransport(annotations))
