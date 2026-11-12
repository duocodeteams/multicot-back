"""Emisión Cardinal: costoFinal, pago simulado y un voucher por pasajero."""

import json
from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.models  # noqa: F401
from app.core.config import Settings
from app.credentials.schemas import CardinalCredentials
from app.models.company import Company
from app.models.sale import EmissionStatus, Sale, SalePaymentStatus, Voucher
from app.models.user import User, UserRole
from app.sales.schemas import PassengersCreate, PaymentSimulate, PaymentStart
from app.sales.service import (
    SaleFlowError,
    confirm_cardinal_payment,
    open_cardinal_sale,
    set_cardinal_passengers,
    simulate_cardinal_payment,
    start_cardinal_payment,
)
from app.services.companies.cardinal_emission import (
    CardinalEmissionError,
    CardinalEmissionResult,
    CardinalPassengerDraft,
    CardinalQuoteSelection,
    CardinalVoucher,
    build_emitir_payload,
    document_type_id,
    emit_cardinal_sale,
    fetch_cardinal_quotation,
)

GUID = "a" * 32
SECRET = "b" * 96
PRODUCT_ID = 15
QUOTE_ID = f"{GUID}|{PRODUCT_ID}"


def _credentials() -> CardinalCredentials:
    return CardinalCredentials(agente_emisor_guid=GUID, agente_emisor_secreto=SECRET)


def _settings() -> Settings:
    settings = Settings()
    settings.cardinal_base_url = "https://cardinal.test/webservice"
    settings.cardinal_destino_europa_id = 8
    return settings


def _selection() -> CardinalQuoteSelection:
    return CardinalQuoteSelection(
        cotizacion_guid=GUID,
        producto_id=PRODUCT_ID,
        plan_name="Europa 100",
        amount=Decimal("120.50"),
        currency_id=3,
        currency="USD",
        base_amount=Decimal("150.00"),
        origen_id=6,
        destino_id=8,
        destination_id=3,
        departure_date=date(2026, 8, 1),
        return_date=date(2026, 8, 10),
        ages=[35],
        trip_type="unico_viaje",
        days_range=None,
        request_body={"cotizacionGuid": GUID, "agenteEmisorSecreto": "***"},
        response_body={"resultado": "ok"},
    )


def _passengers_payload() -> dict:
    return {
        "receive_promotions": False,
        "emergency_contact": {
            "first_name": "Luis",
            "last_name": "Perez",
            "phone": "+5491100000001",
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
                "country_id": "11",
                "province_id": "1",
                "country": "Argentina",
                "province": "Buenos Aires",
                "street": "Av. Ejemplo",
                "street_number": "123",
                "city": "Buenos Aires",
            }
        ],
    }


def _draft() -> CardinalPassengerDraft:
    return CardinalPassengerDraft(
        first_name="Ana",
        last_name="Perez",
        birth_date=date(1991, 4, 22),
        document_type="dni",
        document_type_id=1,
        document_number="30111222",
        email="ana.perez@example.com",
        phone="+5491100000000",
        address="Av. Ejemplo 123",
        city="Buenos Aires",
        country_id=11,
        emergency_first_name="Luis",
        emergency_last_name="Perez",
        emergency_phone="+5491100000001",
    )


def _payment_start() -> PaymentStart:
    return PaymentStart(
        return_url_ok="https://panel.example/ok",
        return_url_error="https://panel.example/error",
        return_url_pending="https://panel.example/pendiente",
    )


class CardinalEmitPayloadTest(TestCase):
    def test_monto_is_costo_final_and_secret_is_redacted(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            if request.url.path.endswith("/obtener_cotizacion"):
                return httpx.Response(
                    200,
                    json={
                        "resultado": "ok",
                        "cotizacion": {
                            "guid": GUID,
                            "parametros": {
                                "origenId": 6,
                                "destinoId": 8,
                                "fechaSalida": "2026-08-01",
                                "fechaRegreso": "2026-08-10",
                                "edades": [35],
                                "tarifaAnual": False,
                            },
                            "productos": [
                                {
                                    "productoId": PRODUCT_ID,
                                    "productoNombre": "Europa 100",
                                    "costoFinal": {"amount": "120.50", "currency": 3, "scale": 2},
                                    "costoFinalMonedaPais": {
                                        "amount": "150000.00",
                                        "currency": 7,
                                        "scale": 2,
                                    },
                                    "costoLista": {"amount": "150.00", "currency": 3, "scale": 2},
                                }
                            ],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "resultado": "ok",
                    "vouchers": [
                        {
                            "nroVoucher": "C-100",
                            "linkImprimir": "https://cardinal.test/print/C-100",
                            "nroDocumento": "30111222",
                        }
                    ],
                },
            )

        transport = httpx.MockTransport(handler)

        class _Client(httpx.Client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        with patch("app.services.companies.cardinal_emission.get_settings", _settings):
            with _Client() as client:
                selection = fetch_cardinal_quotation(
                    _credentials(), GUID, PRODUCT_ID, client=client
                )
                result = emit_cardinal_sale(
                    _credentials(),
                    GUID,
                    PRODUCT_ID,
                    selection.amount,
                    [_draft()],
                    client=client,
                )

        self.assertEqual(selection.amount, Decimal("120.50"))
        self.assertEqual(selection.currency, "USD")
        self.assertEqual(selection.destination_id, 3)
        self.assertEqual(captured[1]["montoCotizado"], 120.5)
        self.assertNotIn("usuarioGuid", captured[1])
        self.assertEqual(result.request_body["agenteEmisorSecreto"], "***")
        self.assertEqual(result.vouchers[0].number, "C-100")
        self.assertEqual(result.vouchers[0].url, "https://cardinal.test/print/C-100")

    def test_document_type_maps_dni(self) -> None:
        catalog = [{"id": "4", "name": "Pasaporte"}, {"id": "1", "name": "DNI"}]
        self.assertEqual(document_type_id("dni", catalog), 1)

    def test_payload_copies_emergency_contact_onto_the_passenger(self) -> None:
        body = build_emitir_payload(_credentials(), GUID, PRODUCT_ID, Decimal("10.00"), [_draft()])
        passenger = body["pasajeros"][0]
        self.assertEqual(passenger["emergenciaNombre"], "Luis")
        self.assertEqual(passenger["domicilio"], "Av. Ejemplo 123")
        self.assertEqual(passenger["paisId"], 11)
        self.assertNotIn("paisesDestino", passenger)

    def test_missing_secret_is_rejected(self) -> None:
        with self.assertRaises(CardinalEmissionError) as raised:
            build_emitir_payload(
                CardinalCredentials(agente_emisor_guid=GUID, agente_emisor_secreto=""),
                GUID,
                PRODUCT_ID,
                Decimal("10.00"),
                [_draft()],
            )
        self.assertEqual(raised.exception.status_code, 400)


class CardinalSaleFlowTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Company(slug="cardinal", name="Cardinal", active=True))
        self.session.commit()
        self.admin = User(
            email="admin@test.com", password_hash="x", role=UserRole.ADMIN, active=True
        )
        self.session.add(self.admin)
        self.session.commit()
        self.session.refresh(self.admin)

    def tearDown(self) -> None:
        self.session.close()

    def test_happy_path_emits_one_voucher_per_passenger(self) -> None:
        with patch("app.sales.service.fetch_cardinal_quotation", return_value=_selection()):
            sale, created = open_cardinal_sale(self.session, self.admin, "cardinal", QUOTE_ID)
        self.assertTrue(created)
        self.assertEqual(sale.amount, Decimal("120.50"))
        self.assertEqual(sale.currency, "USD")
        self.assertEqual(sale.markup, Decimal("0"))
        self.assertEqual(sale.emission_status, EmissionStatus.PRODUCT_SELECTED.value)

        with patch(
            "app.sales.service.list_cardinal_parameters",
            return_value=[{"id": "1", "name": "DNI"}],
        ):
            set_cardinal_passengers(
                self.session,
                self.admin,
                sale.id or 0,
                PassengersCreate.model_validate(_passengers_payload()),
            )
        start_cardinal_payment(self.session, self.admin, sale.id or 0, _payment_start())
        simulate_cardinal_payment(
            self.session, self.admin, sale.id or 0, PaymentSimulate(result="approved")
        )
        emitted = CardinalEmissionResult(
            vouchers=[
                CardinalVoucher(
                    number="C-100",
                    url="https://cardinal.test/print/C-100",
                    document_number="30111222",
                )
            ],
            status_code=200,
            request_body={"agenteEmisorSecreto": "***", "montoCotizado": 120.5},
            response_body={"resultado": "ok"},
        )
        with patch("app.sales.service.emit_cardinal_sale", return_value=emitted) as emit:
            confirm = confirm_cardinal_payment(self.session, self.admin, sale.id or 0)
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[3], Decimal("120.50"))
        self.assertTrue(confirm.payment_received)
        self.assertEqual(confirm.voucher_number, "C-100")
        self.assertEqual(confirm.voucher_url, "https://cardinal.test/print/C-100")
        self.assertEqual(len(confirm.vouchers), 1)
        self.session.refresh(sale)
        self.assertEqual(sale.emission_status, EmissionStatus.ISSUED.value)
        voucher = self.session.exec(select(Voucher)).one()
        self.assertEqual(voucher.number, "C-100")
        self.assertIsNotNone(voucher.passenger_id)

    def test_same_quote_returns_existing(self) -> None:
        with patch("app.sales.service.fetch_cardinal_quotation", return_value=_selection()) as fetch:
            first, _ = open_cardinal_sale(self.session, self.admin, "cardinal", QUOTE_ID)
            second, created = open_cardinal_sale(self.session, self.admin, "cardinal", QUOTE_ID)
        self.assertFalse(created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(self.session.exec(select(Sale)).all()), 1)

    def test_confirm_while_pending_does_not_emit(self) -> None:
        with patch("app.sales.service.fetch_cardinal_quotation", return_value=_selection()):
            sale, _ = open_cardinal_sale(self.session, self.admin, "cardinal", QUOTE_ID)
        with patch(
            "app.sales.service.list_cardinal_parameters",
            return_value=[{"id": "1", "name": "DNI"}],
        ):
            set_cardinal_passengers(
                self.session,
                self.admin,
                sale.id or 0,
                PassengersCreate.model_validate(_passengers_payload()),
            )
        start_cardinal_payment(self.session, self.admin, sale.id or 0, _payment_start())
        with patch("app.sales.service.emit_cardinal_sale") as emit:
            confirm = confirm_cardinal_payment(self.session, self.admin, sale.id or 0)
        emit.assert_not_called()
        self.assertFalse(confirm.payment_received)
        self.assertEqual(confirm.payment_status, SalePaymentStatus.PENDING.value)

    def test_passenger_without_address_is_rejected(self) -> None:
        with patch("app.sales.service.fetch_cardinal_quotation", return_value=_selection()):
            sale, _ = open_cardinal_sale(self.session, self.admin, "cardinal", QUOTE_ID)
        payload = _passengers_payload()
        payload["passengers"].append(
            {
                "is_holder": False,
                "first_name": "Leo",
                "last_name": "Perez",
                "birth_date": "2016-04-22",
                "document_type": "dni",
                "document_number": "50111222",
                "email": "leo.perez@example.com",
                "phone": "+5491100000002",
                "country_id": "11",
                "city": "Buenos Aires",
            }
        )
        sale.quoted_ages = [35, 10]
        self.session.add(sale)
        self.session.commit()
        with self.assertRaises(SaleFlowError) as raised:
            set_cardinal_passengers(
                self.session,
                self.admin,
                sale.id or 0,
                PassengersCreate.model_validate(payload),
            )
        self.assertEqual(raised.exception.status_code, 400)
