"""Cancelar una venta sin liberar la cotización."""

from datetime import date, datetime
from decimal import Decimal
from unittest import TestCase

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.models  # noqa: F401
from app.models.company import Company
from app.models.sale import EmissionStatus, Passenger, Sale, SalePaymentStatus
from app.models.user import User, UserRole
from app.sales.schemas import PassengersCreate
from app.sales.service import SaleFlowError, cancel_sale, set_omint_passengers


class CancelSaleTest(TestCase):
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
        self.owner = self._user("vendedor@test.com", UserRole.SELLER)
        self.admin = self._user("admin@test.com", UserRole.ADMIN)
        self.other = self._user("otro@test.com", UserRole.SELLER)

    def tearDown(self) -> None:
        self.session.close()

    def test_owner_cancels_and_keeps_the_quote_and_passengers(self) -> None:
        sale = self._sale(provider_reference="omint:quote-1")
        self.session.add(
            Passenger(
                sale_id=sale.id,
                is_holder=True,
                first_name="Ana",
                last_name="Perez",
                birth_date=date(1990, 1, 1),
                document_type="dni",
                document_number="30111222",
                provider_data={},
            )
        )
        self.session.commit()

        detail = cancel_sale(self.session, self.owner, sale.id)

        self.assertEqual(detail.emission_status, EmissionStatus.CANCELLED.value)
        self.assertEqual(detail.payment_status, SalePaymentStatus.NOT_STARTED.value)
        self.assertEqual(len(detail.passengers), 1)
        stored = self.session.get(Sale, sale.id)
        self.assertEqual(stored.provider_reference, "omint:quote-1")
        self.assertEqual(len(self.session.exec(select(Passenger)).all()), 1)

    def test_admin_cancels_a_rejected_sale_from_another_user(self) -> None:
        sale = self._sale(payment_status=SalePaymentStatus.REJECTED.value)

        detail = cancel_sale(self.session, self.admin, sale.id)

        self.assertEqual(detail.emission_status, EmissionStatus.CANCELLED.value)

    def test_second_cancel_returns_the_same_sale(self) -> None:
        sale = self._sale()
        cancel_sale(self.session, self.owner, sale.id)

        detail = cancel_sale(self.session, self.owner, sale.id)

        self.assertEqual(detail.emission_status, EmissionStatus.CANCELLED.value)

    def test_another_user_cannot_cancel(self) -> None:
        sale = self._sale()

        with self.assertRaises(SaleFlowError) as raised:
            cancel_sale(self.session, self.other, sale.id)

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(
            self.session.get(Sale, sale.id).emission_status,
            EmissionStatus.PRODUCT_SELECTED.value,
        )

    def test_pending_or_approved_payment_stays_open(self) -> None:
        pending = self._sale(payment_status=SalePaymentStatus.PENDING.value)
        approved = self._sale(
            payment_status=SalePaymentStatus.APPROVED.value,
            emission_status=EmissionStatus.ISSUED.value,
            provider_reference="omint:quote-2",
        )

        for sale in (pending, approved):
            with self.assertRaises(SaleFlowError) as raised:
                cancel_sale(self.session, self.owner, sale.id)
            self.assertEqual(raised.exception.status_code, 409)

        self.assertEqual(
            self.session.get(Sale, pending.id).emission_status,
            EmissionStatus.PRODUCT_SELECTED.value,
        )
        self.assertEqual(
            self.session.get(Sale, approved.id).emission_status,
            EmissionStatus.ISSUED.value,
        )

    def test_cancelled_sale_cannot_load_passengers(self) -> None:
        sale = self._sale()
        cancel_sale(self.session, self.owner, sale.id)
        payload = PassengersCreate.model_validate(
            {
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
                        "birth_date": "1990-01-01",
                        "document_type": "dni",
                        "document_number": "30111222",
                        "email": "ana@test.com",
                        "phone": "111",
                        "country_id": "1",
                        "province_id": "2",
                        "street": "Calle",
                        "street_number": "1",
                        "city": "CABA",
                    }
                ],
            }
        )

        with self.assertRaises(SaleFlowError) as raised:
            set_omint_passengers(self.session, self.owner, sale.id, payload)

        self.assertEqual(raised.exception.status_code, 409)

    def _user(self, email: str, role: UserRole) -> User:
        user = User(email=email, password_hash="x", role=role, active=True)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _sale(
        self,
        *,
        payment_status: str = SalePaymentStatus.NOT_STARTED.value,
        emission_status: str = EmissionStatus.PRODUCT_SELECTED.value,
        provider_reference: str | None = None,
    ) -> Sale:
        sale = Sale(
            created_by_user_id=self.owner.id,
            company_id=self.company.id,
            emission_status=emission_status,
            payment_status=payment_status,
            provider_reference=provider_reference,
            quoted_ages=[35],
            external_plan_id="PA-1",
            plan_name="Plan",
            currency="ARS",
            amount=Decimal("100.00"),
            markup=Decimal("0"),
            provider_context={},
            created_at=datetime(2026, 11, 13, 10, 0),
        )
        self.session.add(sale)
        self.session.commit()
        self.session.refresh(sale)
        return sale
