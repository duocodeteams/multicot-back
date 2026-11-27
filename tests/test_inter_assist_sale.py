"""Venta Inter Assist: venta local y checkout de Mercado Pago / Payway."""

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
    PaymentStart,
)
from app.sales.service import (
    SaleFlowError,
    confirm_inter_assist_payment,
    open_inter_assist_sale,
    passengers_response,
    sale_to_response,
    set_inter_assist_passengers,
    start_inter_assist_payment,
)
from app.services.companies.inter_assist import InterAssistQuoteProvider
from app.services.companies.inter_assist_emission import (
    InterAssistCheckoutResult,
    InterAssistClientDraft,
    PaywayCard,
    PaywayCardsResult,
    build_checkout_payload,
    build_emitir_externa_payload,
    emit_inter_assist_sale,
    emission_type_and_periodo,
    list_payway_cards,
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


def _payment_start(**overrides) -> PaymentStart:
    data = dict(
        payment_method_id="mercadopago",
        return_url_ok="https://panel.example/ok",
        return_url_error="https://panel.example/error",
        return_url_pending="https://panel.example/pendiente",
    )
    data.update(overrides)
    return PaymentStart(**data)


def _cards() -> PaywayCardsResult:
    return PaywayCardsResult(
        cards=[PaywayCard(id=1, name="Visa"), PaywayCard(id=2, name="Mastercard")],
        max_installments=3,
    )


def _checkout(**overrides) -> InterAssistCheckoutResult:
    data = dict(
        order_uuid="8d2c1c3e-5b5a-4f1e-9c2a-1a2b3c4d5e6f",
        status="pending",
        checkout_url="https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=test",
        amount=Decimal("79000.00"),
        currency="ARS",
        exchange_rate=Decimal("1000"),
        gateway="mercadopago",
        status_code=200,
        request_body={"plan_id": 802},
        response_body={"status": "pending", "amount": 79000, "currency": "ARS", "tipo_cambio": 1000},
    )
    data.update(overrides)
    return InterAssistCheckoutResult(**data)


class InterAssistPromoSelectionTest(TestCase):
    def test_only_flat_percentage_promos(self) -> None:
        provider = InterAssistQuoteProvider()
        self.assertTrue(
            provider._is_flat_percentage_promotion(
                {"tipo_promocion": {"id": 4, "nombre": "Porcentaje"}}
            )
        )
        self.assertFalse(
            provider._is_flat_percentage_promotion(
                {"tipo_promocion": {"id": 3, "nombre": "2 x 1 (Porcentaje)"}}
            )
        )
        selected = provider._select_active_promotion(
            {
                "promociones_object": [
                    {
                        "id": 5,
                        "activo": True,
                        "programado": False,
                        "porcentaje": "60",
                        "nombre": "nuevapromo",
                        "tipo_promocion": {"id": 3, "nombre": "2 x 1 (Porcentaje)"},
                    },
                    {
                        "id": 93,
                        "activo": True,
                        "programado": False,
                        "porcentaje": "50",
                        "nombre": "50 OFF",
                        "tipo_promocion": {"id": 4, "nombre": "Porcentaje"},
                    },
                ]
            },
            date.today(),
        )
        self.assertEqual(selected, (93, Decimal("50.00"), "50 OFF"))


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

    def test_checkout_payload_splits_gateways(self) -> None:
        common = dict(
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
                    email="maria.garcia@ejemplo.com",
                )
            ],
            success_url="https://agencia.example/pago/ok",
            failure_url="https://agencia.example/pago/error",
            notification_url="https://agencia.example/webhooks/interassist",
        )
        mercadopago = build_checkout_payload(
            gateway="mercadopago",
            pending_url="https://agencia.example/pago/pendiente",
            **common,
        )
        self.assertEqual(mercadopago["pending_url"], "https://agencia.example/pago/pendiente")
        self.assertNotIn("credit_card_id", mercadopago)
        payway = build_checkout_payload(
            gateway="payway",
            pending_url="https://agencia.example/pago/pendiente",
            credit_card_id=1,
            installments=3,
            **common,
        )
        self.assertEqual(payway["pending_url"], "https://agencia.example/pago/pendiente")
        self.assertEqual(payway["credit_card_id"], 1)
        self.assertEqual(payway["installments"], 3)
        sin_pending = build_checkout_payload(
            gateway="payway",
            credit_card_id=104,
            installments=1,
            **common,
        )
        self.assertNotIn("pending_url", sin_pending)
        self.assertEqual(sin_pending["credit_card_id"], 104)
        self.assertEqual(payway["clientes"][0]["email"], "maria.garcia@ejemplo.com")

    def test_list_payway_cards(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith("/api/ventas/checkout/payway/tarjetas"))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "nombre": "Visa"},
                        {"id": 2, "nombre": "Mastercard"},
                    ],
                    "max_installments": 12,
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch(
            "app.services.companies.inter_assist_emission.get_settings"
        ) as settings:
            settings.return_value.interassist_api_key = "sk_live_test"
            settings.return_value.interassist_base_url = "https://api-staging.interassist.com"
            result = list_payway_cards(client=client)
        self.assertEqual([card.name for card in result.cards], ["Visa", "Mastercard"])
        self.assertEqual(result.max_installments, 12)


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

    def test_stores_promocion_id_exactly(self) -> None:
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            with_promo, _ = open_inter_assist_sale(
                self.session,
                self.admin,
                "inter_assist",
                PLAN_ID,
                _quote(promocion_id=93, amount=Decimal("60.00")),
            )
            without, _ = open_inter_assist_sale(
                self.session,
                self.admin,
                "inter_assist",
                "803",
                _quote(promocion_id=0, plan_name="SIN PROMO"),
            )
        self.assertEqual(with_promo.provider_context["promocion_id"], 93)
        self.assertEqual(without.provider_context["promocion_id"], 0)

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
        self.cards_patch = patch(
            "app.sales.service.list_payway_cards",
            return_value=_cards(),
        )
        self.cards_patch.start()
        with patch("app.sales.service._validate_inter_assist_quote_amount"):
            self.sale, _ = open_inter_assist_sale(
                self.session, self.admin, "inter_assist", PLAN_ID, _quote()
            )
        self.passengers = set_inter_assist_passengers(
            self.session,
            self.admin,
            self.sale.id or 0,
            PassengersCreate.model_validate(_passengers_payload()),
        )
        self.session.refresh(self.sale)

    def tearDown(self) -> None:
        self.cards_patch.stop()
        self.session.close()

    def test_passengers_offer_both_gateways(self) -> None:
        methods = {method.id: method for method in self.passengers.payment_methods}
        self.assertEqual(set(methods), {"mercadopago", "payway"})
        self.assertFalse(methods["mercadopago"].requires_financing)
        payway = methods["payway"]
        self.assertTrue(payway.requires_financing)
        self.assertEqual([card.name for card in payway.cards], ["Visa", "Mastercard"])
        self.assertEqual(payway.max_installments, 3)
        self.assertEqual([option.dues for option in payway.financing], [1, 2, 3])
        again = passengers_response(self.session, self.sale)
        self.assertEqual(
            [method.id for method in again.payment_methods],
            ["mercadopago", "payway"],
        )

    def test_mercadopago_checkout_and_approved_confirm(self) -> None:
        with patch(
            "app.sales.service.create_inter_assist_checkout",
            return_value=_checkout(),
        ) as create:
            started = start_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0, _payment_start()
            )
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["gateway"], "mercadopago")
        self.assertEqual(
            create.call_args.kwargs["pending_url"],
            "https://panel.example/pendiente",
        )
        self.assertTrue(started.created)
        self.assertIn("mercadopago.com", started.checkout_url or "")
        self.assertEqual(started.amount, Decimal("79000.00"))
        self.assertEqual(started.currency, "ARS")
        self.assertEqual(started.exchange_rate, Decimal("1000"))
        payment = self.session.exec(select(Payment)).one()
        self.assertEqual(payment.provider, PaymentProvider.MERCADOPAGO.value)
        self.assertEqual(payment.external_id, "8d2c1c3e-5b5a-4f1e-9c2a-1a2b3c4d5e6f")
        self.assertEqual(payment.amount, Decimal("79000.00"))
        self.assertEqual(payment.currency, "ARS")
        self.assertEqual(payment.provider_data.get("exchange_rate"), "1000")

        approved = _checkout(
            status="approved",
            gateway="mercadopago",
            venta_id=88501,
            voucher_ids=[90002],
            response_body={
                "status": "approved",
                "venta": {"id": 88501, "voucher_ids": [90002]},
            },
        )
        with patch(
            "app.sales.service.get_inter_assist_checkout",
            return_value=approved,
        ), patch("app.sales.service.emit_inter_assist_sale") as emit:
            confirm = confirm_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0
            )
        emit.assert_not_called()
        self.assertTrue(confirm.payment_received)
        self.assertEqual(confirm.voucher_number, "90002")
        self.session.refresh(self.sale)
        self.assertEqual(self.sale.emission_status, EmissionStatus.ISSUED.value)
        self.assertEqual(self.sale.payment_status, SalePaymentStatus.APPROVED.value)
        voucher = self.session.exec(select(Voucher)).one()
        self.assertEqual(voucher.external_id, "90002")

    def test_confirm_while_pending_does_not_emit(self) -> None:
        with patch(
            "app.sales.service.create_inter_assist_checkout",
            return_value=_checkout(),
        ):
            start_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0, _payment_start()
            )
        with patch(
            "app.sales.service.get_inter_assist_checkout",
            return_value=_checkout(status="pending", venta_id=None, voucher_ids=[]),
        ), patch("app.sales.service.emit_inter_assist_sale") as emit:
            confirm = confirm_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0
            )
        emit.assert_not_called()
        self.assertFalse(confirm.payment_received)
        self.session.refresh(self.sale)
        self.assertEqual(self.sale.payment_status, SalePaymentStatus.PENDING.value)

    def test_rejected_blocks_confirm(self) -> None:
        with patch(
            "app.sales.service.create_inter_assist_checkout",
            return_value=_checkout(),
        ):
            start_inter_assist_payment(
                self.session, self.admin, self.sale.id or 0, _payment_start()
            )
        with patch(
            "app.sales.service.get_inter_assist_checkout",
            return_value=_checkout(status="rejected"),
        ):
            with self.assertRaises(SaleFlowError) as raised:
                confirm_inter_assist_payment(
                    self.session, self.admin, self.sale.id or 0
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.session.refresh(self.sale)
        self.assertEqual(self.sale.payment_status, SalePaymentStatus.REJECTED.value)

    def test_payway_requires_card_and_reuses_same_link(self) -> None:
        with self.assertRaises(SaleFlowError) as raised:
            start_inter_assist_payment(
                self.session,
                self.admin,
                self.sale.id or 0,
                _payment_start(payment_method_id="payway", financing_id="3"),
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_payway_rejects_localhost_return_url(self) -> None:
        with self.assertRaises(SaleFlowError) as raised:
            start_inter_assist_payment(
                self.session,
                self.admin,
                self.sale.id or 0,
                _payment_start(
                    payment_method_id="payway",
                    credit_card_id="1",
                    financing_id="3",
                    return_url_ok="http://localhost:3000/portal/pago?sale_id=1&payment=ok",
                    return_url_error="http://localhost:3000/portal/pago?sale_id=1&payment=error",
                    return_url_pending="http://localhost:3000/portal/pago?sale_id=1&payment=pending",
                ),
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("localhost", raised.exception.detail)

    def test_payway_reuses_same_link(self) -> None:
        payway_link = _checkout(
            gateway="payway",
            checkout_url="https://payway.example/checkout",
        )
        with patch(
            "app.sales.service.create_inter_assist_checkout",
            return_value=payway_link,
        ) as create:
            first = start_inter_assist_payment(
                self.session,
                self.admin,
                self.sale.id or 0,
                _payment_start(
                    payment_method_id="payway",
                    credit_card_id="1",
                    financing_id="3",
                ),
            )
            second = start_inter_assist_payment(
                self.session,
                self.admin,
                self.sale.id or 0,
                _payment_start(
                    payment_method_id="payway",
                    credit_card_id="1",
                    financing_id="3",
                ),
            )
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["credit_card_id"], 1)
        self.assertEqual(create.call_args.kwargs["installments"], 3)
        self.assertEqual(
            create.call_args.kwargs["pending_url"],
            "https://panel.example/pendiente",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.checkout_url, "https://payway.example/checkout")
        payments = self.session.exec(select(Payment)).all()
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].provider, PaymentProvider.PAYWAY.value)
        self.assertEqual(payments[0].status, PaymentStatus.PENDING.value)
