"""Venta Omint: selección de producto y persistencia, sin llamar a la API real."""

from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.models  # noqa: F401
from app.models.agency import Agency, BillingFrequency, PaymentMethod, TaxCondition
from app.models.company import Company
from app.models.plan import Plan
from app.models.sale import EmissionAttempt, EmissionStatus, Passenger, Payment, Sale, Voucher
from app.models.seller import Seller
from app.models.user import User, UserRole
from app.sales.schemas import PassengersCreate, PaymentStart
from app.sales.service import (
    SaleFlowError,
    confirm_omint_payment,
    confirm_omint_payment_from_notification,
    get_omint_sale,
    open_omint_sale,
    sale_to_response,
    set_omint_passengers,
    start_omint_payment,
)
from app.services.companies.omint_emission import (
    OmintAssignFailed,
    OmintCheckout,
    OmintConfirmation,
    OmintEmergencyDraft,
    OmintPassengerDraft,
    OmintPassengersResult,
    OmintSelection,
    assign_omint_passengers,
    list_omint_countries,
    select_omint_product,
)

QUOTATION_ID = "11111111-1111-1111-1111-111111111111"
PRODUCT_ID = "22222222-2222-2222-2222-222222222222"


def _quotation() -> dict:
    return {
        "id": QUOTATION_ID,
        "number": "000958143",
        "dateSince": "2026-10-10T00:00:00",
        "dateUntil": "2026-10-20T00:00:00",
        "departureCode": "MAA",
        "destinationCode": "EMO",
        "productTypeCode": "S",
        "ages": [35, 38],
        "products": [
            {
                "id": PRODUCT_ID,
                "productCode": "PA-S-d030-Cov",
                "denomination": "OA 30",
                "finalPriceAmount": 9075.00,
                "basePriceAmount": 15125.00,
                "currencyCode": "ARS",
                "currencyRate": 1210.00,
                "originalFinalPriceAmount": 7.50,
                "originalCurrencyCode": "USD",
            }
        ],
    }


def _selection(**overrides) -> OmintSelection:
    data = dict(
        quotation_id=QUOTATION_ID,
        quotation_number="000958143",
        quotation_product_id=PRODUCT_ID,
        product_code="PA-S-d030-Cov",
        plan_name="OA 30",
        currency="ARS",
        amount=Decimal("9075.00"),
        base_amount=Decimal("15125.00"),
        departure_date=date(2026, 10, 10),
        return_date=date(2026, 10, 20),
        ages=[35, 38],
        origin="MAA",
        destination_id=3,
        trip_type="unico_viaje",
        days_range=None,
        provider_context={
            "quotation_id": QUOTATION_ID,
            "quotation_product_id": PRODUCT_ID,
            "product_code": "PA-S-d030-Cov",
            "amount": "9075.00",
        },
        assign_request={
            "quotationId": QUOTATION_ID,
            "quotationProductId": PRODUCT_ID,
        },
        assign_response={"quotationId": QUOTATION_ID, "selectedProduct": {"finalPrice": 9075}},
        assign_status_code=200,
    )
    data.update(overrides)
    return OmintSelection(**data)


class SelectOmintProductTest(TestCase):
    def test_assigns_product_and_keeps_omint_price(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(f"/Quotation/{QUOTATION_ID}"):
                return httpx.Response(200, json=_quotation())
            if request.url.path.endswith("/Quotation/AssignProduct"):
                body = request.read()
                self.assertIn(PRODUCT_ID.encode(), body)
                return httpx.Response(
                    200,
                    json={
                        "quotationId": QUOTATION_ID,
                        "selectedProduct": {
                            "id": PRODUCT_ID,
                            "productCode": "PA-S-d030-Cov",
                            "denomination": "OA 30",
                            "finalPrice": 9075.00,
                            "basePrice": 15125.00,
                        },
                    },
                )
            return httpx.Response(404, json={"errors": ["missing"]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch(
            "app.services.companies.omint_emission._token_manager.get_token",
            return_value="token",
        ):
            selection = select_omint_product(QUOTATION_ID, PRODUCT_ID, client=client)

        self.assertEqual(selection.amount, Decimal("9075"))
        self.assertEqual(selection.currency, "ARS")
        self.assertEqual(selection.destination_id, 3)
        self.assertEqual(selection.trip_type, "unico_viaje")
        self.assertEqual(selection.departure_date, date(2026, 10, 10))
        self.assertEqual(Decimal(selection.provider_context["currency_rate"]), Decimal("1210"))
        self.assertNotIn("token", str(selection.provider_context))

    def test_rejects_product_from_another_quotation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_quotation())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch(
            "app.services.companies.omint_emission._token_manager.get_token",
            return_value="token",
        ):
            with self.assertRaises(Exception) as raised:
                select_omint_product(QUOTATION_ID, "otro-producto", client=client)
        self.assertIn("no pertenece", str(raised.exception))


class OpenOmintSaleTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.company = Company(slug="omint", name="Omint", active=True)
        self.session.add(self.company)
        self.session.commit()
        self.session.refresh(self.company)
        self.admin = self._user("admin@test.com", UserRole.ADMIN)

    def tearDown(self) -> None:
        self.session.close()

    def test_admin_sale_has_no_agency_and_markup_zero(self) -> None:
        with patch("app.sales.service.select_omint_product", return_value=_selection()):
            sale, created = open_omint_sale(
                self.session,
                self.admin,
                "Omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )

        self.assertTrue(created)
        self.assertEqual(sale.emission_status, EmissionStatus.PRODUCT_SELECTED.value)
        self.assertEqual(sale.payment_status, "not_started")
        self.assertIsNone(sale.agency_id)
        self.assertIsNone(sale.seller_id)
        self.assertEqual(sale.created_by_user_id, self.admin.id)
        self.assertEqual(sale.amount, Decimal("9075.00"))
        self.assertEqual(sale.markup, Decimal("0"))
        self.assertEqual(sale.quoted_ages, [35, 38])
        attempt = self.session.exec(select(EmissionAttempt)).one()
        self.assertTrue(attempt.success)
        self.assertEqual(attempt.step, "assign_product")
        response = sale_to_response(sale, "omint")
        dumped = response.model_dump()
        self.assertNotIn("provider_context", dumped)
        self.assertNotIn(QUOTATION_ID, dumped.values())

    def test_same_quote_does_not_call_omint_again(self) -> None:
        with patch("app.sales.service.select_omint_product", return_value=_selection()) as mocked:
            open_omint_sale(
                self.session,
                self.admin,
                "omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )
            sale, created = open_omint_sale(
                self.session,
                self.admin,
                "omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )
        self.assertFalse(created)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(len(self.session.exec(select(Sale)).all()), 1)
        self.assertEqual(sale.id, self.session.exec(select(Sale)).one().id)

    def test_seller_keeps_agency(self) -> None:
        agency = Agency(
            name="Biant",
            legal_name="Biant SA",
            tax_id="30111222333",
            address="Calle 1",
            country="AR",
            legal_representative_name="Ana",
            agency_email="agencia@test.com",
            office_phone="111",
            activation_date=date(2026, 1, 1),
            billing_frequency=BillingFrequency.MONTHLY,
            payment_method=PaymentMethod.TRANSFER,
            tax_condition=TaxCondition.RESPONSABLE_INSCRIPTO,
        )
        self.session.add(agency)
        self.session.commit()
        self.session.refresh(agency)
        seller_user = self._user("vendedor@test.com", UserRole.SELLER)
        seller = Seller(
            user_id=seller_user.id,
            agency_id=agency.id,
            first_name="Juan",
            last_name="Perez",
            address="Calle 2",
            nationality="AR",
            birth_date=date(1990, 1, 1),
            comments="",
        )
        self.session.add(seller)
        self.session.commit()
        self.session.refresh(seller)

        with patch("app.sales.service.select_omint_product", return_value=_selection()):
            sale, _created = open_omint_sale(
                self.session,
                seller_user,
                "omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )
        self.assertEqual(sale.seller_id, seller.id)
        self.assertEqual(sale.agency_id, agency.id)

    def test_links_local_plan(self) -> None:
        plan = Plan(
            company_id=self.company.id,
            external_plan_id="PA-S-d030-Cov",
            name="OA 30",
        )
        self.session.add(plan)
        self.session.commit()
        self.session.refresh(plan)
        with patch("app.sales.service.select_omint_product", return_value=_selection()):
            sale, _created = open_omint_sale(
                self.session,
                self.admin,
                "omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )
        self.assertEqual(sale.plan_id, plan.id)

    def test_other_company_is_rejected(self) -> None:
        with self.assertRaises(SaleFlowError) as raised:
            open_omint_sale(self.session, self.admin, "cardinal", f"{QUOTATION_ID}|{PRODUCT_ID}")
        self.assertEqual(raised.exception.status_code, 400)

    def test_assign_failure_keeps_the_sale(self) -> None:
        selection = _selection()
        error = OmintAssignFailed("producto vencido", selection, 400, {"errors": ["producto vencido"]})
        with patch("app.sales.service.select_omint_product", side_effect=error):
            with self.assertRaises(SaleFlowError) as raised:
                open_omint_sale(
                    self.session,
                    self.admin,
                    "omint",
                    f"{QUOTATION_ID}|{PRODUCT_ID}",
                )
        self.assertEqual(raised.exception.status_code, 502)
        sale = self.session.exec(select(Sale)).one()
        self.assertEqual(sale.emission_status, EmissionStatus.EMISSION_FAILED.value)
        attempt = self.session.exec(select(EmissionAttempt)).one()
        self.assertFalse(attempt.success)

    def _user(self, email: str, role: UserRole) -> User:
        user = User(email=email, password_hash="x", role=role, active=True)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user


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
                "first_name": "Ana",
                "last_name": "Perez",
                "birth_date": "1991-04-22",
                "document_type": "dni",
                "document_number": "30111222",
                "email": "ana.perez@example.com",
                "phone": "+5491100000000",
                "country_id": "88795069-2EA0-42DF-B76D-C28B3869E831",
                "province_id": "33333333-3333-3333-3333-333333333333",
                "country": "Argentina",
                "province": "Buenos Aires",
                "street": "Av. Ejemplo",
                "street_number": "123",
                "city": "Buenos Aires",
            },
            {
                "is_holder": False,
                "first_name": "Luis",
                "last_name": "Perez",
                "birth_date": "1988-08-09",
                "document_type": "passport",
                "document_number": "28999888",
            },
        ],
    }


class AssignPassengersTest(TestCase):
    def test_request_uses_omint_field_names_and_document_guid(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read().decode()
            return httpx.Response(
                200,
                json={
                    "quotationId": QUOTATION_ID,
                    "paymentMethods": [{"id": "44444444-4444-4444-4444-444444444444", "code": "GNET"}],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        drafts = [
            OmintPassengerDraft(
                first_name="Ana",
                last_name="Perez",
                birth_date=date(1991, 4, 22),
                document_type="dni",
                document_number="30111222",
                is_holder=True,
                email="ana.perez@example.com",
                phone="+5491100000000",
                country_id="88795069-2EA0-42DF-B76D-C28B3869E831",
                province_id="33333333-3333-3333-3333-333333333333",
                street="Av. Ejemplo",
                street_number="123",
                city="Buenos Aires",
            )
        ]
        with patch(
            "app.services.companies.omint_emission._token_manager.get_token",
            return_value="token",
        ):
            result = assign_omint_passengers(
                QUOTATION_ID,
                drafts,
                OmintEmergencyDraft("Luis", "Perez", "+5491100000001"),
                client=client,
            )
        self.assertIn('"passangers"', captured["body"])
        self.assertIn('"recievePromotionAndNews"', captured["body"])
        self.assertIn("5C9D2946-99DA-4235-80CD-8375B852390F", captured["body"])
        self.assertEqual(result.payment_methods[0]["code"], "GNET")

    def test_countries_reads_the_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/Country", request.url.path)
            return httpx.Response(
                200,
                json=[{"id": "88795069-2EA0-42DF-B76D-C28B3869E831", "name": "Argentina"}],
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch(
            "app.services.companies.omint_emission._token_manager.get_token",
            return_value="token",
        ):
            items = list_omint_countries(client=client)
        self.assertEqual(items[0]["name"], "Argentina")


class SetPassengersTest(TestCase):
    def setUp(self) -> None:
        self._user = lambda email, role: OpenOmintSaleTest._user(self, email, role)
        OpenOmintSaleTest.setUp(self)

    def tearDown(self) -> None:
        OpenOmintSaleTest.tearDown(self)

    def test_saves_holder_and_marks_passengers_complete(self) -> None:
        sale = self._open_sale()
        payload = PassengersCreate.model_validate(_passengers_payload())
        methods = [
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "code": "gnet",
                "financing": [{"id": "55555555-5555-5555-5555-555555555555", "dues": "1"}],
                "extra": "no sale",
            },
            {"code": "CASH"},
        ]
        result_payload = OmintPassengersResult(
            request_body={"quotationId": QUOTATION_ID},
            response_body={"paymentMethods": methods},
            status_code=200,
            payment_methods=methods,
        )
        with patch("app.sales.service.assign_omint_passengers", return_value=result_payload):
            response = set_omint_passengers(self.session, self.admin, sale.id, payload)

        self.assertEqual(response.emission_status, EmissionStatus.PASSENGERS_COMPLETE.value)
        self.assertEqual(len(response.passengers), 2)
        holder = next(passenger for passenger in response.passengers if passenger.is_holder)
        self.assertEqual(holder.document_type, "dni")
        self.assertEqual(holder.street, "Av. Ejemplo")
        self.assertEqual(response.emergency_contact.first_name, "Luis")
        stored = self.session.exec(select(Passenger).where(Passenger.is_holder == True)).one()  # noqa: E712
        self.assertEqual(
            stored.provider_data["document_type_id"],
            "5C9D2946-99DA-4235-80CD-8375B852390F",
        )
        self.session.refresh(sale)
        self.assertEqual(sale.provider_context["payment_methods"][0]["code"], "gnet")
        dumped = response.model_dump()
        self.assertNotIn("quotation_id", dumped)
        self.assertEqual(len(response.payment_methods), 1)
        method = response.payment_methods[0]
        self.assertEqual(method.code, "GNET")
        self.assertTrue(method.requires_financing)
        self.assertEqual(method.financing[0].dues, 1)
        self.assertNotIn("extra", method.model_dump())

    def test_rejects_ages_that_do_not_match_the_quote(self) -> None:
        sale = self._open_sale()
        raw = _passengers_payload()
        raw["passengers"][0]["birth_date"] = "1980-01-01"
        payload = PassengersCreate.model_validate(raw)
        with patch("app.sales.service.assign_omint_passengers") as mocked:
            with self.assertRaises(SaleFlowError) as raised:
                set_omint_passengers(self.session, self.admin, sale.id, payload)
        self.assertEqual(raised.exception.status_code, 400)
        mocked.assert_not_called()
        self.assertEqual(self.session.exec(select(Passenger)).all(), [])

    def _open_sale(self) -> Sale:
        with patch("app.sales.service.select_omint_product", return_value=_selection()):
            sale, _created = open_omint_sale(
                self.session,
                self.admin,
                "omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )
        return sale


def _payment_form(**overrides) -> PaymentStart:
    data = dict(
        payment_method_id="44444444-4444-4444-4444-444444444444",
        financing_id="55555555-5555-5555-5555-555555555555",
        return_url_ok="https://panel.test/pago/ok",
        return_url_error="https://panel.test/pago/error",
        return_url_pending="https://panel.test/pago/pendiente",
    )
    data.update(overrides)
    return PaymentStart.model_validate(data)


class StartPaymentTest(TestCase):
    def setUp(self) -> None:
        self._user = lambda email, role: OpenOmintSaleTest._user(self, email, role)
        OpenOmintSaleTest.setUp(self)

    def tearDown(self) -> None:
        OpenOmintSaleTest.tearDown(self)

    def test_opens_checkout_once(self) -> None:
        sale = self._ready_sale()
        checkout = OmintCheckout(
            request_body={},
            response_body={"paymentUrl": "https://gateway.test/checkout"},
            status_code=200,
            payment_url="https://gateway.test/checkout",
        )
        with patch("app.sales.service.get_settings") as settings, patch(
            "app.sales.service.start_omint_checkout",
            return_value=checkout,
        ) as mocked:
            settings.return_value.public_api_base_url = "https://api.test"
            first = start_omint_payment(self.session, self.admin, sale.id, _payment_form())
            second = start_omint_payment(self.session, self.admin, sale.id, _payment_form())
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.checkout_url, "https://gateway.test/checkout")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.session.exec(select(Payment)).one().status, "pending")
        self.session.refresh(sale)
        self.assertEqual(sale.payment_status, "pending")

    def test_card_method_requires_financing(self) -> None:
        sale = self._ready_sale()
        with patch("app.sales.service.get_settings") as settings:
            settings.return_value.public_api_base_url = "https://api.test"
            with self.assertRaises(SaleFlowError) as raised:
                start_omint_payment(
                    self.session,
                    self.admin,
                    sale.id,
                    _payment_form(financing_id=None),
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_unpaid_confirmation_does_not_open_another_payment(self) -> None:
        sale = self._ready_sale()
        self._mark_pending(sale)
        unpaid = OmintConfirmation(paid=False, http_status=400, response_body={"detail": "El pago no ha sido realizado"})
        with patch("app.sales.service.confirm_omint_checkout", return_value=unpaid) as mocked:
            result = confirm_omint_payment(self.session, self.admin, sale.id)
        self.assertFalse(result.payment_received)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.session.exec(select(Voucher)).all(), [])
        self.session.refresh(sale)
        self.assertEqual(sale.payment_status, "pending")
        self.assertEqual(sale.emission_status, EmissionStatus.PASSENGERS_COMPLETE.value)

    def test_paid_confirmation_stores_one_voucher(self) -> None:
        sale = self._ready_sale()
        self._mark_pending(sale)
        paid = OmintConfirmation(
            paid=True,
            http_status=200,
            response_body={},
            voucher_id="66666666-6666-6666-6666-666666666666",
            voucher_number="12345678",
            voucher_url="https://core.omintassistance.com.ar/Quotation/Voucher/x",
            voucher_exists=False,
        )
        with patch("app.sales.service.confirm_omint_checkout", return_value=paid) as mocked:
            first = confirm_omint_payment(self.session, self.admin, sale.id)
            second = confirm_omint_payment(self.session, self.admin, sale.id)
        self.assertTrue(first.payment_received)
        self.assertEqual(first.voucher_number, "12345678")
        self.assertEqual(second.voucher_number, "12345678")
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(len(self.session.exec(select(Voucher)).all()), 1)
        self.session.refresh(sale)
        self.assertEqual(sale.emission_status, EmissionStatus.ISSUED.value)

    def test_get_sale_exposes_checkout_and_then_the_voucher(self) -> None:
        sale = self._ready_sale()
        detail = get_omint_sale(self.session, self.admin, sale.id)
        self.assertEqual(detail.emission_status, EmissionStatus.PASSENGERS_COMPLETE.value)
        self.assertEqual(detail.payment_methods[0].code, "GNET")
        self.assertIsNone(detail.checkout_url)
        self.assertIsNone(detail.voucher_number)
        self.assertNotIn("quotation_id", detail.model_dump())

        self._mark_pending(sale)
        pending = get_omint_sale(self.session, self.admin, sale.id)
        self.assertEqual(pending.checkout_url, "https://gateway.test/checkout")
        self.assertEqual(pending.payment_status, "pending")

        paid = OmintConfirmation(
            paid=True,
            http_status=200,
            response_body={},
            voucher_id="66666666-6666-6666-6666-666666666666",
            voucher_number="12345678",
            voucher_url="https://core.omintassistance.com.ar/Quotation/Voucher/x",
            voucher_exists=False,
        )
        with patch("app.sales.service.confirm_omint_checkout", return_value=paid):
            confirm_omint_payment(self.session, self.admin, sale.id)
        issued = get_omint_sale(self.session, self.admin, sale.id)
        self.assertEqual(issued.emission_status, EmissionStatus.ISSUED.value)
        self.assertEqual(issued.voucher_number, "12345678")
        self.assertEqual(issued.voucher_url, "https://core.omintassistance.com.ar/Quotation/Voucher/x")
        self.assertIsNone(issued.checkout_url)

    def test_another_user_cannot_read_the_sale(self) -> None:
        sale = self._ready_sale()
        other = self._user("otro@test.com", UserRole.ADMIN)
        with self.assertRaises(SaleFlowError) as raised:
            get_omint_sale(self.session, other, sale.id)
        self.assertEqual(raised.exception.status_code, 403)

    def test_notification_before_checkout_does_not_call_omint(self) -> None:
        sale = self._ready_sale()
        with patch("app.sales.service.confirm_omint_checkout") as mocked:
            result = confirm_omint_payment_from_notification(self.session, sale.id)
        mocked.assert_not_called()
        self.assertFalse(result.payment_received)

    def _ready_sale(self) -> Sale:
        with patch("app.sales.service.select_omint_product", return_value=_selection()):
            sale, _created = open_omint_sale(
                self.session,
                self.admin,
                "omint",
                f"{QUOTATION_ID}|{PRODUCT_ID}",
            )
        sale.emission_status = EmissionStatus.PASSENGERS_COMPLETE.value
        sale.provider_context = {
            **sale.provider_context,
            "payment_methods": [
                {
                    "id": "44444444-4444-4444-4444-444444444444",
                    "code": "GNET",
                    "financing": [{"id": "55555555-5555-5555-5555-555555555555", "dues": 1}],
                }
            ],
        }
        self.session.add(sale)
        self.session.commit()
        self.session.refresh(sale)
        return sale

    def _mark_pending(self, sale: Sale) -> None:
        sale.payment_status = "pending"
        self.session.add(
            Payment(
                sale_id=sale.id,
                provider="omint_gateway",
                status="pending",
                amount=sale.amount,
                currency=sale.currency,
                checkout_url="https://gateway.test/checkout",
                provider_data={},
            )
        )
        self.session.add(sale)
        self.session.commit()
        self.session.refresh(sale)
