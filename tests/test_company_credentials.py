"""Credenciales de agencia: Cardinal propio, y el .env cuando no hay fila."""

import json
from datetime import date
from unittest import TestCase
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401
from app.core.config import settings
from app.credentials.schemas import CardinalCredentials
from app.credentials.service import (
    CredentialServiceError,
    delete_agency_credential,
    list_agency_credentials,
    resolve_cardinal_credentials,
    upsert_agency_credential,
)
from app.models.agency import Agency, BillingFrequency, PaymentMethod, TaxCondition
from app.models.company import Company
from app.models.seller import Seller
from app.models.user import User, UserRole
from app.quotations.schemas import QuoteRequest
from app.services.companies.cardinal import CardinalQuoteProvider

AGENCY_GUID = "agency-guid-1111"
ADMIN_GUID = "admin-guid-from-env"


def _agency(name: str, tax_id: str) -> Agency:
    return Agency(
        name=name,
        legal_name=f"{name} SA",
        tax_id=tax_id,
        address="Calle 1",
        country="AR",
        legal_representative_name="Ana",
        agency_email=f"{name}@test.com",
        office_phone="111",
        activation_date=date(2026, 1, 1),
        billing_frequency=BillingFrequency.MONTHLY,
        payment_method=PaymentMethod.TRANSFER,
        tax_condition=TaxCondition.RESPONSABLE_INSCRIPTO,
    )


class AgencyCredentialTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.company = Company(slug="cardinal", name="Cardinal", active=True)
        self.session.add(self.company)
        self.agency = _agency("Norte", "30111222333")
        self.session.add(self.agency)
        self.session.commit()
        self.session.refresh(self.agency)
        self._previous_key = settings.password_encryption_key
        self._previous_guid = settings.cardinal_agente_emisor_guid
        self._previous_secret = settings.cardinal_agente_emisor_secreto
        settings.password_encryption_key = Fernet.generate_key().decode()
        settings.cardinal_agente_emisor_guid = ADMIN_GUID
        settings.cardinal_agente_emisor_secreto = "admin-secret"

    def tearDown(self) -> None:
        settings.password_encryption_key = self._previous_key
        settings.cardinal_agente_emisor_guid = self._previous_guid
        settings.cardinal_agente_emisor_secreto = self._previous_secret
        self.session.close()

    def test_save_returns_plaintext_and_falls_back_after_delete(self) -> None:
        saved = upsert_agency_credential(
            self.session,
            self.agency.id,
            "cardinal",
            {"agente_emisor_guid": AGENCY_GUID, "agente_emisor_secreto": "secreto-agencia"},
        )
        self.assertFalse(saved.uses_default)
        self.assertEqual(saved.credentials["agente_emisor_guid"], AGENCY_GUID)
        self.assertEqual(saved.credentials["agente_emisor_secreto"], "secreto-agencia")

        listed = list_agency_credentials(self.session, self.agency.id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].credentials["agente_emisor_guid"], AGENCY_GUID)

        delete_agency_credential(self.session, self.agency.id, "cardinal")
        after = list_agency_credentials(self.session, self.agency.id)[0]
        self.assertTrue(after.uses_default)
        self.assertIsNone(after.credentials)

    def test_agency_without_row_uses_admin_guid(self) -> None:
        user = self._user("agencia@test.com", UserRole.AGENCY, self.agency.id)
        resolved = resolve_cardinal_credentials(self.session, user)
        self.assertEqual(resolved.agente_emisor_guid, ADMIN_GUID)

    def test_agency_and_its_seller_use_the_agency_guid(self) -> None:
        upsert_agency_credential(
            self.session,
            self.agency.id,
            "cardinal",
            {"agente_emisor_guid": AGENCY_GUID},
        )
        agency_user = self._user("agencia@test.com", UserRole.AGENCY, self.agency.id)
        seller_user = self._user("vendedor@test.com", UserRole.SELLER, None)
        self.session.add(
            Seller(
                user_id=seller_user.id,
                agency_id=self.agency.id,
                first_name="Juan",
                last_name="Perez",
                address="Calle 2",
                nationality="AR",
                birth_date=date(1990, 1, 1),
                comments="",
            )
        )
        self.session.commit()

        self.assertEqual(
            resolve_cardinal_credentials(self.session, agency_user).agente_emisor_guid,
            AGENCY_GUID,
        )
        self.assertEqual(
            resolve_cardinal_credentials(self.session, seller_user).agente_emisor_guid,
            AGENCY_GUID,
        )

    def test_another_agency_does_not_receive_the_saved_guid(self) -> None:
        upsert_agency_credential(
            self.session,
            self.agency.id,
            "cardinal",
            {"agente_emisor_guid": AGENCY_GUID},
        )
        other = _agency("Sur", "30999888777")
        self.session.add(other)
        self.session.commit()
        self.session.refresh(other)
        other_user = self._user("sur@test.com", UserRole.AGENCY, other.id)
        resolved = resolve_cardinal_credentials(self.session, other_user)
        self.assertEqual(resolved.agente_emisor_guid, ADMIN_GUID)

    def test_omint_is_not_configurable(self) -> None:
        with self.assertRaises(CredentialServiceError) as raised:
            upsert_agency_credential(
                self.session,
                self.agency.id,
                "omint",
                {"client_id": "x", "client_secret": "y"},
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_quote_sends_the_agency_guid(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            if request.url.path.endswith("/parametros"):
                return httpx.Response(200, json={"origenes": [{"id": 9, "nombre": "Argentina"}]})
            return httpx.Response(200, json={"cotizacion": {"guid": "q1", "productos": []}})

        transport = httpx.MockTransport(handler)

        class _Client(httpx.Client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        provider = CardinalQuoteProvider()
        provider._destino_ids = {3: 55}
        request = QuoteRequest(
            departure_date=date(2026, 12, 1),
            return_date=date(2026, 12, 10),
            ages=[35],
            destination_id=3,
        )
        with patch("app.services.companies.cardinal.httpx.Client", _Client):
            provider.get_quotes(
                request,
                credentials=CardinalCredentials(
                    agente_emisor_guid=AGENCY_GUID,
                    agente_emisor_secreto="no-sale-en-la-cotizacion",
                ),
            )

        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(body["agenteEmisorGuid"] == AGENCY_GUID for body in bodies))
        self.assertNotIn("no-sale-en-la-cotizacion", json.dumps(bodies))

    def _user(self, email: str, role: UserRole, agency_id: int | None) -> User:
        user = User(
            email=email,
            password_hash="x",
            role=role,
            active=True,
            agency_id=agency_id,
        )
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user
