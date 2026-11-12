"""Listado de ventas para el admin."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401
from app.models.agency import Agency, BillingFrequency, PaymentMethod, TaxCondition
from app.models.company import Company
from app.models.sale import EmissionStatus, Sale, SalePaymentStatus
from app.models.user import User, UserRole
from app.sales.service import SaleFlowError, get_sale_for_user, list_sales


class ListSalesTest(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.omint = Company(slug="omint", name="Omint", active=True)
        self.inter = Company(slug="inter_assist", name="Inter Assist", active=True)
        self.session.add(self.omint)
        self.session.add(self.inter)
        self.session.commit()
        self.session.refresh(self.omint)
        self.session.refresh(self.inter)
        self.admin = self._user("admin@test.com")
        self.seller = self._user("vendedor@test.com", UserRole.SELLER)
        self.agency = Agency(
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
        self.session.add(self.agency)
        self.session.commit()
        self.session.refresh(self.agency)

    def tearDown(self) -> None:
        self.session.close()

    def test_lists_every_sale_newest_first_without_passengers(self) -> None:
        older = self._sale(
            user_id=self.seller.id,
            company_id=self.omint.id,
            emission_status=EmissionStatus.PRODUCT_SELECTED.value,
            created_at=datetime(2026, 11, 1, 10, 0),
        )
        newer = self._sale(
            user_id=self.admin.id,
            company_id=self.inter.id,
            emission_status=EmissionStatus.ISSUED.value,
            payment_status=SalePaymentStatus.APPROVED.value,
            agency_id=self.agency.id,
            created_at=datetime(2026, 11, 10, 10, 0),
        )

        page = list_sales(self.session, limit=50, offset=0)

        self.assertEqual(page.total, 2)
        self.assertEqual([item.id for item in page.items], [newer.id, older.id])
        self.assertEqual(page.items[0].company, "inter_assist")
        self.assertEqual(page.items[0].agency_id, self.agency.id)
        self.assertNotIn("passengers", page.items[0].model_dump())

    def test_filters_by_status_company_agency_and_date(self) -> None:
        self._sale(
            user_id=self.seller.id,
            company_id=self.omint.id,
            emission_status=EmissionStatus.ISSUED.value,
            payment_status=SalePaymentStatus.APPROVED.value,
            agency_id=self.agency.id,
            created_at=datetime(2026, 11, 2, 8, 0),
        )
        self._sale(
            user_id=self.admin.id,
            company_id=self.inter.id,
            emission_status=EmissionStatus.ISSUED.value,
            created_at=datetime(2026, 11, 3, 8, 0),
        )
        self._sale(
            user_id=self.seller.id,
            company_id=self.omint.id,
            emission_status=EmissionStatus.EMISSION_FAILED.value,
            agency_id=self.agency.id,
            created_at=datetime(2026, 11, 4, 8, 0),
        )

        page = list_sales(
            self.session,
            emission_status="issued",
            payment_status="approved",
            company="Omint",
            agency_id=self.agency.id,
            created_from=date(2026, 11, 1),
            created_to=date(2026, 11, 2),
        )

        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].company, "omint")
        self.assertEqual(page.items[0].emission_status, "issued")

    def test_paginates_without_changing_the_total(self) -> None:
        for day in range(3):
            self._sale(
                user_id=self.admin.id,
                company_id=self.omint.id,
                created_at=datetime(2026, 11, 1, 10, 0) + timedelta(days=day),
            )

        page = list_sales(self.session, limit=1, offset=1)

        self.assertEqual(page.total, 3)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.limit, 1)
        self.assertEqual(page.offset, 1)
        self.assertEqual(page.items[0].created_at, datetime(2026, 11, 2, 10, 0))

    def test_admin_can_open_another_users_sale(self) -> None:
        sale = self._sale(
            user_id=self.seller.id,
            company_id=self.omint.id,
            created_at=datetime(2026, 11, 5, 12, 0),
        )

        detail = get_sale_for_user(self.session, self.admin, sale.id)
        foreign = self._sale(
            user_id=self.admin.id,
            company_id=self.omint.id,
            created_at=datetime(2026, 11, 6, 12, 0),
        )

        self.assertEqual(detail.id, sale.id)
        self.assertEqual(detail.created_by_user_id, self.seller.id)
        with self.assertRaises(SaleFlowError) as raised:
            get_sale_for_user(self.session, self.seller, foreign.id)
        self.assertEqual(raised.exception.status_code, 403)

    def test_rejects_unknown_status(self) -> None:
        with self.assertRaises(SaleFlowError) as raised:
            list_sales(self.session, emission_status="emitida")
        self.assertEqual(raised.exception.status_code, 400)

    def _user(self, email: str, role: UserRole = UserRole.ADMIN) -> User:
        user = User(email=email, password_hash="x", role=role, active=True)
        self.session.add(user)
        self.session.commit()
        self.session.refresh(user)
        return user

    def _sale(
        self,
        *,
        user_id: int,
        company_id: int,
        emission_status: str = EmissionStatus.PRODUCT_SELECTED.value,
        payment_status: str = SalePaymentStatus.NOT_STARTED.value,
        agency_id: int | None = None,
        created_at: datetime,
    ) -> Sale:
        sale = Sale(
            created_by_user_id=user_id,
            agency_id=agency_id,
            company_id=company_id,
            emission_status=emission_status,
            payment_status=payment_status,
            quoted_ages=[35],
            external_plan_id="PA-1",
            plan_name="Plan",
            currency="ARS",
            amount=Decimal("100.00"),
            markup=Decimal("0"),
            provider_context={},
            created_at=created_at,
        )
        self.session.add(sale)
        self.session.commit()
        self.session.refresh(sale)
        return sale
