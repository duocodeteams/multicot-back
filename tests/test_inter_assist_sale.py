"""Venta Inter Assist: venta local, pago simulado y emisión mockeada."""

from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.models  # noqa: F401
from app.models.company import Company
from app.models.sale import (
    EmissionAttempt,
    EmissionStatus,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Sale,
    SalePaymentStatus,
    Voucher,
)
from app.models.user import User, UserRole
from app.sales.schemas import (
    InterAssistQuoteSnapshot,
    PassengersCreate,
    PaymentSimulate,
    PaymentStart,
)
from app.sales.service import (
    SaleFlowError,
    confirm_inter_assist_payment,
    open_inter_assist_sale,
    sale_to_response,
    set_inter_assist_passengers,
    simulate_inter_assist_payment,
    start_inter_assist_payment,
)
from app.services.companies.inter_assist_emission import (
    InterAssistClientDraft,
    InterAssistEmissionError,
    build_emitir_externa_payload,
    emit_inter_assist_sale,
    emission_type_and_periodo,
)


PLAN_ID = "802"


def _quote(**overrides) -> InterAssistQuoteSnapshot:
    data = dict(
        departure_date=date(2026, 6, 20),
        return_date=date(2026, 6, 27),
        ages=[35],
        origin="AR",
        destination_id=3,
        trip_type="unico_viaje",
        days_range=None,
        amount=Decimal("120.50"),
        currency="USD",
        plan_name="INTER 100",
        promocion_id=0,
        markup=Decimal("0"),
    )
    data.update(overrides)
    return InterAssistQuoteSnapshot(**data)


def _passengers_payload() -> dict:
    return {
        "receive_promotions": False,
        "emergency_contact": {
            "first_name": "Luis",
            "last_name": "Perez",
            "phone": "+5491100000001",
            "email": "luis.perez@example.com",
        },
        "passengers": [
            {
                "is_holder": True,
                "first_name": "Maria",
                "last_name": "Garcia",
                "birth_date": "1991-03-15",
                "document_type": "dni",
                "document_number": "30123456",
                "email": "maria.garcia@example.com",
                "phone": "1144556677",
                "country_id": "AR",
                "province_id": "BA",
                "country": "Argentina",
                "province": "Buenos Aires",
                "street": "Av. Ejemplo",
                "street_number": "123",
                "city": "Buenos Aires",
            }
        ],
    }


def _payment_start() -> PaymentStart:
    return PaymentStart(
        payment_method_id="simulated",
        return_url_ok="https://panel.example/ok",
        return_url_error="https://panel.example/error",
        return_url_pending="https://panel.example/pendiente",
    )


class EmitirExternaPayloadTest(TestCase):
    def test_daily_payload(self) -> None:
        body = build_emitir_externa_payload(
            plan_id=802,
            trip_type="unico_viaje",
            days_range=None,
            departure_date=date(2026, 6, 20),
            return_date=date(2026, 6, 27),
            clientes=[
                InterAssistClientDraft(
                    first_name="Maria",
                    last_name="Garcia",
                    birth_date=date(1990, 3, 15),
                    document_number="30123456",
                    email="maria.garcia@example.com",
                )
            ],
            promocion_id=305,
        )
        self.assertEqual(body["type"], 0)
        self.assertEqual(body["data_inicial"], "20/06/2026")
        self.assertEqual(body["data_final"], "27/06/2026")
        self.assertEqual(body["promocion_id"], 305)
        self.assertNotIn("periodo", body)

    def test_annual_requires_periodo(self) -> None:
        emission_type, periodo = emission_type_and_periodo("multiviaje", 60)
        self.assertEqual(emission_type, 1)
        self.assertEqual(periodo, 2)

    def test_emit_parses_voucher_ids(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith("/api/ventas/emitir-externa"))
            return httpx.Response(
                201,
                json={
                    "message": "Venta Creada con Éxito",
                    "venta": {"id": 88500, "voucher_ids": [90001]},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch(
            "app.services.companies.inter_assist_emission.get_settings"
        ) as settings:
            settings.return_value.interassist_api_key = "sk_live_test"
            settings.return_value.interassist_base_url = "https://api-staging.interassist.com"
            result = emit_inter_assist_sale(
                plan_id=802,
                trip_type="unico_viaje",
                days_range=None,
                departure_date=date(2026, 6, 20),
                return_date=date(2026, 6, 27),
                clientes=[
                    InterAssistClientDraft(
                        first_name="Maria",
                        last_name="Garcia",
                        birth_date=date(1990, 3, 15),
                        document_number="30123456",
                    )
                ],
                client=client,
            )
        self.assertEqual(result.venta_id, 88500)
        self.assertEqual(result.voucher_ids, [90001])


class OpenInterAssistSaleTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.company = Company(slug="inter_assist", name="Inter Assist", active=True)
        self.session.add(self.company)
        self.session.commit()
        self.session.refresh(self.company)
        self.admin = self._user("admin@test.com", UserRole.ADMIN)

    def tearDown(self) -> None:
        self.session.close()

    def test_opens_sale_without_calling_interassist(self) -> None:
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            sale, created = open_inter_assist_sale(
                self.session,
                self.admin,
                "inter_assist",
                PLAN_ID,
                _quote(),
            )
        self.assertTrue(created)
        self.assertEqual(sale.emission_status, EmissionStatus.PRODUCT_SELECTED.value)
        self.assertEqual(sale.payment_status, SalePaymentStatus.NOT_STARTED.value)
        self.assertEqual(sale.external_plan_id, PLAN_ID)
        self.assertEqual(sale.amount, Decimal("120.50"))
        self.assertTrue(sale.provider_reference.startswith("inter_assist:802:"))
        response = sale_to_response(sale, "inter_assist")
        self.assertEqual(response.company, "inter_assist")
        self.assertNotIn("provider_context", response.model_dump())

    def test_same_quote_returns_existing(self) -> None:
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            first, _ = open_inter_assist_sale(
                self.session, self.admin, "inter_assist", PLAN_ID, _quote()
            )
            second, created = open_inter_assist_sale(
                self.session, self.admin, "inter_assist", PLAN_ID, _quote()
            )
        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.session.exec(select(Sale)).all()), 1)
        attempt = self.session.exec(select(EmissionAttempt)).one()
        self.assertEqual(attempt.sale_id, first.id)
        self.assertNotEqual(attempt.sale_id, 0)

    def test_advanced_sale_returns_existing_for_resume(self) -> None:
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            first, _ = open_inter_assist_sale(
                self.session, self.admin, "inter_assist", PLAN_ID, _quote()
            )
        first.emission_status = EmissionStatus.PASSENGERS_COMPLETE.value
        self.session.add(first)
        self.session.commit()
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            second, created = open_inter_assist_sale(
                self.session, self.admin, "inter_assist", PLAN_ID, _quote()
            )
        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            second.emission_status, EmissionStatus.PASSENGERS_COMPLETE.value
        )

    def test_rejects_other_company(self) -> None:
        with self.assertRaises(SaleFlowError) as raised:
            open_inter_assist_sale(
                self.session, self.admin, "omint", PLAN_ID, _quote()
            )
        self.assertEqual(raised.exception.status_code, 400)

    def _user(self, email: str, role: UserRole) -> User:
        user = User(email=email, password_hash="x", role=role, active=True)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user


class InterAssistPaymentFlowTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.company = Company(slug="inter_assist", name="Inter Assist", active=True)
        self.session.add(self.company)
        self.session.commit()
        self.session.refresh(self.company)
        self.admin = User(
            email="admin@test.com", password_hash="x", role=UserRole.ADMIN, active=True
        )
        self.session.add(self.admin)
        self.session.commit()
        self.session.refresh(self.admin)
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            self.sale, _ = open_inter_assist_sale(
                self.session, self.admin, "inter_assist", PLAN_ID, _quote()
            )
        set_inter_assist_passengers(
            self.session,
            self.admin,
            self.sale.id or 0,
            PassengersCreate.model_validate(_passengers_payload()),
        )
        self.session.refresh(self.sale)

    def tearDown(self) -> None:
        self.session.close()

    def test_happy_path_emits_after_approved(self) -> None:
        start_inter_assist_payment(
            self.session, self.admin, self.sale.id or 0, _payment_start()
        )
        simulate_inter_assist_payment(
            self.session,
            self.admin,
            self.sale.id or 0,
            PaymentSimulate(result="approved"),
        )
        with patch(
            "app.sales.service.emit_inter_assist_sale",
            return_value=_emission_result(),
        ) as emit:
            confirm = confirm_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0
            )
        emit.assert_called_once()
        self.assertTrue(confirm.payment_received)
        self.assertEqual(confirm.voucher_number, "90001")
        self.session.refresh(self.sale)
        self.assertEqual(self.sale.emission_status, EmissionStatus.ISSUED.value)
        self.assertEqual(self.sale.payment_status, SalePaymentStatus.APPROVED.value)
        voucher = self.session.exec(select(Voucher)).one()
        self.assertEqual(voucher.external_id, "90001")
        payment = self.session.exec(select(Payment)).one()
        self.assertEqual(payment.provider, PaymentProvider.SIMULATED.value)
        self.assertEqual(payment.status, PaymentStatus.APPROVED.value)

    def test_confirm_while_pending_does_not_emit(self) -> None:
        start_inter_assist_payment(
            self.session, self.admin, self.sale.id or 0, _payment_start()
        )
        with patch("app.sales.service.emit_inter_assist_sale") as emit:
            confirm = confirm_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0
            )
        emit.assert_not_called()
        self.assertFalse(confirm.payment_received)
        self.session.refresh(self.sale)
        self.assertEqual(self.sale.payment_status, SalePaymentStatus.PENDING.value)

    def test_rejected_blocks_confirm(self) -> None:
        start_inter_assist_payment(
            self.session, self.admin, self.sale.id or 0, _payment_start()
        )
        simulate_inter_assist_payment(
            self.session,
            self.admin,
            self.sale.id or 0,
            PaymentSimulate(result="rejected"),
        )
        with self.assertRaises(SaleFlowError) as raised:
            confirm_inter_assist_payment(self.session, self.admin, self.sale.id or 0)
        self.assertEqual(raised.exception.status_code, 409)

    def test_emission_failure_marks_sale(self) -> None:
        start_inter_assist_payment(
            self.session, self.admin, self.sale.id or 0, _payment_start()
        )
        simulate_inter_assist_payment(
            self.session,
            self.admin,
            self.sale.id or 0,
            PaymentSimulate(result="approved"),
        )
        error = InterAssistEmissionError(502, "falló emisión")
        error.http_status = 500
        error.request_body = {"plan_id": 802}
        error.response_body = {"message": "boom"}
        with patch("app.sales.service.emit_inter_assist_sale", side_effect=error):
            with self.assertRaises(SaleFlowError) as raised:
                confirm_inter_assist_payment(
                    self.session, self.admin, self.sale.id or 0
                )
        self.assertEqual(raised.exception.status_code, 502)
        self.session.refresh(self.sale)
        self.assertEqual(self.sale.emission_status, EmissionStatus.EMISSION_FAILED.value)
        attempt = self.session.exec(
            select(EmissionAttempt).where(EmissionAttempt.step == "emit_sale")
        ).one()
        self.assertFalse(attempt.success)


def _emission_result():
    from app.services.companies.inter_assist_emission import InterAssistEmissionResult

    return InterAssistEmissionResult(
        venta_id=88500,
        voucher_ids=[90001],
        status_code=201,
        request_body={"plan_id": 802},
        response_body={"venta": {"id": 88500, "voucher_ids": [90001]}},
    )
