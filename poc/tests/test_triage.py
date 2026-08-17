"""分流管線的測試。

跑法：cd poc && uv run python -m unittest discover tests

測試呼叫實際的函式，不複製一份被測邏輯到測試裡；分類器一律走重跑既有回應的路徑，
所以整套測試不需要金鑰、也不會消耗額度。
"""

import csv
import json
import os.path
import unittest
from pathlib import Path

from triage import bootloader, catalog, gemini, ingest, pii, pipeline, reply, report, router, validate

SOURCE = bootloader.POC_ROOT.parent / "requirements" / "sample_tickets.xlsx"
TICKETS = ingest.load(SOURCE)
BY_ID = {t.ticket_id: t for t in TICKETS}


def _answer(**overrides) -> validate.Answer:
    """組一個合法的分類結果，測試只覆寫關心的欄位。"""
    base = {
        "reason": "客戶詢問產品內容。",
        "disposition": catalog.ACCEPT,
        "assignment": catalog.SUPPORT,
        "scenarios": ("product_info",),
        "is_complaint": False,
        "is_irreversible": False,
        "money_mentioned": "",
        "residual_pii": (),
        "confidence": 0.9,
    }
    return validate.Answer(**(base | overrides))


class TestIngest(unittest.TestCase):
    def test_loads_all_48_tickets(self):
        self.assertEqual(len(TICKETS), 48)

    def test_excel_serial_becomes_taipei_datetime(self):
        first = BY_ID["TK-1001"].created_at
        self.assertEqual(first.isoformat(), "2026-06-22T09:14:00+08:00")

    def test_html_and_signature_are_stripped(self):
        ticket = BY_ID["TK-1020"]
        self.assertNotIn("<br>", ticket.body)
        self.assertNotIn("從我的 iPhone 傳送", ticket.body)
        self.assertIn("HTML 換行標籤", ticket.notes)

    def test_fullwidth_digits_become_halfwidth_but_punctuation_stays(self):
        ticket = BY_ID["TK-1007"]
        self.assertIn("3月28日", ticket.body)
        self.assertIn("，", ticket.body)

    def test_short_body_is_flagged(self):
        self.assertIn("內文過短", BY_ID["TK-1033"].notes)


class TestPII(unittest.TestCase):
    def setUp(self):
        self.transport = pii.MockTransport()

    def test_every_ticket_round_trips_without_leaking(self):
        for ticket in TICKETS:
            fields = {
                "sender": ticket.customer_name,
                "subject": ticket.subject,
                "body": ticket.body,
            }
            masked = pii.Pseudonymizer(self.transport.extract(ticket.ticket_id, fields))
            swapped = {k: masked.apply(v) for k, v in fields.items()}
            with self.subTest(ticket=ticket.ticket_id):
                self.assertEqual(masked.leaked("\n".join(swapped.values())), [])
                self.assertEqual(masked.restore(swapped["body"]), ticket.body)

    def _assert_near(self, ticket_id: str, shorter: str, longer: str) -> None:
        """兩個原值共用前綴時，換出來的假值也必須共用同樣長度的前綴，且不相等。"""
        ticket = BY_ID[ticket_id]
        fields = {"sender": ticket.customer_name, "subject": ticket.subject, "body": ticket.body}
        masked = pii.Pseudonymizer(self.transport.extract(ticket_id, fields))
        a, b = masked.apply(shorter), masked.apply(longer)
        shared = len(os.path.commonprefix([shorter, longer]))
        self.assertNotEqual(a, b)
        self.assertEqual(a[:shared], b[:shared])

    def test_near_identical_passport_names_stay_near_identical(self):
        """TK-1028 的兩個護照英文名只差一個字母，換成假名後也必須只差在字尾。"""
        self._assert_near("TK-1028", "HUNG MEI LI", "HUNG MEI LII")

    def test_near_identical_tax_ids_stay_near_identical(self):
        """TK-1010 的錯誤與正確統編只差最後一碼，假值也必須只差最後一碼。"""
        self._assert_near("TK-1010", "12345677", "12345678")

    def test_uncovered_ticket_raises_instead_of_guessing(self):
        with self.assertRaises(pii.ExtractionUnavailable):
            self.transport.extract("TK-9999", {"body": "任何內容"})

    def test_fakes_are_not_reported_as_residual(self):
        masked = pii.Pseudonymizer([pii.Entity("person_name", "林佳蓉")])
        self.assertTrue(masked.covers(masked.apply("林佳蓉")))


class TestValidate(unittest.TestCase):
    def _raw(self, **overrides) -> dict:
        base = {
            "reason": "客戶詢問產品內容。",
            "disposition": "accept",
            "assignment": "support",
            "scenarios": ["product_info"],
            "is_complaint": False,
            "is_irreversible": False,
            "money_mentioned": "",
            "residual_pii": [],
            "confidence": 0.9,
        }
        return base | overrides

    def test_accepts_a_well_formed_answer(self):
        self.assertEqual(validate.parse(self._raw()).primary.name, "product_info")

    def test_rejects_confidence_above_one(self):
        """實際發生過：模型回了 1.95。"""
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(confidence=1.95))

    def test_rejects_boolean_confidence(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(confidence=True))

    def test_rejects_disposition_that_contradicts_the_scenario(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(scenarios=["spam_fraud"]))

    def test_rejects_assignment_that_contradicts_the_scenario(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(scenarios=["non_support"]))

    def test_rejects_scenarios_from_two_different_dispositions(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(scenarios=["product_info", "spam_fraud"]))

    def test_rejects_unknown_scenario(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(scenarios=["refund_please"]))

    def test_rejects_empty_scenarios(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(self._raw(scenarios=[]))

    def test_rejects_rejected_ticket_marked_as_complaint(self):
        with self.assertRaises(validate.InvalidAnswer):
            validate.parse(
                self._raw(
                    disposition="reject",
                    assignment="not_applicable",
                    scenarios=["spam_fraud"],
                    is_complaint=True,
                )
            )


class TestRouter(unittest.TestCase):
    def test_invoice_work_always_goes_to_a_human(self):
        """開立與更正發票屬不可逆動作，不論模型怎麼判客訴都必須轉真人。"""
        for is_complaint in (True, False):
            decision = router.decide(_answer(scenarios=("payment_billing",), is_complaint=is_complaint), ())
            with self.subTest(is_complaint=is_complaint):
                self.assertEqual(decision.route, router.HUMAN)

    def test_amount_does_not_change_the_decision(self):
        """金額只做記錄，不進決策（docs/adr/0002）。"""
        small = router.decide(_answer(scenarios=("cancel_refund",), money_mentioned="500 元"), ())
        large = router.decide(_answer(scenarios=("cancel_refund",), money_mentioned="500,000 元"), ())
        self.assertEqual(small.route, large.route)
        self.assertEqual(small.triggers, large.triggers)

    def test_residual_pii_forces_human(self):
        decision = router.decide(_answer(), ("0912345678",))
        self.assertEqual(decision.route, router.HUMAN)
        self.assertIn("殘留個資", decision.triggers)

    def test_low_confidence_forces_human(self):
        self.assertEqual(router.decide(_answer(confidence=0.5), ()).route, router.HUMAN)

    def test_referral_forces_human(self):
        decision = router.decide(_answer(scenarios=("non_support",), assignment=catalog.REFERRAL), ())
        self.assertEqual(decision.route, router.HUMAN)

    def test_complaint_gets_acknowledgement_only(self):
        decision = router.decide(_answer(is_complaint=True), ())
        self.assertEqual(decision.reply_mode, router.ACK_ONLY)
        self.assertEqual(decision.priority, "P1")

    def test_clean_enquiry_stays_auto(self):
        decision = router.decide(_answer(), ())
        self.assertEqual((decision.route, decision.reply_mode), (router.AUTO, router.TEMPLATE))

    def test_rejected_ticket_closes_without_a_reply(self):
        decision = router.decide(
            _answer(
                disposition=catalog.REJECT,
                assignment=catalog.NOT_APPLICABLE,
                scenarios=("spam_fraud",),
            ),
            (),
        )
        self.assertEqual((decision.route, decision.reply_mode), (router.AUTO, router.NO_REPLY))

    def test_degrade_never_lets_a_ticket_through(self):
        decision = router.degraded("分類服務不可用")
        self.assertEqual(decision.route, router.HUMAN)
        self.assertIn(router.DEGRADED, decision.triggers)


class TestReply(unittest.TestCase):
    def setUp(self):
        self.masked = pii.Pseudonymizer([pii.Entity("person_name", "林佳蓉")])
        self.fake = self.masked.apply("林佳蓉")

    def test_draft_is_restored_to_the_real_name_before_it_is_written(self):
        answer = _answer()
        decision = router.decide(answer, ())
        _, draft, _ = reply.render(answer, decision, self.fake, self.masked)
        self.assertIn("林佳蓉", draft)
        self.assertNotIn(self.fake, draft)

    def test_promise_wording_downgrades_the_draft(self):
        self.assertTrue(reply.banned_hits("我們已為您辦理退款"))
        self.assertTrue(reply.banned_hits("退費 8,000 元"))
        self.assertFalse(reply.banned_hits("我們會核對訂單後與您聯繫。"))

    def test_no_template_makes_a_promise(self):
        """情境範本本身不能命中禁用詞，否則每一封都會被降級。"""
        for scenario in catalog.SCENARIOS:
            with self.subTest(scenario=scenario.name):
                self.assertEqual(reply.banned_hits(scenario.reply), ())

    def test_handoff_sentence_appears_only_when_a_human_takes_over(self):
        answer = _answer(scenarios=("cancel_refund",))
        decision = router.decide(answer, ())
        _, draft, _ = reply.render(answer, decision, self.fake, self.masked)
        self.assertIn("將由專人接手", draft)


class TestCatalog(unittest.TestCase):
    def test_every_scenario_belongs_to_a_known_group(self):
        for scenario in catalog.SCENARIOS:
            self.assertIn(scenario.group, catalog.GROUP_BY_NAME)

    def test_rejected_scenarios_have_no_assignment_and_no_reply(self):
        for scenario in catalog.SCENARIOS:
            if scenario.disposition == catalog.REJECT:
                self.assertEqual(scenario.assignment, catalog.NOT_APPLICABLE)
                self.assertEqual(scenario.reply, "")

    def test_irreversible_scenarios_require_a_human(self):
        for scenario in catalog.SCENARIOS:
            if scenario.irreversible:
                self.assertTrue(scenario.requires_human, scenario.name)

    def test_scenario_names_are_unique(self):
        self.assertEqual(len(catalog.BY_NAME), len(catalog.SCENARIOS))


class TestPipelineOutputs(unittest.TestCase):
    """跑完整管線（重跑既有回應，不呼叫 API），檢查落地的檔案。"""

    @classmethod
    def setUpClass(cls):
        cls.rows = pipeline.Pipeline(gemini.Classifier(live=False), pii.MockTransport()).run(TICKETS)

    def test_every_ticket_produces_a_row(self):
        self.assertEqual(len(self.rows), 48)

    def test_no_ticket_is_left_unclassified_and_auto(self):
        for row in self.rows:
            if not row.category:
                self.assertEqual(row.decision.route, router.HUMAN, row.ticket.ticket_id)

    def test_csv_carries_no_original_personal_data(self):
        path = Path(bootloader.POC_ROOT / "triage_result.csv")
        report.write_csv(self.rows, path)
        text = path.read_text(encoding="utf-8")
        annotations = json.loads((bootloader.DATA_DIR / "pii_annotations.json").read_text("utf-8"))
        for ticket_id, entities in annotations.items():
            if ticket_id.startswith("_"):
                continue
            for entity in entities:
                if entity["type"] in {"national_id", "phone", "bank_account", "tax_id"}:
                    with self.subTest(ticket=ticket_id, value=entity["value"]):
                        self.assertNotIn(entity["value"], text)

    def test_csv_has_every_declared_column(self):
        path = Path(bootloader.POC_ROOT / "triage_result.csv")
        report.write_csv(self.rows, path)
        with path.open(encoding="utf-8") as handle:
            self.assertEqual(tuple(next(csv.reader(handle))), report.FIELDS)


if __name__ == "__main__":
    unittest.main()
