"""Venta Universal: cobro simulado y alta SOAP mockeada."""

from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET
from unittest import TestCase
from unittest.mock import patch

from cryptography.fernet import Fernet

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.models  # noqa: F401
from app.core.config import settings
from app.credentials.service import resolve_universal_organization, upsert_agency_credential
from app.models.agency import Agency, BillingFrequency, PaymentMethod, TaxCondition
from app.models.company import Company
from app.models.sale import EmissionStatus, Sale, SalePaymentStatus, Voucher
from app.models.seller import Seller
from app.models.user import User, UserRole
from app.quotations.schemas import QuoteRequest
from app.sales.schemas import (
    PassengersCreate,
    PaymentSimulate,
    PaymentStart,
    UniversalQuoteSnapshot,
)
from app.sales.service import (
    SaleFlowError,
    confirm_universal_payment,
    get_sale_for_user,
    open_universal_sale,
    set_universal_passengers,
    simulate_universal_payment,
    start_universal_payment,
)
from app.services.companies.universal import UniversalQuoteProvider, universal_coverage_end
from app.services.companies.universal_emission import (
    UniversalPassengerDraft,
    UniversalVoucher,
    _alta_envelope,
    document_type_name,
    universal_charge,
)

ORG = "1-SELLER-ORG"


def _quote(**overrides) -> UniversalQuoteSnapshot:
    data = dict(
        departure_date=date(2026, 6, 20),
        return_date=date(2026, 6, 27),
        ages=[35],
        origin="AR",
        destination_id=3,
        trip_type="unico_viaje",
        plan_name="Master DP 35%OFF (NO INCLUYE 18% IMPUESTO DE LEY)",
        product_name="Master",
        product_id="PROD-1",
        amount_usd=Decimal("120.50"),
        amount_local=Decimal("150000.00"),
        markup=Decimal("0"),
    )
    data.update(overrides)
    return UniversalQuoteSnapshot(**data)


def _passengers(*, document_type: str = "dni") -> PassengersCreate:
    return PassengersCreate.model_validate(
        {
            "emergency_contact": {
                "first_name": "Luis",
                "last_name": "Perez",
                "phone": "+5491100000001",
            },
            "passengers": [
                {
                    "is_holder": True,
                    "first_name": "Maria",
                    "last_name": "Garcia",
                    "birth_date": "1991-03-15",
                    "document_type": document_type,
                    "document_number": "30123456",
                    "email": "maria.garcia@example.com",
                    "phone": "1144556677",
                    "country_id": "AR",
                    "province_id": "BA",
                    "street": "Av. Ejemplo",
                    "street_number": "123",
                    "city": "Buenos Aires",
                }
            ],
        }
    )


def _payment_start() -> PaymentStart:
    return PaymentStart(
        payment_method_id="simulated",
        return_url_ok="https://panel.example/ok",
        return_url_error="https://panel.example/error",
        return_url_pending="https://panel.example/pendiente",
    )


class UniversalChargeTest(TestCase):
    def test_national_charges_pesos_and_abroad_charges_dollars(self) -> None:
        currency, amount, destination = universal_charge(1, Decimal("10"), Decimal("9000"))
        self.assertEqual((currency, amount, destination), ("ARS", Decimal("9000"), "Territorio Nacional"))

        currency, amount, destination = universal_charge(3, Decimal("120.50"), Decimal("9000"))
        self.assertEqual((currency, amount, destination), ("USD", Decimal("120.50"), "Europa"))

    def test_national_without_local_price_is_rejected(self) -> None:
        with self.assertRaises(Exception) as raised:
            universal_charge(1, Decimal("10"), None)
        self.assertEqual(raised.exception.status_code, 400)

    def test_cuit_is_otros_and_the_alta_uses_turismo(self) -> None:
        self.assertEqual(document_type_name("cuit"), "Otros")
        self.assertEqual(document_type_name("passport"), "Pasaporte")
        envelope = _alta_envelope(
            username="WSPRUEBAAR",
            password="secret",
            convenio="1-CONVENIO",
            lead_id="LEAD-1",
            control_number="DYS-UA-1",
            organization_id=ORG,
            plan_name="UNIVERSAL 100",
            destination="Territorio Nacional",
            currency="ARS",
            amount=Decimal("9000"),
            issued_on=date(2026, 11, 16),
            departure_date=date(2026, 6, 20),
            return_date=date(2026, 6, 27),
            passengers=[
                UniversalPassengerDraft(
                    first_name="Maria",
                    last_name="Garcia",
                    birth_date=date(1991, 3, 15),
                    document_type="cuit",
                    document_number="20111222333",
                    email="maria@test.com",
                )
            ],
        )
        self.assertIn("<ua:Canal>Turismo</ua:Canal>", envelope)
        self.assertIn("<ua:MonedaLista>ARS</ua:MonedaLista>", envelope)
        self.assertIn("<ua:TipoDocumentoSolicitante>Otros</ua:TipoDocumentoSolicitante>", envelope)
        self.assertIn("<ua:PaisResidenciaSolicitante>ARGENTINA</ua:PaisResidenciaSolicitante>", envelope)
        self.assertIn(f"<ua:OrganizacionRegistradora>{ORG}</ua:OrganizacionRegistradora>", envelope)
        self.assertNotIn("secret", envelope.split("Password")[0])

    def test_multiviaje_ends_the_day_before_the_anniversary(self) -> None:
        self.assertEqual(
            universal_coverage_end("multiviaje", date(2026, 3, 1), date(2026, 3, 10)),
            date(2027, 2, 28),
        )
        self.assertEqual(
            universal_coverage_end("unico_viaje", date(2026, 6, 20), date(2026, 6, 27)),
            date(2026, 6, 27),
        )
        self.assertEqual(
            universal_coverage_end("multiviaje", date(2024, 2, 29), date(2024, 3, 10)),
            date(2025, 2, 28),
        )


class UniversalQuoteOrganizationTest(TestCase):
    def test_non_argentina_origin_does_not_call_universal(self) -> None:
        provider = UniversalQuoteProvider()
        settings = provider._settings
        previous = (
            settings.universal_username,
            settings.universal_password,
            settings.universal_organizacion_emisora,
        )
        settings.universal_username = "user"
        settings.universal_password = "pass"
        settings.universal_organizacion_emisora = "1-QA"
        request = QuoteRequest(
            departure_date=date(2026, 6, 20),
            return_date=date(2026, 6, 27),
            ages=[35],
            origin="BR",
            destination_id=3,
            trip_type="unico_viaje",
        )
        try:
            with patch("app.services.companies.universal.httpx.Client") as client:
                plans = provider.get_quotes(request, organization_id="1-QA")
            client.assert_not_called()
            self.assertEqual(plans, [])
        finally:
            (
                settings.universal_username,
                settings.universal_password,
                settings.universal_organizacion_emisora,
            ) = previous

    def test_quote_envelope_uses_the_seller_organization(self) -> None:
        provider = UniversalQuoteProvider()
        request = QuoteRequest(
            departure_date=date(2026, 6, 20),
            return_date=date(2026, 6, 27),
            ages=[35],
            origin="AR",
            destination_id=3,
            trip_type="unico_viaje",
        )
        envelope = provider._build_envelope(request, "Europa", "Un viaje", ORG)
        self.assertIn(f"<ual:OrganizacionEmisora>{ORG}</ual:OrganizacionEmisora>", envelope)

    def test_quote_separates_the_commercial_name_from_the_picklist_name(self) -> None:
        datos = ET.fromstring(
            """
            <DatosLeadCotizadorOut>
              <IdLeadOut>1-LEAD</IdLeadOut>
              <IdProducto>1-PROD</IdProducto>
              <Producto>Master DP 35%OFF (NO INCLUYE 18% IMPUESTO DE LEY)</Producto>
              <NombreProducto>Master</NombreProducto>
              <PrecioEmision>121.30</PrecioEmision>
            </DatosLeadCotizadorOut>
            """
        )
        plan = UniversalQuoteProvider()._datos_to_plan(datos)
        self.assertEqual(plan.id, "1-LEAD")
        self.assertEqual(plan.plan_id, "1-PROD")
        self.assertEqual(plan.plan_name, "Master DP 35%OFF (NO INCLUYE 18% IMPUESTO DE LEY)")
        self.assertEqual(plan.product_name, "Master")


class UniversalSaleFlowTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.company = Company(slug="universal", name="Universal", active=True)
        self.session.add(self.company)
        self.session.commit()
        self.session.refresh(self.company)
        self.owner = self._user("vendedor@test.com", UserRole.SELLER)
        self.other = self._user("otro@test.com", UserRole.SELLER)
        self.session.add(
            Seller(
                user_id=self.owner.id,
                first_name="Betiana",
                last_name="Uzal",
                address="Calle 1",
                nationality="AR",
                birth_date=date(1990, 1, 1),
                comments="",
                commission=Decimal("0"),
            )
        )
        self.session.commit()
        self._previous_org = settings.universal_organizacion_emisora
        self._previous_key = settings.password_encryption_key
        settings.universal_organizacion_emisora = ORG
        settings.password_encryption_key = Fernet.generate_key().decode()

    def tearDown(self) -> None:
        settings.universal_organizacion_emisora = self._previous_org
        settings.password_encryption_key = self._previous_key
        self.session.close()

    def test_seller_organization_is_frozen_on_the_sale(self) -> None:
        self.assertEqual(resolve_universal_organization(self.session, self.owner), ORG)
        sale, created = open_universal_sale(
            self.session,
            self.owner,
            "universal",
            "LEAD-9",
            _quote(),
        )
        self.assertTrue(created)
        self.assertEqual(sale.currency, "USD")
        self.assertEqual(sale.amount, Decimal("120.50"))
        self.assertEqual(sale.provider_context["organization_id"], ORG)
        self.assertEqual(sale.provider_reference, "universal:LEAD-9")

    def test_agency_organization_replaces_the_server_one(self) -> None:
        agency = Agency(
            name="Norte",
            legal_name="Norte SA",
            tax_id="30111222333",
            address="Calle 1",
            country="AR",
            legal_representative_name="Ana",
            agency_email="norte@test.com",
            office_phone="111",
            activation_date=date(2026, 1, 1),
            billing_frequency=BillingFrequency.MONTHLY,
            payment_method=PaymentMethod.TRANSFER,
            tax_condition=TaxCondition.RESPONSABLE_INSCRIPTO,
        )
        self.session.add(agency)
        self.session.commit()
        self.session.refresh(agency)
        seller = self.session.exec(select(Seller).where(Seller.user_id == self.owner.id)).one()
        seller.agency_id = agency.id
        self.session.add(seller)
        self.session.commit()
        upsert_agency_credential(
            self.session,
            agency.id,
            "universal",
            {"organizacion_emisora": "1-AGENCIA"},
        )

        sale, _created = open_universal_sale(
            self.session,
            self.owner,
            "universal",
            "LEAD-AGENCIA",
            _quote(),
        )

        self.assertEqual(resolve_universal_organization(self.session, self.owner), "1-AGENCIA")
        self.assertEqual(sale.provider_context["organization_id"], "1-AGENCIA")

    def test_origin_outside_argentina_is_rejected(self) -> None:
        with self.assertRaises(SaleFlowError) as raised:
            open_universal_sale(
                self.session,
                self.owner,
                "universal",
                "LEAD-9",
                _quote(origin="BR"),
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_confirm_stores_one_voucher_for_the_sale(self) -> None:
        sale, _created = open_universal_sale(
            self.session,
            self.owner,
            "universal",
            "LEAD-1",
            _quote(destination_id=1),
        )
        self.assertEqual(sale.currency, "ARS")
        self.assertEqual(sale.amount, Decimal("150000.00"))
        set_universal_passengers(self.session, self.owner, sale.id, _passengers(document_type="cuit"))
        start_universal_payment(self.session, self.owner, sale.id, _payment_start())
        simulate_universal_payment(
            self.session,
            self.owner,
            sale.id,
            PaymentSimulate(result="approved"),
        )
        issued = UniversalVoucher(
            number="VCH-100",
            status_code=200,
            request_body={"lead_id": "LEAD-1", "currency": "ARS"},
            response_body={"voucher_number": "VCH-100"},
        )
        with (
            patch(
                "app.sales.service.emit_universal_voucher",
                return_value=issued,
            ) as emit,
            patch("app.sales.service.retire_universal_lead") as retire,
        ):
            confirmed = confirm_universal_payment(self.session, self.owner, sale.id)

        emit.assert_called_once()
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["organization_id"], ORG)
        self.assertEqual(kwargs["plan_name"], "Master")
        self.assertEqual(kwargs["currency"], "ARS")
        self.assertEqual(kwargs["destination"], "Territorio Nacional")
        self.assertEqual(kwargs["passengers"][0].document_type, "cuit")
        retire.assert_called_once_with("LEAD-1")
        self.assertTrue(confirmed.payment_received)
        self.assertEqual(confirmed.voucher_number, "VCH-100")
        stored = self.session.exec(select(Voucher).where(Voucher.sale_id == sale.id)).all()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].number, "VCH-100")
        self.assertEqual(
            self.session.get(Sale, sale.id).emission_status,
            EmissionStatus.ISSUED.value,
        )

    def test_another_user_cannot_confirm(self) -> None:
        sale, _created = open_universal_sale(
            self.session,
            self.owner,
            "universal",
            "LEAD-2",
            _quote(),
        )
        with self.assertRaises(SaleFlowError) as raised:
            confirm_universal_payment(self.session, self.other, sale.id)
        self.assertEqual(raised.exception.status_code, 403)

    def test_detail_is_available_for_universal(self) -> None:
        sale, _created = open_universal_sale(
            self.session,
            self.owner,
            "universal",
            "LEAD-3",
            _quote(),
        )
        detail = get_sale_for_user(self.session, self.owner, sale.id)
        self.assertEqual(detail.company, "universal")
        self.assertEqual(detail.emission_status, EmissionStatus.PRODUCT_SELECTED.value)
        self.assertEqual(detail.payment_status, SalePaymentStatus.NOT_STARTED.value)

    def _user(self, email: str, role: UserRole) -> User:
        user = User(email=email, password_hash="x", role=role, active=True)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user
